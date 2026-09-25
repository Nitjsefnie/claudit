"""/login GET + POST + /logout.

Login UI is inlined HTML (no shared layout chrome — claudit uses
its own visualizer dark theme). Every credential failure — unknown
id, no web password configured, wrong password — answers one generic
401 with an identical body, and every failure costs about one PBKDF2
run at the write count: the real verification where it can run, a
dummy remainder run on top where it cannot or would run cheaper (a
legacy 200k hash, a malformed stored hash), so an account id cannot
be enumerated by response shape or timing (issue #109) — with one
documented residual: a stored hash versioned above the target count
still costs longer. Rate limiting: 5 failures per IP+user pair per
5-minute window (issue #111), so one user's failures never lock a
different user behind the same egress IP; entries are pruned per key
on access and, once the table grows past _LOGIN_MAX_KEYS, every fully
expired key is swept, so it never grows without bound.
"""
from __future__ import annotations

import html
import logging
import time

from fastapi import APIRouter, Form, Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from backend import auth, branding
from backend import session as session_mod


router = APIRouter()

log = logging.getLogger("claudit.login")

_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_MAX_FAILURES = 5
_LOGIN_WINDOW_SECONDS = 300
# Sweep trigger: above this many tracked (ip, user) pairs, every key
# whose window has fully expired is dropped on the next access, so the
# dict cannot be grown without bound by a remote peer.
_LOGIN_MAX_KEYS = 4096

# One answer for every credential failure — identical status and body
# for unknown id, no web password and wrong password (issue #109).
_GENERIC_FAILURE_TEXT = "Invalid credentials."


def _failure_key(ip: str, uid: int) -> str:
    return f"{ip}:{uid}"


def _prune_key(key: str, now: float) -> list[float]:
    """Drop one key's expired timestamps; delete the key once empty."""
    attempts = [
        t for t in _LOGIN_FAILURES.get(key, [])
        if now - t < _LOGIN_WINDOW_SECONDS
    ]
    if attempts:
        _LOGIN_FAILURES[key] = attempts
    else:
        _LOGIN_FAILURES.pop(key, None)
    return attempts


def _sweep_expired_keys(now: float) -> None:
    """Delete every key whose timestamps have all aged out of the
    window. Runs only above _LOGIN_MAX_KEYS, so the steady-state cost
    stays at one key's prune."""
    expired = [
        key for key, attempts in _LOGIN_FAILURES.items()
        if not any(now - t < _LOGIN_WINDOW_SECONDS for t in attempts)
    ]
    for key in expired:
        del _LOGIN_FAILURES[key]


def _check_login_rate_limit(ip: str, uid: int) -> bool:
    now = time.time()
    attempts = _prune_key(_failure_key(ip, uid), now)
    if len(_LOGIN_FAILURES) > _LOGIN_MAX_KEYS:
        _sweep_expired_keys(now)
    return len(attempts) >= _LOGIN_MAX_FAILURES


def _record_login_failure(ip: str, uid: int) -> None:
    now = time.time()
    key = _failure_key(ip, uid)
    attempts = _prune_key(key, now)
    attempts.append(now)
    _LOGIN_FAILURES[key] = attempts
    if len(_LOGIN_FAILURES) > _LOGIN_MAX_KEYS:
        _sweep_expired_keys(now)


def reset_login_rate_limits() -> None:
    """Clear the process-global failure dict. Tests need this between
    cases that POST from the same TestClient host; production never
    calls it."""
    _LOGIN_FAILURES.clear()


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8" />
<title>Sign in · {app_name}</title>
<style>
  body {{ background:#0b0d10; color:#dde; font-family: 'Inter',sans-serif;
         display:flex; align-items:center; justify-content:center;
         min-height:100vh; margin:0; }}
  form {{ background:#14181d; padding:24px 28px; border:1px solid #25303c;
         border-radius:8px; min-width:320px; }}
  h1 {{ margin:0 0 16px 0; font-size:18px; letter-spacing:.04em; color:#9bd; }}
  label {{ display:block; margin:10px 0 4px 0; font-size:12px; color:#8aa; }}
  input {{ width:100%; box-sizing:border-box; padding:8px 10px;
          background:#0e1216; color:#dde; border:1px solid #25303c;
          border-radius:4px; font: 14px 'JetBrains Mono', monospace; }}
  button {{ margin-top:18px; width:100%; padding:10px 14px;
           background:#1f6f9c; color:#fff; border:0; border-radius:4px;
           font-weight:600; cursor:pointer; }}
  .guest-btn {{ background:#1a1c2e; border:1px solid #25303c; }}
  .guest-btn:hover {{ background:#222640; }}
  .or {{ text-align:center; color:#556; font-size:11px; margin:16px 0 4px; letter-spacing:.2em; }}
  .err {{ color:#e76; font-size:12px; min-height:16px; margin-top:8px; }}
</style>
</head><body>
<form method="post" action="/login">
  <h1>{app_name} · sign in</h1>
  <label>Username</label>
  <input name="user_id" required inputmode="numeric" pattern="[0-9]+"
         autocomplete="username">
  <label>Password</label>
  <input name="password" type="password" required autocomplete="current-password">
  <button type="submit">Sign in</button>
  <div class="err">{err}</div>
  <div class="or">or</div>
  <button type="submit" class="guest-btn"
          formaction="/login/guest" formmethod="post" formnovalidate>
    Continue as guest
  </button>
</form>
</body></html>
"""


@router.get("/login")
async def login_page(request: Request) -> HTMLResponse:
    # The upper-cased APP_NAME is the page's brand word; html-escaped —
    # both slots are real HTML contexts. Default = today's CLAUDIT.
    return HTMLResponse(_LOGIN_HTML.format(
        err="",
        app_name=html.escape(branding.brand_name().upper(), quote=True),
    ))


@router.post("/login")
async def login_post(
    request: Request,
    user_id: str = Form(""),
    password: str = Form(""),
) -> Response:
    ip = request.client.host if request.client else "unknown"
    try:
        uid = int(user_id.strip())
    except ValueError:
        uid = 0
    if uid <= 0:
        # Malformed input reveals nothing about accounts — uid 0 is the
        # guest sentinel — and it costs no query and no PBKDF2, so it
        # answers before the limiter with its own fixed text.
        return Response(
            "Invalid user ID", status_code=400, media_type="text/plain"
        )
    if _check_login_rate_limit(ip, uid):
        return Response(
            "Too many login attempts. Try again later.",
            status_code=429, media_type="text/plain",
        )
    config = session_mod.load_user_config(uid)
    if not config or not auth.has_web_password(config):
        # The real verification cannot run: normalize from zero — burn
        # the CPU the real verification would cost — and give the same
        # generic answer a wrong password gets, so neither response
        # shape nor timing separates the two (#109).
        auth.normalize_verification_timing(password, 0)
        _record_login_failure(ip, uid)
        return Response(
            _GENERIC_FAILURE_TEXT, status_code=401, media_type="text/plain"
        )
    if not auth.verify_web_password(config, password):
        # Top up whatever the real verification spent (its own count
        # for a versioned hash, the legacy count for bare hex, zero
        # for a malformed hash that ran no PBKDF2 at all) so a failure
        # costs ≈ the target whatever shape the stored hash is (#109).
        auth.normalize_verification_timing(
            password, auth.stored_verification_iterations(config)
        )
        _record_login_failure(ip, uid)
        return Response(
            _GENERIC_FAILURE_TEXT, status_code=401, media_type="text/plain"
        )
    secret, generation = session_mod.get_or_create_session_row(uid)
    token = session_mod.make_session_token(uid, secret, generation=generation)
    response = RedirectResponse("/", status_code=303)
    session_mod.set_session_cookie(response, token)
    return response


@router.get("/logout")
async def logout(request: Request) -> Response:
    # /logout stays on the public path list and self-authenticates via
    # the cookie: it acts only on the session the request itself
    # presents. SameSite=strict already keeps a cross-site GET /logout
    # from carrying the cookie, so it cannot name — or bump — a session
    # it does not hold (issue #108).
    #
    # Resolve-and-bump is best-effort: this is the one route whose job
    # is dropping credentials, so it must not fail closed — if the
    # session store is unavailable, the cookie is still deleted and the
    # redirect still returned, and only the server-side invalidation
    # (issue #108, defense in depth on top of the cookie deletion) is
    # deferred until the store answers again.
    cookie = request.cookies.get(session_mod.SESSION_COOKIE_NAME, "")
    try:
        user_id = (
            session_mod.resolve_session_user_id(cookie) if cookie else None
        )
        if user_id is not None and user_id != session_mod.GUEST_USER_ID:
            session_mod.bump_session_generation(user_id)
    except Exception:
        log.warning(
            "logout: session store unavailable; clearing the cookie "
            "without the server-side generation bump",
            exc_info=True,
        )
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(session_mod.SESSION_COOKIE_NAME, path="/")
    return response


@router.post("/login/guest")
async def login_guest(request: Request) -> Response:
    """Mint an unauthenticated 'guest' session — read-only, gated to
    aggregate-only views (no per-project filter, no per-session detail).
    Cookie invalidates on every server restart since the guest secret
    is regenerated."""
    token = session_mod.make_guest_session_token()
    response = RedirectResponse("/", status_code=303)
    session_mod.set_session_cookie(response, token)
    return response
