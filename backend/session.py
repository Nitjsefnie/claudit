"""Session token mint/verify + FastAPI auth middleware.

Password hashes are looked up in the external auth DB by `user_id`
(that DB is READ-ONLY from this application). Each real user's
web-session secret and a per-user generation counter live in claudit's
OWN database, in the `user_session` table (backend/schema.sql) — issue
#94 moved them out of the shared users table so claudit never writes to
the auth DB. The row also stores a fingerprint of the auth-DB password
hash and salt. Resolution checks the current auth-DB credential through
a 60-second cache, so deleted users and changed credentials are revoked
within that window; rows from before the fingerprint migration are
invalid until login rebinds them. Guest tokens are signed with a
process-local secret and have no row anywhere; they die on restart.
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

_USER_CONFIG_CACHE: dict[int, tuple[float, dict | None]] = {}
_USER_CONFIG_CACHE_TTL_SECONDS = 60
_USER_CONFIG_CACHE_MAX_KEYS = 1024


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
    if not hmac.compare_digest(
        expected.encode("utf-8"), sig.encode("utf-8")
    ):
        return None
    if generation is not None and token_generation != generation:
        return None
    return user_id


def get_or_create_session_row(
    user_id: int, cred_fp: str
) -> tuple[str, int]:
    """Return (secret, generation) for a real user, inserting a fresh
    secret at generation 0 on the user's first login. Re-login with the
    same fingerprint preserves the secret and generation; a changed or
    previously NULL fingerprint rotates the secret and bumps generation.

    The row lives in claudit's own DB (user_session, backend/schema.sql)
    — never in the shared auth DB (issue #94).
    """
    with db.viz_conn() as c:
        row = c.execute(
            "INSERT INTO user_session (user_id, secret, cred_fp) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE "
            "SET secret = CASE "
            "WHEN user_session.cred_fp IS DISTINCT FROM EXCLUDED.cred_fp "
            "THEN EXCLUDED.secret ELSE user_session.secret END, "
            "generation = CASE "
            "WHEN user_session.cred_fp IS DISTINCT FROM EXCLUDED.cred_fp "
            "THEN user_session.generation + 1 "
            "ELSE user_session.generation END, "
            "cred_fp = EXCLUDED.cred_fp "
            "RETURNING secret, generation",
            (user_id, secrets.token_urlsafe(32), cred_fp),
        ).fetchone()
        c.commit()
    if row is None:
        raise RuntimeError(f"user_session row for {user_id} was not returned")
    return str(row[0]), int(row[1])


def load_session_row(
    user_id: int,
) -> tuple[str, int, str | None] | None:
    """(secret, generation, credential fingerprint) from claudit's DB;
    None when the user has never logged in."""
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT secret, generation, cred_fp FROM user_session "
            "WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    cred_fp = str(row[2]) if row[2] is not None else None
    return str(row[0]), int(row[1]), cred_fp


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


# Key for the credential fingerprint's HMAC — a fixed domain separator,
# not a secret: the fingerprint is a change-detection tag over material
# that is already a PBKDF2 output (issue #237).
_FP_KEY = b"claudit credential fingerprint v1"


def credential_fingerprint(config: dict) -> str:
    """HMAC-SHA256 tag binding a user_session row to the stored
    credential (its stored hash string and salt). Keyed because it is a
    MAC over already-hashed material for change detection — it never
    sees a password and must not be read as password hashing."""
    stored_hash = config.get("web_password_hash", "")
    stored_salt = config.get("web_password_salt", "")
    material = f"{stored_hash}\x00{stored_salt}".encode("utf-8")
    return hmac.new(_FP_KEY, material, hashlib.sha256).hexdigest()


def _prune_user_config_cache(now: float) -> None:
    """Drop expired entries above the cache cap, then evict oldest if
    active entries alone still exceed it."""
    if len(_USER_CONFIG_CACHE) <= _USER_CONFIG_CACHE_MAX_KEYS:
        return
    expired = [
        user_id for user_id, (expires_at, _config) in _USER_CONFIG_CACHE.items()
        if expires_at <= now
    ]
    for user_id in expired:
        del _USER_CONFIG_CACHE[user_id]
    while len(_USER_CONFIG_CACHE) > _USER_CONFIG_CACHE_MAX_KEYS:
        oldest = min(
            _USER_CONFIG_CACHE,
            key=lambda user_id: _USER_CONFIG_CACHE[user_id][0],
        )
        del _USER_CONFIG_CACHE[oldest]


def remember_user_config(user_id: int, config: dict | None) -> None:
    """Cache a freshly loaded auth config for the current login binding."""
    now = time.time()
    cached_config = dict(config) if config is not None else None
    _USER_CONFIG_CACHE[user_id] = (
        now + _USER_CONFIG_CACHE_TTL_SECONDS, cached_config
    )
    _prune_user_config_cache(now)


def _cached_user_config(user_id: int) -> dict | None:
    now = time.time()
    cached = _USER_CONFIG_CACHE.get(user_id)
    if cached is not None:
        expires_at, config = cached
        if expires_at > now:
            return config
        del _USER_CONFIG_CACHE[user_id]
    config = load_user_config(user_id)
    remember_user_config(user_id, config)
    return config


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
    secret, generation, cred_fp = row
    verified_user_id = verify_session_token(token, secret, generation)
    if verified_user_id is None or cred_fp is None:
        return None
    config = _cached_user_config(user_id)
    if config is None or credential_fingerprint(config) != cred_fp:
        return None
    return verified_user_id


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def check_origin(request: Request) -> bool:
    if request.method in _SAFE_METHODS:
        return True
    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")
    host = request.headers.get("host", "")
    if not host:
        return False
    try:
        if origin:
            return urlparse(origin).netloc == host
        if referer:
            return urlparse(referer).netloc == host
    except ValueError:
        return False
    return False


_AUTH_PUBLIC_PATHS = {"/health", "/login", "/logout", "/login/guest"}


def _admin_denied(request: Request) -> Response | None:
    """Admin-token + origin gate for /admin/*; None means allowed."""
    token = request.headers.get("x-admin-token", "")
    expected = os.environ.get("ADMIN_TOKEN", "")
    if not expected or not hmac.compare_digest(
        token.encode("utf-8"), expected.encode("utf-8")
    ):
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
