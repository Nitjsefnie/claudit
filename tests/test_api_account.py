"""Login leaves a revocable session row; activity slides the cookie.

Also covers the /api/account/sessions self-service endpoints (list + revoke).

Imports are top-level (like tests/test_sessions_repo.py): pytest imports
conftest.py — and therefore lands the test DSN setdefaults — before any
test module in this directory, so a deferred-import guard is not needed
here.
"""
import json
from http.cookies import Morsel, SimpleCookie

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth, db
from backend import session as session_mod
from backend import sessions_repo
from backend.api_account import router as account_router
from backend.app import app

_ACCOUNT_SECRET = "account-test-secret-0123456789"
_ORIGIN = {"origin": "http://testserver"}


def _seed_user(uid: int) -> None:
    """Insert an auth-DB user row with a known password + session secret."""
    config = {session_mod.WEB_SESSION_SECRET_KEY: _ACCOUNT_SECRET}
    auth.set_web_password(config, "fixture-password")
    with db.auth_conn() as c:
        c.execute(
            "INSERT INTO users (user_id, config) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (user_id) DO UPDATE SET config = EXCLUDED.config",
            (uid, json.dumps(config)),
        )


def _login_as(uid: int) -> TestClient:
    client = TestClient(app)
    resp = client.post(
        "/login",
        data={"user_id": str(uid), "password": "fixture-password"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return client


def _nonce_of(client: TestClient) -> str:
    cookie = client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    parsed = session_mod.parse_session_token(cookie or "")
    assert parsed is not None
    return parsed[2]


def _session_cookie_morsel(resp) -> Morsel:
    """The session-cookie Morsel from a response's Set-Cookie headers."""
    for header in resp.headers.get_list("set-cookie"):
        jar = SimpleCookie()
        jar.load(header)
        morsel = jar.get(session_mod.SESSION_COOKIE_NAME)
        if morsel is not None:
            return morsel
    raise AssertionError("no session cookie in Set-Cookie headers")


def test_login_records_a_session_row(logged_in_client, auth_db):
    cookie = logged_in_client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    parsed = session_mod.parse_session_token(cookie)
    assert parsed is not None
    nonce = parsed[2]
    assert sessions_repo.is_session_active(nonce) is True


def test_authenticated_request_refreshes_the_cookie(logged_in_client, auth_db):
    before = logged_in_client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    resp = logged_in_client.get("/api/me")
    assert resp.status_code == 200
    morsel = _session_cookie_morsel(resp)
    # The SAME cookie value is re-issued — a re-mint here would mint a new
    # nonce per request, write a web_sessions row each time, and break the
    # session list.
    assert morsel.value == before
    # The full flag contract, matching the login sites' set_cookie calls.
    # Parsed-attribute equality, not substring: a substring check on
    # "Max-Age=34560000" would also accept Max-Age=345600000.
    assert morsel["max-age"] == str(session_mod.SESSION_COOKIE_MAX_AGE)
    assert morsel["httponly"] is True
    assert morsel["samesite"] == "strict"
    assert morsel["path"] == "/"
    # Never a Domain attribute — the cookie must stay host-only.
    assert morsel["domain"] == ""
    # conftest forces COOKIE_SECURE=0 so TestClient (plain HTTP) returns the
    # cookie at all; test_cookie_secure_tracks_the_env pins both directions.
    assert bool(morsel["secure"]) is False


def test_cookie_secure_tracks_the_env(logged_in_client, auth_db, monkeypatch):
    # secure=False leg first: once the jar holds a Secure cookie, httpx
    # refuses to send it over TestClient's plain HTTP (401, no slide).
    monkeypatch.setenv("COOKIE_SECURE", "0")
    morsel = _session_cookie_morsel(logged_in_client.get("/api/me"))
    assert bool(morsel["secure"]) is False
    monkeypatch.setenv("COOKIE_SECURE", "1")
    morsel = _session_cookie_morsel(logged_in_client.get("/api/me"))
    assert bool(morsel["secure"]) is True


def test_guest_request_does_not_refresh_the_cookie(guest_client, auth_db):
    resp = guest_client.get("/api/me")
    assert resp.status_code == 200
    assert session_mod.SESSION_COOKIE_NAME not in resp.headers.get("set-cookie", "")


def test_admin_path_does_not_slide_the_cookie(logged_in_client, auth_db):
    # /admin/* authenticates by token, not session: _session_denied never
    # runs there, so no user_id is resolved and the slide must not fire —
    # even though a session cookie rides along on the request.
    resp = logged_in_client.post(
        "/admin/nope",
        headers={"X-Admin-Token": "test-admin", "origin": "http://testserver"},
    )
    assert resp.status_code == 404
    assert session_mod.SESSION_COOKIE_NAME not in resp.headers.get("set-cookie", "")


def test_account_sessions_without_identity_is_401(auth_db):
    """No session cookie at all -> 401 before any sessions_repo call.

    Mounted WITHOUT auth_middleware on purpose: the middleware would 401
    first, and this test exists to pin the handlers' own guard — a falsy
    user_id must never reach sessions_repo.
    """
    bare = FastAPI()
    bare.include_router(account_router, prefix="/api")
    client = TestClient(bare)
    resp = client.get("/api/account/sessions")
    assert resp.status_code == 401
    assert "sessions" not in resp.json()
    resp = client.post(
        "/api/account/sessions/revoke", json={"nonce": "x"}, headers=_ORIGIN
    )
    assert resp.status_code == 401
    assert "revoked" not in resp.json()


def test_account_sessions_guest_forbidden(guest_client, auth_db):
    # /api/account is NOT in the middleware's guest-deny list (that list is
    # for transcript data), so these 403s come from the handlers themselves.
    resp = guest_client.get("/api/account/sessions")
    assert resp.status_code == 403
    resp = guest_client.post(
        "/api/account/sessions/revoke", json={"nonce": "x"}, headers=_ORIGIN
    )
    assert resp.status_code == 403


def test_account_sessions_lists_only_own_sessions(auth_db):
    # Private user_ids, never conftest's _TEST_UID: the auth DB is
    # module-scoped and list_sessions is keyed by user alone.
    _seed_user(987001)
    _seed_user(987099)
    first = _login_as(987001)
    second = _login_as(987001)
    other = _login_as(987099)
    resp = first.get("/api/account/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    nonces = {s["nonce"] for s in body["sessions"]}
    assert nonces == {_nonce_of(first), _nonce_of(second)}
    assert _nonce_of(other) not in nonces
    for entry in body["sessions"]:
        assert set(entry) == {
            "nonce", "created_at", "last_seen_at", "user_agent", "ip",
        }


def test_account_sessions_revoke(auth_db):
    _seed_user(987002)
    first = _login_as(987002)
    second = _login_as(987002)
    nonce_second = _nonce_of(second)
    resp = first.post(
        "/api/account/sessions/revoke",
        json={"nonce": nonce_second},
        headers=_ORIGIN,
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "revoked": True}
    assert sessions_repo.is_session_active(nonce_second) is False
    remaining = {
        s["nonce"]
        for s in first.get("/api/account/sessions").json()["sessions"]
    }
    assert remaining == {_nonce_of(first)}


def test_account_sessions_cannot_revoke_another_users(auth_db):
    _seed_user(987003)
    _seed_user(987004)
    mine = _login_as(987003)
    theirs = _login_as(987004)
    nonce_theirs = _nonce_of(theirs)
    resp = mine.post(
        "/api/account/sessions/revoke",
        json={"nonce": nonce_theirs},
        headers=_ORIGIN,
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "revoked": False}
    assert sessions_repo.is_session_active(nonce_theirs) is True


def test_account_sessions_revoke_requires_nonce(auth_db):
    _seed_user(987005)
    client = _login_as(987005)
    # {} is plain missing; {"nonce": None} and {"nonce": 123} pin the
    # isinstance(raw_nonce, str) guard — a str() coercion would turn the
    # null into the literal string "None" and look it up as a real nonce.
    for body in ({}, {"nonce": None}, {"nonce": 123}):
        resp = client.post(
            "/api/account/sessions/revoke", json=body, headers=_ORIGIN
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False
