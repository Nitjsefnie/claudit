"""Session token mint/verify + FastAPI auth middleware.

Password hashes are looked up in the external auth DB by `user_id`
(that DB is READ-ONLY from this application). Each real user's
web-session secret and a per-user generation counter live in claudit's
OWN database, in the `user_session` table (backend/schema.sql) — issue
#94 moved them out of the shared users table so claudit never writes to
the auth DB. Guest tokens are signed with a process-local secret and
have no row anywhere; they die on restart.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from backend import db


SESSION_COOKIE_NAME = "session"
SESSION_COOKIE_MAX_AGE = 7 * 24 * 3600

# Sentinel user_id reserved for unauthenticated guest sessions.
# Tokens with this user_id are signed with a process-local secret
# regenerated at startup, so guest cookies invalidate on restart.
GUEST_USER_ID = 0
_GUEST_SECRET = secrets.token_urlsafe(32)


def set_session_cookie(response: Response, token: str) -> None:
    """Issue a session cookie with the application's complete flag contract."""
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        secure=os.environ.get("COOKIE_SECURE", "1") == "1",
        samesite="strict",
        max_age=SESSION_COOKIE_MAX_AGE,
        path="/",
    )


def parse_session_token(token: str):
    """Split a token into (user_id, issued_at, nonce, sig, generation).

    Five parts — the pre-generation 4-part shape (obsolete since
    #108) is rejected — or None for anything malformed.
    """
    parts = token.split(".")
    if len(parts) != 5:
        return None
    raw_uid, raw_ts, nonce, raw_gen, sig = parts
    try:
        user_id = int(raw_uid)
        issued_at = int(raw_ts)
        generation = int(raw_gen)
    except ValueError:
        return None
    if not nonce or not sig:
        return None
    return user_id, issued_at, nonce, sig, generation


def make_session_token(user_id: int, secret: str, generation: int = 0) -> str:
    """Sign f"{uid}.{issued_at}.{nonce}.{generation}" and return the
    five-part token."""
    issued_at = int(time.time())
    nonce = secrets.token_urlsafe(10)
    payload = f"{user_id}.{issued_at}.{nonce}.{generation}"
    sig = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(
    token: str, secret: str, generation: int | None = None
):
    """Return the user_id when the signature and age check out; when
    `generation` is not None, also that the token was minted at that
    (current) generation. None skips the check — the guest path, whose
    tokens answer to the process-local secret alone."""
    parsed = parse_session_token(token)
    if parsed is None:
        return None
    user_id, issued_at, nonce, sig, token_generation = parsed
    now = int(time.time())
    if issued_at > now + 60:
        return None
    if now - issued_at > SESSION_COOKIE_MAX_AGE:
        return None
    payload = f"{user_id}.{issued_at}.{nonce}.{token_generation}"
    expected = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    if generation is not None and token_generation != generation:
        return None
    return user_id


def get_or_create_session_row(user_id: int) -> tuple[str, int]:
    """Return (secret, generation) for a real user, inserting a fresh
    secret at generation 0 on the user's first login.

    The row lives in claudit's own DB (user_session, backend/schema.sql)
    — never in the shared auth DB (issue #94). Race-safe for two
    concurrent first logins: INSERT ... ON CONFLICT DO NOTHING decides
    the winner and the SELECT reads it back, both in one transaction, so
    whichever login loses the race leaves with the winner's secret.
    """
    with db.viz_conn() as c:
        row = c.execute(
            "INSERT INTO user_session (user_id, secret) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO NOTHING "
            "RETURNING secret, generation",
            (user_id, secrets.token_urlsafe(32)),
        ).fetchone()
        if row is None:
            row = c.execute(
                "SELECT secret, generation FROM user_session "
                "WHERE user_id = %s",
                (user_id,),
            ).fetchone()
        c.commit()
    if row is None:
        # The INSERT above either created the row or conflicted with one
        # this transaction can already see; a None here has no path.
        raise RuntimeError(f"user_session row for {user_id} vanished")
    return str(row[0]), int(row[1])


def load_session_row(user_id: int) -> tuple[str, int] | None:
    """(secret, generation) for a real user from claudit's own DB; None
    when the user has never logged in."""
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT secret, generation FROM user_session WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return str(row[0]), int(row[1])


def bump_session_generation(user_id: int) -> None:
    """Invalidate every session token a real user holds (issue #108):
    verification requires the token's generation to equal the stored one,
    and this raises the stored one by one."""
    with db.viz_conn() as c:
        c.execute(
            "UPDATE user_session SET generation = generation + 1 "
            "WHERE user_id = %s",
            (user_id,),
        )
        c.commit()


def load_user_config(user_id: int) -> dict | None:
    """Fetch the auth DB's users table.config for one user. Returns None
    if no row. The one thing claudit still reads from the auth DB."""
    with db.auth_conn() as c:
        row = c.execute(
            "SELECT config FROM users WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    return row[0] if row else None


def make_guest_session_token() -> str:
    return make_session_token(GUEST_USER_ID, _GUEST_SECRET)


def resolve_session_user_id(token: str) -> int | None:
    parsed = parse_session_token(token)
    if parsed is None:
        return None
    user_id = parsed[0]
    if user_id == GUEST_USER_ID:
        # Guests have no user_session row, so there is no generation to
        # compare — the process-local secret is what kills the cookie.
        return verify_session_token(token, _GUEST_SECRET)
    row = load_session_row(user_id)
    if row is None:
        return None
    secret, generation = row
    return verify_session_token(token, secret, generation)


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def check_origin(request: Request) -> bool:
    if request.method in _SAFE_METHODS:
        return True
    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")
    host = request.headers.get("host", "")
    if not host:
        return False
    if origin:
        return urlparse(origin).netloc == host
    if referer:
        return urlparse(referer).netloc == host
    return False


_AUTH_PUBLIC_PATHS = {"/health", "/login", "/logout", "/login/guest"}


def _admin_denied(request: Request) -> Response | None:
    """Admin-token + origin gate for /admin/*; None means allowed."""
    token = request.headers.get("x-admin-token", "")
    expected = os.environ.get("ADMIN_TOKEN", "")
    if not expected or not hmac.compare_digest(token, expected):
        return JSONResponse(
            {"ok": False, "error": "Unauthorized"}, status_code=401
        )
    if not check_origin(request):
        return Response("Forbidden (cross-origin)", status_code=403)
    return None


def _unauthenticated(path: str) -> Response:
    """APIs get a JSON 401; page loads get bounced to /login."""
    if path.startswith("/api/"):
        return JSONResponse(
            {"ok": False, "error": "Unauthorized"}, status_code=401
        )
    return RedirectResponse("/login", status_code=302)


def _guest_denied(request: Request) -> Response | None:
    """403s for the endpoints/filters a guest session must not use."""
    path = request.url.path
    # /api/projects leaks project names (filesystem paths).
    # /api/sessions* covers list, single detail, raw transcript, sidecar.
    # Context-growth-by-session is allowed — just numbers, needed for graphs.
    if (
        path == "/api/projects"
        or path.startswith("/api/sessions")
        or path.startswith("/api/export")
    ):
        return JSONResponse(
            {"ok": False, "error": "Forbidden (guest)"}, status_code=403
        )
    # Block project= filtering on aggregate endpoints — guest sees
    # project-mixed data only.
    if request.query_params.get("project"):
        return JSONResponse(
            {"ok": False, "error": "Forbidden (guest cannot filter by project)"},
            status_code=403,
        )
    return None


def _session_denied(request: Request) -> Response | None:
    """Origin + session-cookie + guest gate; None means allowed."""
    if not check_origin(request):
        return Response("Forbidden (cross-origin)", status_code=403)
    cookie = request.cookies.get(SESSION_COOKIE_NAME, "")
    user_id = resolve_session_user_id(cookie) if cookie else None
    if user_id is None:
        return _unauthenticated(request.url.path)
    request.state.user_id = user_id
    request.state.is_guest = user_id == GUEST_USER_ID
    # Gate per-session and per-project endpoints from guests, plus
    # disallow `project=` filters on aggregate endpoints so guests can
    # only see project-mixed data.
    if request.state.is_guest:
        return _guest_denied(request)
    return None


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in _AUTH_PUBLIC_PATHS:
        # Public paths still enforce same-origin on mutating methods, so a
        # cross-origin POST cannot reach /login or /login/guest either.
        if request.method not in _SAFE_METHODS and not check_origin(request):
            return Response("Forbidden (cross-origin)", status_code=403)
        return await call_next(request)
    if path.startswith("/admin/"):
        denied = _admin_denied(request)
    else:
        denied = _session_denied(request)
    if denied is not None:
        return denied
    return await call_next(request)
