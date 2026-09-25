"""Session token/store tests.

Token shape (issue #108): five dot-separated parts, uid, issued-at,
nonce, generation, signature — the signature covers the four payload
parts. Each real user's secret and generation live in claudit's own
user_session table; a token verifies only while its generation is still
current, which is what server-side logout bumps.
"""
import hashlib
import hmac
import time
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from backend import db, session
from tests import scratch_db


def test_token_roundtrip():
    secret = "super-secret-32-bytes" * 2
    tok = session.make_session_token(99, secret)
    assert session.verify_session_token(tok, secret) == 99


def test_token_roundtrip_carries_generation():
    secret = "super-secret-32-bytes" * 2
    tok = session.make_session_token(9, secret, generation=3)
    parsed = session.parse_session_token(tok)
    assert parsed is not None
    user_id, _issued_at, _nonce, _sig, generation = parsed
    assert (user_id, generation) == (9, 3)
    assert session.verify_session_token(tok, secret, generation=3) == 9


def test_verify_rejects_wrong_generation():
    secret = "k" * 32
    tok = session.make_session_token(7, secret, generation=0)
    assert session.verify_session_token(tok, secret, generation=1) is None


def test_old_four_part_token_is_rejected():
    """The pre-generation 4-part shape (obsolete since #108) must not
    parse, and so must not verify, even against the right secret."""
    secret = "k" * 4
    payload = "7.1234567890.nonce"
    sig = hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    tok = f"{payload}.{sig}"
    assert session.parse_session_token(tok) is None
    assert session.verify_session_token(tok, secret) is None


def test_verify_rejects_wrong_secret():
    tok = session.make_session_token(42, "secret-a" * 4)
    assert session.verify_session_token(tok, "secret-b" * 4) is None


def test_verify_rejects_expired_token():
    secret = "k" * 32
    tok = session.make_session_token(7, secret)
    far_future = int(time.time()) + session.SESSION_COOKIE_MAX_AGE + 60
    with patch.object(session.time, "time", return_value=far_future):
        assert session.verify_session_token(tok, secret) is None


def test_verify_rejects_future_token():
    secret = "k" * 32
    payload = "5.99999999999.nonce.0"
    sig = hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    tok = f"{payload}.{sig}"
    assert session.verify_session_token(tok, secret) is None


def test_parse_session_token_rejects_garbage():
    assert session.parse_session_token("not.a.real.token.too.many") is None
    assert session.parse_session_token("missing-dots") is None
    assert session.parse_session_token("a.b.c.d") is None
    assert session.parse_session_token("1.2.nonce.notanint.sig") is None


# --------------------------------------------------------------- the store

@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    """A fresh claudit DB with the startup schema applied — the real
    user_session table the store functions run against."""
    yield from scratch_db.scratch_viz_database(monkeypatch, "session")


def test_session_row_first_call_inserts(fresh_db):
    secret, generation = session.get_or_create_session_row(12345)
    assert generation == 0
    assert secret
    assert session.load_session_row(12345) == (secret, 0)


def test_session_row_second_call_returns_same_row(fresh_db):
    first = session.get_or_create_session_row(12345)
    assert session.get_or_create_session_row(12345) == first


def test_session_row_conflict_keeps_the_existing_secret(fresh_db):
    """The losing half of a concurrent first login must keep the
    winner's row, not overwrite it: INSERT ... ON CONFLICT DO NOTHING
    then SELECT, in one transaction."""
    with db.viz_conn() as c:
        c.execute(
            "INSERT INTO user_session (user_id, secret) VALUES (%s, %s)",
            (555, "winner-secret"),
        )
        c.commit()
    assert session.get_or_create_session_row(555) == ("winner-secret", 0)


def test_resolve_rejects_real_user_without_a_session_row(fresh_db):
    tok = session.make_session_token(314, "some-secret", generation=0)
    assert session.resolve_session_user_id(tok) is None


def test_bump_invalidates_the_token_and_relogin_mints_a_new_one(fresh_db):
    secret, generation = session.get_or_create_session_row(77)
    tok = session.make_session_token(77, secret, generation=generation)
    assert session.resolve_session_user_id(tok) == 77

    session.bump_session_generation(77)

    assert session.resolve_session_user_id(tok) is None
    # A re-login after the bump keeps the secret and returns the new
    # generation, so the freshly minted token verifies again.
    secret2, generation2 = session.get_or_create_session_row(77)
    assert (secret2, generation2) == (secret, generation + 1)
    tok2 = session.make_session_token(77, secret2, generation=generation2)
    assert session.resolve_session_user_id(tok2) == 77


def test_bump_of_user_without_row_is_a_noop(fresh_db):
    session.bump_session_generation(404)
    assert session.load_session_row(404) is None


def test_guest_token_ignores_generation():
    """Guests have no user_session row: the guest verifier ignores the
    generation entirely (guests keep dying on restart via the
    process-local secret), so a guest token minted at ANY generation
    still resolves."""
    tok = session.make_session_token(
        session.GUEST_USER_ID,
        session._GUEST_SECRET,  # pylint: disable=protected-access
        generation=7,
    )
    assert session.resolve_session_user_id(tok) == session.GUEST_USER_ID


# ------------------------------------------------------------ the cookie

def test_set_session_cookie_owns_complete_flag_contract(monkeypatch):
    """One helper owns every issuance flag, including absent Domain."""
    monkeypatch.setenv("COOKIE_SECURE", "1")
    response = Response()

    session.set_session_cookie(response, "token")

    header = response.headers["set-cookie"].lower()
    assert "session=token" in header
    assert "httponly" in header
    assert "secure" in header
    assert "samesite=strict" in header
    assert f"max-age={session.SESSION_COOKIE_MAX_AGE}" in header
    assert "path=/" in header
    assert "domain=" not in header


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


def test_check_origin_rejects_missing_host_header():
    """A POST with no Host header is malformed, never same-origin."""
    scope = {
        "type": "http", "method": "POST", "headers": [],
        "path": "/login",
    }
    req = Request(scope)
    assert not session.check_origin(req)


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
