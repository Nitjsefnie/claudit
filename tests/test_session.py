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

_TEST_CONFIG = {
    "web_password_hash": "stored-hash",
    "web_password_salt": "stored-salt",
}
_TEST_CREDENTIAL_FP = (
    "33875a22ef8572036d179f52c5464c6941cc1cd4c8779dcd762f27092ca8ca4a"
)


def _clear_user_config_cache():
    session._USER_CONFIG_CACHE.clear()  # pylint: disable=protected-access


def test_token_roundtrip():
    secret = "super-secret-32-bytes" * 2
    tok = session.make_session_token(99, secret)
    assert session.verify_session_token(tok, secret) == 99


def test_token_roundtrip_survives_an_18_digit_user_id():
    """The token payload carries the user id as a decimal string and the
    signature covers it whole — an id above 2^31 must round-trip with no
    32-bit clamp anywhere on the token path (issue #185)."""
    secret = "super-secret-32-bytes" * 2
    tok = session.make_session_token(123456789012345678, secret)
    assert session.verify_session_token(tok, secret) == 123456789012345678


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


def test_non_ascii_session_signature_is_generic_unauthorized(monkeypatch):
    rows = {41: ("known-secret", 0, _TEST_CREDENTIAL_FP)}
    monkeypatch.setattr(session, "load_session_row", rows.get)
    app = FastAPI()
    app.middleware("http")(session.auth_middleware)

    @app.get("/api/me")
    async def _me():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    statuses = []
    bodies = []
    for uid in (41, 42):
        token = f"{uid}.{int(time.time())}.nonce.0.é"
        response = client.get(
            "/api/me",
            headers=[(b"cookie", f"session={token}".encode("latin-1"))],
        )
        statuses.append(response.status_code)
        bodies.append(response.json())

    assert statuses == [401, 401]
    assert bodies == [
        {"ok": False, "error": "Unauthorized"},
        {"ok": False, "error": "Unauthorized"},
    ]


def test_non_ascii_admin_token_is_unauthorized(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "expected-admin-secret")
    app = FastAPI()
    app.middleware("http")(session.auth_middleware)

    @app.post("/admin/ingest")
    async def _ingest():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/admin/ingest",
        headers=[
            (b"origin", b"http://testserver"),
            (b"x-admin-token", "é".encode("latin-1")),
        ],
    )

    assert response.status_code == 401
    assert response.json() == {"ok": False, "error": "Unauthorized"}


# --------------------------------------------------------------- the store

@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    """A fresh claudit DB with the startup schema applied — the real
    user_session table the store functions run against."""
    _clear_user_config_cache()
    monkeypatch.setattr(session, "load_user_config", lambda user_id: _TEST_CONFIG)
    yield from scratch_db.scratch_viz_database(monkeypatch, "session")
    _clear_user_config_cache()


def test_credential_fingerprint_hashes_hash_and_salt_with_a_separator():
    assert (
        session.credential_fingerprint(_TEST_CONFIG)
        == _TEST_CREDENTIAL_FP
    )
    assert session.credential_fingerprint({
        "web_password_hash": "other-hash",
        "web_password_salt": "stored-salt",
    }) != _TEST_CREDENTIAL_FP
    assert session.credential_fingerprint({
        "web_password_hash": "stored-hash",
        "web_password_salt": "other-salt",
    }) != _TEST_CREDENTIAL_FP


def test_session_row_first_call_inserts(fresh_db):
    secret, generation = session.get_or_create_session_row(
        12345, _TEST_CREDENTIAL_FP
    )
    assert generation == 0
    assert secret
    assert session.load_session_row(12345) == (
        secret, 0, _TEST_CREDENTIAL_FP
    )


def test_session_row_second_call_returns_same_row(fresh_db):
    first = session.get_or_create_session_row(12345, _TEST_CREDENTIAL_FP)
    assert session.get_or_create_session_row(
        12345, _TEST_CREDENTIAL_FP
    ) == first


def test_session_row_conflict_with_null_fingerprint_rotates_secret(
    fresh_db,
):
    """A NULL fingerprint differs from the just-proven credential, so
    binding it rotates the secret and invalidates previously captured
    tokens."""
    with db.viz_conn() as c:
        c.execute(
            "INSERT INTO user_session (user_id, secret) VALUES (%s, %s)",
            (555, "winner-secret"),
        )
        c.commit()
    secret, generation = session.get_or_create_session_row(
        555, _TEST_CREDENTIAL_FP
    )
    assert secret != "winner-secret"
    assert generation == 1
    assert session.load_session_row(555) == (
        secret, 1, _TEST_CREDENTIAL_FP
    )


def test_session_row_changed_fingerprint_rotates_secret_and_generation(
    fresh_db,
):
    secret, generation = session.get_or_create_session_row(
        77, _TEST_CREDENTIAL_FP
    )
    token = session.make_session_token(77, secret, generation=generation)
    assert session.resolve_session_user_id(token) == 77

    changed_secret, changed_generation = session.get_or_create_session_row(
        77, "new-fingerprint"
    )
    assert changed_secret != secret
    assert changed_generation == generation + 1
    assert session.load_session_row(77) == (
        changed_secret, generation + 1, "new-fingerprint"
    )
    assert session.resolve_session_user_id(token) is None

    assert session.get_or_create_session_row(77, "new-fingerprint") == (
        changed_secret, changed_generation
    )


def test_session_row_accepts_an_18_digit_user_id(fresh_db):
    """Issue #185: the auth DB's user ids are bigint (18 digits), so the
    user_session row must hold one — an id above 2^31 must insert, load
    and round-trip instead of failing with integer-out-of-range."""
    secret, generation = session.get_or_create_session_row(
        123456789012345678, _TEST_CREDENTIAL_FP)
    assert generation == 0
    assert secret
    assert session.load_session_row(123456789012345678) == (
        secret, 0, _TEST_CREDENTIAL_FP
    )


def test_resolve_rejects_real_user_without_a_session_row(fresh_db):
    tok = session.make_session_token(314, "some-secret", generation=0)
    assert session.resolve_session_user_id(tok) is None


def test_bump_invalidates_the_token_and_relogin_mints_a_new_one(fresh_db):
    secret, generation = session.get_or_create_session_row(
        77, _TEST_CREDENTIAL_FP
    )
    tok = session.make_session_token(77, secret, generation=generation)
    assert session.resolve_session_user_id(tok) == 77

    session.bump_session_generation(77)

    assert session.resolve_session_user_id(tok) is None
    # A re-login after the bump keeps the secret and returns the new
    # generation, so the freshly minted token verifies again.
    secret2, generation2 = session.get_or_create_session_row(
        77, _TEST_CREDENTIAL_FP
    )
    assert (secret2, generation2) == (secret, generation + 1)
    tok2 = session.make_session_token(77, secret2, generation=generation2)
    assert session.resolve_session_user_id(tok2) == 77


def test_preexisting_session_row_without_fingerprint_is_invalid(
    fresh_db, monkeypatch
):
    with db.viz_conn() as c:
        c.execute(
            "INSERT INTO user_session (user_id, secret) VALUES (%s, %s)",
            (808, "legacy-secret"),
        )
        c.commit()
    monkeypatch.setattr(
        session,
        "load_user_config",
        lambda user_id: pytest.fail("NULL fingerprint must reject first"),
    )
    token = session.make_session_token(808, "legacy-secret", generation=0)

    assert session.resolve_session_user_id(token) is None


def test_invalid_signature_does_not_load_auth_config(fresh_db, monkeypatch):
    secret, generation = session.get_or_create_session_row(
        809, _TEST_CREDENTIAL_FP
    )
    token = session.make_session_token(809, secret, generation=generation)
    payload, _signature = token.rsplit(".", 1)
    monkeypatch.setattr(
        session,
        "load_user_config",
        lambda user_id: pytest.fail("invalid signature must reject first"),
    )

    assert session.resolve_session_user_id(f"{payload}.invalid") is None


def test_session_resolution_caches_auth_config_for_sixty_seconds(
    fresh_db, monkeypatch
):
    config = dict(_TEST_CONFIG)
    calls: list[int] = []
    now = [int(time.time())]
    monkeypatch.setattr(session, "load_user_config", lambda uid: (
        calls.append(uid) or config
    ))
    monkeypatch.setattr(session.time, "time", lambda: now[0])
    _clear_user_config_cache()
    secret, generation = session.get_or_create_session_row(
        810, _TEST_CREDENTIAL_FP
    )
    token = session.make_session_token(810, secret, generation=generation)

    assert session.resolve_session_user_id(token) == 810
    assert session.resolve_session_user_id(token) == 810
    assert calls == [810]

    now[0] += 61
    assert session.resolve_session_user_id(token) == 810
    assert calls == [810, 810]


def test_operator_reads_the_auth_db_flag():
    """The capability is one boolean key in the auth DB's `users.config`,
    read through the same 60-second cache the credential resolution uses."""
    _clear_user_config_cache()
    session.remember_user_config(31, _TEST_CONFIG)
    assert session.is_operator(31) is False
    session.remember_user_config(
        31, {**_TEST_CONFIG, session.OPERATOR_KEY: True})
    assert session.is_operator(31) is True
    _clear_user_config_cache()


def test_a_user_with_no_config_row_is_not_an_operator():
    """No row, no capability. This is the shape every guest resolves as."""
    _clear_user_config_cache()
    session.remember_user_config(32, None)
    assert session.is_operator(32) is False
    _clear_user_config_cache()


def test_the_operator_flag_is_a_boolean_and_nothing_else():
    """`{"web_operator": "false"}` is JSON that hands Python a TRUTHY
    string, so a truthiness test would GRANT operator rights on a
    capability row that says no. Only a real JSON boolean counts."""
    _clear_user_config_cache()
    for value in ("false", "no", "true", 1, 0, [], {}, None):
        session.remember_user_config(33, {session.OPERATOR_KEY: value})
        assert session.is_operator(33) is False, repr(value)
    _clear_user_config_cache()


def test_a_guest_is_an_operator_for_nothing_at_all():
    """Refused before the config is even read, so "a guest is never an
    operator" does not depend on the auth DB happening to hold no row for
    user 0 — the cache below is primed with one that says yes."""
    _clear_user_config_cache()
    session.remember_user_config(
        session.GUEST_USER_ID, {session.OPERATOR_KEY: True})
    assert session.is_operator(session.GUEST_USER_ID) is False
    _clear_user_config_cache()


def test_user_config_cache_stays_within_its_key_limit(monkeypatch):
    _clear_user_config_cache()

    for user_id in range(1025):
        session.remember_user_config(user_id, _TEST_CONFIG)

    assert len(session._USER_CONFIG_CACHE) <= 1024  # pylint: disable=protected-access


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


def test_check_origin_rejects_malformed_ipv6_origin():
    scope = {
        "type": "http", "method": "POST",
        "headers": [
            (b"host", b"viz.example.com"),
            (b"origin", b"http://[::1"),
        ],
        "path": "/login",
    }
    assert not session.check_origin(Request(scope))


def test_check_origin_rejects_malformed_ipv6_referer():
    scope = {
        "type": "http", "method": "POST",
        "headers": [
            (b"host", b"viz.example.com"),
            (b"referer", b"http://[ typo/x"),
        ],
        "path": "/login",
    }
    assert not session.check_origin(Request(scope))


def test_malformed_ipv6_origin_login_is_forbidden():
    app = FastAPI()
    app.middleware("http")(session.auth_middleware)

    @app.post("/login")
    async def _login():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/login",
        headers={"Origin": "http://[::1"},
        follow_redirects=False,
    )

    assert response.status_code == 403


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
