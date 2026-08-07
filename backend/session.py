"""Session token mint/verify + FastAPI auth middleware.

Looks up users in an external auth DB by `user_id`, fetching only the
`config` JSONB column (which carries the PBKDF2 web-password hash and
a per-user web-session secret).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from psycopg import Connection

from backend import db
from backend import sessions_repo


SESSION_COOKIE_NAME = "session"
# Cookie lifetime per issuance. auth_middleware re-issues the SAME cookie
# value with a fresh expiry on every authenticated non-guest request, so an
# active session's cookie never lapses; only an idle one hits this limit,
# 400 days after its last request. Guest cookies are excluded — unless
# GUEST_SESSION_SECRET is set they are signed with a process-local secret
# and cannot be honoured after a restart anyway. Sessions themselves do
# not expire on a clock — they end when revoked via the `web_sessions` row
# (see sessions_repo).
SESSION_COOKIE_MAX_AGE = 400 * 24 * 3600
WEB_SESSION_SECRET_KEY = "web_session_secret"

# Sentinel user_id reserved for unauthenticated guest sessions.
# Tokens with this user_id are signed with the GUEST_SESSION_SECRET env
# value, or with a process-local secret regenerated at startup when it is
# unset (in which case guest cookies invalidate on restart).
GUEST_USER_ID = 0
_GUEST_SECRET_FALLBACK = secrets.token_urlsafe(32)


def _guest_secret() -> str:
    """Guest-token signing secret.

    Set GUEST_SESSION_SECRET to keep guest sessions alive across restarts.
    Without it a per-process fallback is used and every restart signs out
    every guest, which is the pre-Phase-1 behaviour.

    The os.environ read is deliberately per-call, not hoisted into a
    module constant: caching it at import time would re-couple signing to
    process start (and break tests that set the variable after import).
    """
    return os.environ.get("GUEST_SESSION_SECRET", "").strip() or _GUEST_SECRET_FALLBACK


def parse_session_token(token: str):
    parts = token.split(".")
    if len(parts) != 4:
        return None
    raw_uid, raw_ts, nonce, sig = parts
    try:
        user_id = int(raw_uid)
        issued_at = int(raw_ts)
    except ValueError:
        return None
    if not nonce or not sig:
        return None
    return user_id, issued_at, nonce, sig


def make_session_token(user_id: int, secret: str) -> str:
    issued_at = int(time.time())
    nonce = secrets.token_urlsafe(10)
    payload = f"{user_id}.{issued_at}.{nonce}"
    sig = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: str, secret: str):
    parsed = parse_session_token(token)
    if parsed is None:
        return None
    user_id, issued_at, _nonce, sig = parsed
    now = int(time.time())
    if issued_at > now + 60:
        return None
    payload = f"{user_id}.{issued_at}.{parsed[2]}"
    expected = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    return user_id


def _stored_session_secret(config: dict) -> str:
    """The configured web-session secret, or "" when absent.

    A stored JSON null (or any non-string) is ABSENT, not a value: coercing
    it with str() would turn null into the literal string "None", which
    would then work as a signing secret. Fail closed instead.
    """
    value = config.get(WEB_SESSION_SECRET_KEY)
    return value.strip() if isinstance(value, str) else ""


def get_or_create_session_secret(config: dict) -> str:
    secret = _stored_session_secret(config)
    if secret:
        return secret
    secret = secrets.token_urlsafe(32)
    config[WEB_SESSION_SECRET_KEY] = secret
    return secret


def _query_user_config(c: Connection, user_id: int) -> dict | None:
    row = c.execute(
        "SELECT config FROM users WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    return row[0] if row else None


def load_user_config(user_id: int, conn: Connection | None = None) -> dict | None:
    """Fetch the auth DB's users table.config for one user. Returns None if no row.

    Pass an open conn to share one pool checkout with other lookups on
    the same request (the auth pool is small); omit it to take your own.
    """
    if conn is not None:
        return _query_user_config(conn, user_id)
    with db.auth_conn() as c:
        return _query_user_config(c, user_id)


def write_user_config(user_id: int, config: dict) -> None:
    """Persist a modified config back to the auth DB's users table.

    Called unconditionally on every successful login (see login.py). The
    web_session_secret itself is minted only on first login; the config
    write happens every time.
    """
    with db.auth_conn() as c:
        c.execute(
            "UPDATE users SET config = %s::jsonb WHERE user_id = %s",
            (json.dumps(config), user_id),
        )


def make_guest_session_token() -> str:
    return make_session_token(GUEST_USER_ID, _guest_secret())


def resolve_session_user_id(token: str) -> int | None:
    parsed = parse_session_token(token)
    if parsed is None:
        return None
    user_id = parsed[0]
    if user_id == GUEST_USER_ID:
        return verify_session_token(token, _guest_secret())
    # Config and nonce state resolve on ONE shared auth-DB connection —
    # the auth pool is max_size=4, and per-request checkouts must not
    # grow beyond the pre-plan single load_user_config acquisition plus
    # one (the touch below takes that one).
    with db.auth_conn() as conn:
        config = load_user_config(user_id, conn=conn)
        secret = _stored_session_secret(config) if config else ""
        if not secret:
            return None
        verified = verify_session_token(token, secret)
        if verified is None:
            return None
        if not sessions_repo.is_session_active(parsed[2], conn=conn):
            return None
    sessions_repo.touch_session(parsed[2])
    return verified


def check_origin(request: Request) -> bool:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return True
    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")
    host = request.headers.get("host", "")
    if not host:
        return True
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


def _session_cookie_domain() -> str | None:
    """The registrable domain the session cookie is shared across, or None.

    Single source for both cookie helpers — a browser keys a cookie on
    (name, domain, path), so setter and clearer must agree on the domain
    by construction, not by two copies of the same lookup. Read per call
    (like _guest_secret) so tests can set the variable after import.
    Unset → host-only cookie, the pre-sharing behaviour: a developer
    checkout is not served from the shared domain and must not try to
    set a cookie for it.
    """
    return os.environ.get("SESSION_COOKIE_DOMAIN") or None


def set_session_cookie(response, token: str, *, guest: bool = False) -> None:
    """The one place a session cookie is issued.

    Every issuing path must go through this so a new attribute cannot
    reach some responses and not others. Guest cookies are excluded from
    sharing (guest=True): guest secrets are per-service, so a shared
    guest cookie would be rejected by every service except the one that
    issued it and would shadow the host-only one.
    """
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        secure=os.environ.get("COOKIE_SECURE", "1") == "1",
        samesite="strict",
        max_age=SESSION_COOKIE_MAX_AGE,
        path="/",
        domain=None if guest else _session_cookie_domain(),
    )


def clear_session_cookie(response) -> None:
    """The one place the session cookie is cleared.

    A browser keys a cookie on (name, domain, path), so the deletion must
    agree with set_session_cookie on all three or it writes an expired
    cookie that does not match the live one and logout silently stops
    working. The domain comes from the same _session_cookie_domain()
    source as the setter.

    Both keyings are cleared. The host-only deletion is UNCONDITIONAL:
    with the variable unset it is the only deletion, and with it set it
    is the one that reaches a cookie issued BEFORE the rollout — that
    cookie is keyed (name, host-only, path), so a browser holding it
    keeps authenticating after a logout that deletes only the
    domain-keyed one.
    """
    domain = _session_cookie_domain()
    if domain is not None:
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            path="/",
            domain=domain,
        )
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
    )


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in _AUTH_PUBLIC_PATHS:
        return await call_next(request)
    if path.startswith("/admin/"):
        denied = _admin_denied(request)
    else:
        denied = _session_denied(request)
    if denied is not None:
        return denied
    response = await call_next(request)
    cookie = request.cookies.get(SESSION_COOKIE_NAME, "")
    # Slide only on the session-validated path: user_id is set exclusively
    # by _session_denied, so /admin/* (token-authed, is_guest never set)
    # never matches — and neither do guests, whose cookie (unless
    # GUEST_SESSION_SECRET is set) is signed with a process-local secret
    # the server cannot honour after a restart.
    if (
        cookie
        and getattr(request.state, "user_id", None) is not None
        and not getattr(request.state, "is_guest", False)
    ):
        set_session_cookie(response, cookie)
    return response
