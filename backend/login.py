"""/login GET + POST + /logout.

Login UI is inlined HTML (no shared layout chrome — claudit uses
its own visualizer dark theme). Every credential failure — unknown
id, no web password configured, wrong password — answers one generic
401 rendering the FULL sign-in page (issue #395), identical status and
body across the three shapes, and every failure costs about one PBKDF2
run at the write count: the real verification where it can run, a
dummy remainder run on top where it cannot or would run cheaper (a
legacy 200k hash, a malformed stored hash), so an account id cannot
be enumerated by response shape or timing (issue #109) — with one
documented residual: a stored hash versioned above the target count
still costs longer. Rate limiting: 5 failures per IP+user pair per
5-minute window (issue #111), so one user's failures never lock a
different user behind the same egress IP; entries are pruned per key
on access. Each history table and its in-flight reservation table is
bounded at 4,096 keys. Expired history keys are swept above the cap; at
capacity, expired keys are swept first and unseen keys are refused if
the table remains full. In-flight keys
reserve history capacity, and active histories are never evicted.
An aggregate limit also admits at most 20 failures per IP per 5-minute
window, so rotating user ids cannot evade the pair limit; it uses the
same 4,096-key history and reservation bounds and fail-closed policy.
"""
from __future__ import annotations

import html
import logging
import time

from fastapi import APIRouter, Form, Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from backend import auth, branding
from backend.login_workers import WorkerReservation, run_reserved_worker
from backend import session as session_mod


router = APIRouter()

log = logging.getLogger("claudit.login")

_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_INFLIGHT: dict[str, int] = {}
_LOGIN_MAX_FAILURES = 5
_LOGIN_WINDOW_SECONDS = 300
# Hard bound for tracked (ip, user) pairs. At capacity, expired keys are
# swept first and unseen keys are refused if the table remains full.
_LOGIN_MAX_KEYS = 4096

_LOGIN_IP_FAILURES: dict[str, list[float]] = {}
_LOGIN_IP_INFLIGHT: dict[str, int] = {}
# Above the per-pair cap so the pair limit remains binding for one id,
# while id rotation is limited to 20 failures from one IP per 5 minutes.
_LOGIN_MAX_IP_FAILURES = 20
# Hard bound for tracked client IPs, matching the pair limiter's
# prune-on-access, expired-key sweep, and fail-closed admission policy.
_LOGIN_MAX_IP_KEYS = 4096

# One answer for every credential failure — identical status and body
# for unknown id, no web password and wrong password (issue #109). The
# body is the FULL sign-in page (issue #395): the error announced in a
# role=alert element, the form still there to retry from, nothing
# user-derived echoed back — the byte-identical contract holds.
_GENERIC_FAILURE_TEXT = "Invalid credentials."
_GENERIC_LIMIT_TEXT = "Too many login attempts. Try again later."
_INVALID_UID_TEXT = "Invalid user ID"


def _login_page_response(err: str, status: int) -> HTMLResponse:
    """The sign-in page with `err` in its role=alert element.

    A credential failure or rate-limit answer used to be a bare
    text/plain line — no form to retry from, no page title, no lang
    (issue #395). Rendering the page keeps the status codes and every
    limiter semantic; only the body changes. `err` is a server-owned
    literal, never user input, so nothing from the request is echoed
    back — that is what keeps the generic bodies byte-identical (#109).
    """
    return HTMLResponse(
        _LOGIN_HTML.format(
            err=err,
            app_name=html.escape(branding.brand_name().upper(), quote=True),
            **_privacy_notice_slots(),
        ),
        status_code=status,
    )


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
    """Remove expired pair histories without evicting live windows."""
    expired = [
        key for key, attempts in _LOGIN_FAILURES.items()
        if not any(now - t < _LOGIN_WINDOW_SECONDS for t in attempts)
    ]
    for key in expired:
        del _LOGIN_FAILURES[key]


def _history_capacity_limited(
    key: str,
    histories: dict[str, list[float]],
    inflight: dict[str, int],
    max_keys: int,
) -> bool:
    """Refuse unseen keys when histories plus reserved slots fill."""
    if key in histories or key in inflight:
        return False
    if len(histories) + len(inflight) < max_keys:
        return False
    reserved_history_slots = sum(
        reserved_key not in histories for reserved_key in inflight
    )
    return len(histories) + reserved_history_slots >= max_keys


def _check_login_rate_limit(ip: str, uid: int) -> bool:
    now = time.time()
    key = _failure_key(ip, uid)
    attempts = _prune_key(key, now)
    if len(_LOGIN_FAILURES) >= _LOGIN_MAX_KEYS:
        _sweep_expired_keys(now)
    history_full = _history_capacity_limited(
        key, _LOGIN_FAILURES, _LOGIN_INFLIGHT, _LOGIN_MAX_KEYS
    )
    reservations_full = (
        len(_LOGIN_INFLIGHT) >= _LOGIN_MAX_KEYS
        and key not in _LOGIN_INFLIGHT
    )
    return (
        len(attempts) + _LOGIN_INFLIGHT.get(key, 0) >= _LOGIN_MAX_FAILURES
        or history_full
        or reservations_full
    )


def _record_login_failure(ip: str, uid: int) -> None:
    now = time.time()
    key = _failure_key(ip, uid)
    attempts = _prune_key(key, now)
    attempts.append(now)
    _LOGIN_FAILURES[key] = attempts
    if len(_LOGIN_FAILURES) > _LOGIN_MAX_KEYS:
        _sweep_expired_keys(now)


def _prune_ip_key(ip: str, now: float) -> list[float]:
    """Drop one IP's expired timestamps; delete the key once empty."""
    attempts = [
        t for t in _LOGIN_IP_FAILURES.get(ip, [])
        if now - t < _LOGIN_WINDOW_SECONDS
    ]
    if attempts:
        _LOGIN_IP_FAILURES[ip] = attempts
    else:
        _LOGIN_IP_FAILURES.pop(ip, None)
    return attempts


def _sweep_expired_ip_keys(now: float) -> None:
    """Remove expired IP histories without evicting live windows."""
    expired = [
        ip for ip, attempts in _LOGIN_IP_FAILURES.items()
        if not any(now - t < _LOGIN_WINDOW_SECONDS for t in attempts)
    ]
    for ip in expired:
        del _LOGIN_IP_FAILURES[ip]


def _check_login_ip_rate_limit(ip: str) -> bool:
    now = time.time()
    attempts = _prune_ip_key(ip, now)
    if len(_LOGIN_IP_FAILURES) >= _LOGIN_MAX_IP_KEYS:
        _sweep_expired_ip_keys(now)
    history_full = _history_capacity_limited(
        ip, _LOGIN_IP_FAILURES, _LOGIN_IP_INFLIGHT, _LOGIN_MAX_IP_KEYS
    )
    reservations_full = (
        len(_LOGIN_IP_INFLIGHT) >= _LOGIN_MAX_IP_KEYS
        and ip not in _LOGIN_IP_INFLIGHT
    )
    return (
        len(attempts) + _LOGIN_IP_INFLIGHT.get(ip, 0)
        >= _LOGIN_MAX_IP_FAILURES
        or history_full
        or reservations_full
    )


def _record_login_ip_failure(ip: str) -> None:
    now = time.time()
    attempts = _prune_ip_key(ip, now)
    attempts.append(now)
    _LOGIN_IP_FAILURES[ip] = attempts
    if len(_LOGIN_IP_FAILURES) > _LOGIN_MAX_IP_KEYS:
        _sweep_expired_ip_keys(now)


def _reserve_login_attempt(ip: str, uid: int) -> None:
    """Reserve both limiter slots before a login awaits verification."""
    key = _failure_key(ip, uid)
    _LOGIN_INFLIGHT[key] = _LOGIN_INFLIGHT.get(key, 0) + 1
    _LOGIN_IP_INFLIGHT[ip] = _LOGIN_IP_INFLIGHT.get(ip, 0) + 1


def _release_login_attempt(ip: str, uid: int) -> None:
    """Release both reservations when the request finishes."""
    key = _failure_key(ip, uid)
    pair_count = _LOGIN_INFLIGHT[key] - 1
    if pair_count:
        _LOGIN_INFLIGHT[key] = pair_count
    else:
        del _LOGIN_INFLIGHT[key]
    ip_count = _LOGIN_IP_INFLIGHT[ip] - 1
    if ip_count:
        _LOGIN_IP_INFLIGHT[ip] = ip_count
    else:
        del _LOGIN_IP_INFLIGHT[ip]


async def _normalize_login_failure(
    reservation: WorkerReservation, password: str, spent: int
) -> None:
    """Attempt timing normalization but keep worker errors generic too."""
    try:
        await run_reserved_worker(
            reservation, auth.normalize_verification_timing, password, spent
        )
    except Exception:
        log.warning(
            "login timing worker failed; returning generic credentials error",
            exc_info=True,
        )


async def _verification_failure_spent(
    reservation: WorkerReservation, config: dict, password: str
) -> int | None:
    """Return spent iterations on failure, or None when verification passed."""
    try:
        verified = await run_reserved_worker(
            reservation, auth.verify_web_password, config, password
        )
        if verified:
            return None
        return auth.stored_verification_iterations(config)
    except Exception:
        log.warning(
            "login verification worker failed; treating as invalid credentials",
            exc_info=True,
        )
        return 0


def _bind_verified_credentials(
    user_id: int, verified_config: dict
) -> str | None:
    """Fresh-check, bind and cache credentials without an await between them."""
    fresh_config = session_mod.load_user_config(user_id)
    verified_fp = session_mod.credential_fingerprint(verified_config)
    if (
        fresh_config is None
        or session_mod.credential_fingerprint(fresh_config) != verified_fp
    ):
        return None
    secret, generation = session_mod.get_or_create_session_row(
        user_id, verified_fp
    )
    session_mod.remember_user_config(user_id, fresh_config)
    return session_mod.make_session_token(
        user_id, secret, generation=generation
    )


def reset_login_rate_limits() -> None:
    """Clear the process-global failure dict. Tests need this between
    cases that POST from the same TestClient host; production never
    calls it."""
    _LOGIN_FAILURES.clear()
    _LOGIN_IP_FAILURES.clear()
    _LOGIN_INFLIGHT.clear()
    _LOGIN_IP_INFLIGHT.clear()


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
  /* app.css's --muted (#7a81a8): 4.70:1 over the form card #14181d,
     above the 4.5:1 AA text floor — #556 computed 2.44:1 (issue #395). */
  .or {{ text-align:center; color:#7a81a8; font-size:11px; margin:16px 0 4px; letter-spacing:.2em; }}
  .err {{ color:#e76; font-size:12px; min-height:16px; margin-top:8px; }}
{privacy_css}</style>
</head><body>
<form method="post" action="/login">
  <h1>{app_name} · sign in</h1>
  <label for="login-user">Username</label>
  <input id="login-user" name="user_id" required inputmode="numeric" pattern="[0-9]+"
         autocomplete="username">
  <label for="login-password">Password</label>
  <input id="login-password" name="password" type="password" required
         autocomplete="current-password">
  <button type="submit">Sign in</button>
  <div class="err" role="alert">{err}</div>
  <div class="or">or</div>
  <button type="submit" class="guest-btn"
          formaction="/login/guest" formmethod="post" formnovalidate>
    Continue as guest
  </button>
{privacy_link}</form>
</body></html>
"""


def _privacy_notice_slots() -> dict[str, str]:
    """The two _LOGIN_HTML slots for the operator's privacy notice
    (issue #270). Empty unless APP_PRIVACY_NOTICE_URL is set, so the
    unset page stays byte-identical to before — no anchor, no empty
    href, and no styling either. The URL goes through
    ``branding.url_attr`` (SV-BRAND-ESCAPE): attribute-escaping alone
    cannot stop a ``javascript:`` scheme, so a value outside the
    allow-list drops the link and logs a warning (#365)."""
    url = branding.privacy_notice_url()
    if not url:
        return {"privacy_link": "", "privacy_css": ""}
    href = branding.url_attr(url)
    if href is None:
        return {"privacy_link": "", "privacy_css": ""}
    css = (
        "  .privacy-link { display:block; text-align:center;"
        " margin-top:12px; color:#8aa; font-size:12px; }\n"
    )
    link = f'<a class="privacy-link" href="{href}">Privacy notice</a>\n'
    return {"privacy_link": link, "privacy_css": css}


@router.get("/login")
async def login_page(request: Request) -> HTMLResponse:
    # The upper-cased APP_NAME is the page's brand word; html-escaped —
    # every slot is a real HTML context. Default = today's CLAUDIT.
    return _login_page_response("", 200)


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
        # answers before the limiter with its own fixed text, on the
        # full sign-in page like every other failure (#427).
        return _login_page_response(_INVALID_UID_TEXT, 400)
    pair_limited = _check_login_rate_limit(ip, uid)
    ip_limited = _check_login_ip_rate_limit(ip)
    if pair_limited or ip_limited:
        return _login_page_response(_GENERIC_LIMIT_TEXT, 429)
    # No await may intervene between both checks and reservation: this
    # event-loop turn atomically accounts for the admitted request.
    _reserve_login_attempt(ip, uid)
    reservation = WorkerReservation(lambda: _release_login_attempt(ip, uid))
    try:
        config = session_mod.load_user_config(uid)
        if not config or not auth.has_web_password(config):
            # The real verification cannot run: normalize from zero — burn
            # the CPU the real verification would cost — and give the same
            # generic answer a wrong password gets, so neither response
            # shape nor timing separates the two (#109).
            await _normalize_login_failure(reservation, password, 0)
            _record_login_failure(ip, uid)
            _record_login_ip_failure(ip)
            return _login_page_response(_GENERIC_FAILURE_TEXT, 401)
        failure_spent = await _verification_failure_spent(
            reservation, config, password
        )
        if failure_spent is not None:
            # Top up whatever the real verification spent (its own count
            # for a versioned hash, the legacy count for valid bare hex,
            # zero for malformed versioned or corrupt legacy material
            # that ran no PBKDF2 at all) so a failure costs ≈ the target
            # whatever shape the stored hash is (#109).
            await _normalize_login_failure(
                reservation, password, failure_spent
            )
            _record_login_failure(ip, uid)
            _record_login_ip_failure(ip)
            return _login_page_response(_GENERIC_FAILURE_TEXT, 401)

        # Verification used the config captured before its worker-thread
        # await. The helper performs the fresh read, comparison, bind and
        # cache update synchronously so another login cannot interleave.
        token = _bind_verified_credentials(uid, config)
        if token is None:
            # The credential changed while verification ran. Treat the
            # stale proof as a wrong password: it already spent the real
            # verification count, so normalize from that count and record
            # the same pair and IP failures without binding or caching it.
            await _normalize_login_failure(
                reservation,
                password,
                auth.stored_verification_iterations(config),
            )
            _record_login_failure(ip, uid)
            _record_login_ip_failure(ip)
            return _login_page_response(_GENERIC_FAILURE_TEXT, 401)
        response = RedirectResponse("/", status_code=303)
        session_mod.set_session_cookie(response, token)
        return response
    finally:
        reservation.close()


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
