import contextlib
import hashlib
import hmac
import logging
import time
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import db
from backend import session
from backend import sessions_repo


def test_token_roundtrip():
    secret = "super-secret-32-bytes" * 2
    tok = session.make_session_token(99, secret)
    assert session.verify_session_token(tok, secret) == 99


def test_verify_rejects_wrong_secret():
    tok = session.make_session_token(42, "secret-a" * 4)
    assert session.verify_session_token(tok, "secret-b" * 4) is None


def test_verify_accepts_old_token():
    # Sessions no longer expire on a clock — only revocation ends them.
    # A token minted long in the past must still verify.
    secret = "k" * 32
    tok = session.make_session_token(7, secret)
    far_future = int(time.time()) + 365 * 24 * 3600
    with patch.object(session.time, "time", return_value=far_future):
        assert session.verify_session_token(tok, secret) == 7


def test_verify_rejects_future_token():
    secret = "k" * 32
    payload = "5.99999999999.nonce"
    sig = hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    tok = f"{payload}.{sig}"
    assert session.verify_session_token(tok, secret) is None


def test_parse_session_token_rejects_garbage():
    assert session.parse_session_token("not.a.real.token.too.many") is None
    assert session.parse_session_token("missing-dots") is None
    assert session.parse_session_token("a.b.c.d") is None


def test_get_or_create_session_secret_persists():
    config: dict = {}
    s1 = session.get_or_create_session_secret(config)
    assert config[session.WEB_SESSION_SECRET_KEY] == s1
    s2 = session.get_or_create_session_secret(config)
    assert s2 == s1


def test_check_origin_allows_safe_methods():
    scope = {
        "type": "http", "method": "GET", "headers": [],
        "path": "/api/projects",
    }
    req = Request(scope)
    assert session.check_origin(req)


def test_check_origin_rejects_cross_origin_post():
    scope = {
        "type": "http", "method": "POST",
        "headers": [
            (b"host", b"viz.example.com"),
            (b"origin", b"https://evil.example.com"),
        ],
        "path": "/admin/ingest",
    }
    req = Request(scope)
    assert not session.check_origin(req)


def test_check_origin_accepts_same_origin_post():
    scope = {
        "type": "http", "method": "POST",
        "headers": [
            (b"host", b"viz.example.com"),
            (b"origin", b"https://viz.example.com"),
        ],
        "path": "/admin/ingest",
    }
    req = Request(scope)
    assert session.check_origin(req)


def test_guest_blocked_from_export():
    app = FastAPI()
    app.middleware("http")(session.auth_middleware)

    @app.get("/api/export")
    async def _stub():
        return {"ok": True}

    client = TestClient(app)
    guest_cookie = session.make_guest_session_token()
    client.cookies.set(session.SESSION_COOKIE_NAME, guest_cookie)
    resp = client.get("/api/export?range=7d")
    assert resp.status_code == 403


def test_revoked_session_stops_resolving(auth_db, seeded_user):
    uid, secret = seeded_user
    token = session.make_session_token(uid, secret)
    parsed = session.parse_session_token(token)
    assert parsed is not None
    nonce = parsed[2]
    sessions_repo.record_session(uid, nonce, "curl", "127.0.0.1")
    assert session.resolve_session_user_id(token) == uid
    sessions_repo.revoke_session(uid, nonce)
    assert session.resolve_session_user_id(token) is None


def test_signature_valid_but_unrecorded_nonce_is_rejected(auth_db, seeded_user):
    uid, secret = seeded_user
    token = session.make_session_token(uid, secret)
    assert session.resolve_session_user_id(token) is None


def test_guest_session_has_no_web_session_row(guest_client, monkeypatch):
    cookie = guest_client.cookies.get(session.SESSION_COOKIE_NAME)
    parsed = session.parse_session_token(cookie)
    assert parsed is not None
    assert parsed[0] == session.GUEST_USER_ID
    # Guests are unrecorded BY DESIGN — resolve_session_user_id must bypass
    # the nonce lookup for them, or every guest would be logged out.
    assert sessions_repo.is_session_active(parsed[2]) is False
    assert session.resolve_session_user_id(cookie) == session.GUEST_USER_ID

    # The guest branch returns before ANY auth-DB access, so guests must
    # survive a total auth-DB outage.
    def _down():
        raise RuntimeError("auth DB down")

    monkeypatch.setattr(db, "auth_conn", _down)
    assert session.resolve_session_user_id(cookie) == session.GUEST_USER_ID


class _FailOnUpdate:
    """Connection wrapper whose UPDATEs raise — simulates a read-only
    replica after failover. SELECTs pass through to the real connection."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        if str(sql).lstrip().upper().startswith("UPDATE"):
            raise RuntimeError("simulated read-only replica")
        return self._conn.execute(sql, params)


def test_touch_failure_still_resolves(auth_db, seeded_user, monkeypatch, caplog):
    """A failing touch_session (write-side bookkeeping) must never deny a
    request whose session was already proven valid — warn and resolve."""
    uid, secret = seeded_user
    token = session.make_session_token(uid, secret)
    parsed = session.parse_session_token(token)
    assert parsed is not None
    nonce = parsed[2]
    sessions_repo.record_session(uid, nonce, "curl", "127.0.0.1")

    real_auth_conn = db.auth_conn

    @contextlib.contextmanager
    def flaky_auth_conn():
        with real_auth_conn() as conn:
            yield _FailOnUpdate(conn)

    monkeypatch.setattr(db, "auth_conn", flaky_auth_conn)
    with caplog.at_level(logging.WARNING, logger="backend.sessions_repo"):
        assert session.resolve_session_user_id(token) == uid
    # Swallowed, but not silently.
    assert "touch_session" in caplog.text


def test_resolve_uses_at_most_two_auth_connections(
    auth_db, seeded_user, monkeypatch
):
    """Config + nonce state resolve on ONE shared auth-DB connection; the
    throttled touch takes one more. The plan allows at most one acquisition
    beyond the pre-plan single load_user_config checkout."""
    uid, secret = seeded_user
    token = session.make_session_token(uid, secret)
    parsed = session.parse_session_token(token)
    assert parsed is not None
    nonce = parsed[2]
    sessions_repo.record_session(uid, nonce, "curl", "127.0.0.1")

    real_auth_conn = db.auth_conn
    acquisitions = 0

    @contextlib.contextmanager
    def counting_auth_conn():
        nonlocal acquisitions
        acquisitions += 1
        with real_auth_conn() as conn:
            yield conn

    monkeypatch.setattr(db, "auth_conn", counting_auth_conn)
    assert session.resolve_session_user_id(token) == uid
    assert acquisitions <= 2


def test_token_from_a_year_ago_still_verifies(monkeypatch):
    secret = "super-secret-32-bytes" * 2
    real_time = session.time.time
    monkeypatch.setattr(session.time, "time", lambda: real_time() - 365 * 24 * 3600)
    old_token = session.make_session_token(42, secret)
    monkeypatch.setattr(session.time, "time", real_time)
    assert session.verify_session_token(old_token, secret) == 42


def test_future_dated_token_is_still_rejected():
    secret = "super-secret-32-bytes" * 2
    real_time = session.time.time
    session.time.time = lambda: real_time() + 3600
    try:
        tok = session.make_session_token(42, secret)
    finally:
        session.time.time = real_time
    assert session.verify_session_token(tok, secret) is None
