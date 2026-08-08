import contextlib
import json
from http.cookies import SimpleCookie

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import db
from backend import login as login_mod
from backend import session as session_mod
from backend import auth
from backend import sessions_repo
from backend.app import app as real_app


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """The login module's rate-limit dict is process-global; clear between tests
    so test_session_cookie_round_trip doesn't inherit failures from
    test_rate_limit_after_5_failures (both POST from the same TestClient host).
    """
    login_mod.reset_login_rate_limits()
    yield
    login_mod.reset_login_rate_limits()


@pytest.fixture(name="app")
def _app_fixture(monkeypatch):
    """Build a fresh FastAPI app per test, with the auth DB mocked."""
    a = FastAPI()
    a.middleware("http")(session_mod.auth_middleware)
    a.include_router(login_mod.router)

    @a.get("/api/me")
    def me(request: Request):
        return {"user_id": request.state.user_id}

    return a


@pytest.fixture(name="fake_user")
def _fake_user_fixture(monkeypatch):
    """Stub the auth DB with one user that has a known password."""
    config: dict = {}
    auth.set_web_password(config, "hunter2")
    store = {12345: config}

    def _load(user_id, **_kwargs):
        return store.get(user_id)

    def _write(user_id, cfg):
        store[user_id] = cfg

    def _exists(user_id):
        return user_id in store

    monkeypatch.setattr(session_mod, "load_user_config", _load)
    monkeypatch.setattr(session_mod, "write_user_config", _write)
    monkeypatch.setattr(login_mod, "user_exists", _exists)
    # Session rows go to the real auth DB, which these tests don't have;
    # row recording is covered end-to-end in test_api_account.py.
    monkeypatch.setattr(
        sessions_repo, "record_session", lambda *args, **kwargs: None
    )
    # resolve_session_user_id now checks the nonce against web_sessions;
    # with no real auth DB here, treat every nonce as active. The reject
    # path is covered against a real DB in test_session.py.
    #
    # is_session_active must stay pinned at True even though /logout now
    # revokes server-side: the logout tests below assert a post-logout 401
    # specifically to prove a cookie-DELETION leg fired (header inspection
    # cannot see a surviving cookie that still resolves). If revocation
    # could also produce that 401, those tests would pass with a deletion
    # leg removed and the dual-keying clearing would be untested. So
    # revocation is stubbed to a no-op instead — the real revocation path
    # is pinned against a real auth DB further down this file.
    monkeypatch.setattr(
        sessions_repo, "is_session_active", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(
        sessions_repo, "touch_session", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        sessions_repo, "revoke_session", lambda *args, **kwargs: True
    )

    # resolve_session_user_id opens one shared auth_conn() around the
    # (stubbed) config/nonce lookups; yield a dummy since there is no
    # real auth DB here at all.
    @contextlib.contextmanager
    def _no_auth_conn():
        yield None

    monkeypatch.setattr(db, "auth_conn", _no_auth_conn)
    return store


def test_login_page_is_html(app):
    client = TestClient(app)
    r = client.get("/login")
    assert r.status_code == 200
    assert "<form" in r.text and "user_id" in r.text


def test_successful_login_sets_cookie(app, fake_user):
    client = TestClient(app)
    r = client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    assert session_mod.SESSION_COOKIE_NAME in r.cookies


def test_wrong_password_is_401(app, fake_user):
    client = TestClient(app)
    r = client.post(
        "/login",
        data={"user_id": "12345", "password": "wrong"},
    )
    assert r.status_code == 401


def test_unknown_user_is_404(app, fake_user):
    client = TestClient(app)
    r = client.post(
        "/login",
        data={"user_id": "999", "password": "anything"},
    )
    assert r.status_code == 404


def test_rate_limit_after_5_failures(app, fake_user):
    client = TestClient(app)
    for _ in range(5):
        client.post("/login", data={"user_id": "12345", "password": "x"})
    r = client.post("/login", data={"user_id": "12345", "password": "x"})
    assert r.status_code == 429


def test_logout_clears_cookie(app, fake_user):
    client = TestClient(app)
    client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert any(
        session_mod.SESSION_COOKIE_NAME in v
        for v in r.headers.get_list("set-cookie")
    )


def test_logout_rejects_the_next_request(app, fake_user):
    """End to end: after logout the session must stop authenticating.

    Helper symmetry (test_clear_cookie_path_matches_the_setter) does not
    pin this: a clear_session_cookie that re-issues a LIVE cookie leaves
    that test green while logout silently stops working (Amendment 5 of
    the Phase 2 plan). The jar assertion is what catches that mutant —
    it stores the live cookie as the non-empty value '""', so the
    server-side 401 alone cannot tell it apart from a real deletion.
    """
    client = TestClient(app)
    client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    r = client.get("/api/me")
    assert r.status_code == 200
    client.get("/logout", follow_redirects=False)
    # The jar must not hold a usable session cookie after logout.
    assert not client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    # Unauthenticated /api/* gets a JSON 401 (see _unauthenticated).
    r = client.get("/api/me")
    assert r.status_code == 401


def test_logout_clears_a_pre_rollout_host_only_cookie(app, fake_user, monkeypatch):
    """The rollout boundary, not the steady state.

    A user logged in BEFORE SESSION_COOKIE_DOMAIN was turned on holds a
    host-only session cookie. After the rollout the server issues and
    deletes domain-keyed cookies — a different (name, domain, path) key —
    so a logout that clears only the domain keying leaves the host-only
    cookie alive and still authenticating. The post-logout request is the
    assertion that matters: header inspection alone cannot see a surviving
    cookie that still resolves.
    """
    # Pre-rollout: no domain configured, login issues a host-only cookie.
    monkeypatch.delenv("SESSION_COOKIE_DOMAIN", raising=False)
    client = TestClient(app)
    client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    assert client.get("/api/me").status_code == 200
    # The rollout happens between login and logout. The domain needs an
    # embedded dot: http.cookiejar refuses to return a dotless
    # Domain=testserver cookie to host testserver.
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "testserver.local")
    client.get("/logout", follow_redirects=False)
    # The surviving host-only cookie must NOT authenticate — this is the
    # assertion that fails when the host-only deletion is dropped.
    r = client.get("/api/me")
    assert r.status_code == 401
    assert not client.cookies.get(session_mod.SESSION_COOKIE_NAME)


def test_guest_logout_with_domain_set_rejects_the_next_request(app, monkeypatch):
    """Guest logout must work when SESSION_COOKIE_DOMAIN is set.

    Guest cookies are host-only BY DESIGN (guest secrets are per-service —
    see set_session_cookie), so when the variable is set the domain-keyed
    deletion can never match a guest cookie: a clearer that deletes only
    the domain keying leaves the guest cookie alive and still
    authenticating. The post-logout request is the assertion that matters —
    header inspection cannot see a surviving cookie that still resolves.
    """
    # Same dotted-domain rule as the pre-rollout test above: http.cookiejar
    # refuses a dotless Domain=testserver cookie for host testserver.
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "testserver.local")
    client = TestClient(app)
    client.post("/login/guest", follow_redirects=False)
    assert client.get("/api/me").status_code == 200
    client.get("/logout", follow_redirects=False)
    # The surviving host-only guest cookie must NOT authenticate — this is
    # the assertion that fails when the host-only deletion is dropped.
    r = client.get("/api/me")
    assert r.status_code == 401
    assert not client.cookies.get(session_mod.SESSION_COOKIE_NAME)


def test_guest_cookie_gets_no_domain(app, monkeypatch):
    """A guest cookie must stay host-only even when the domain is set.

    Guest secrets are per-service: a guest cookie issued with the shared
    domain would be rejected by every service except the one that minted
    it, and would shadow the host-only guest cookie there.
    """
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "example.test")
    client = TestClient(app)
    r = client.post("/login/guest", follow_redirects=False)
    assert r.status_code in (302, 303)
    jar = SimpleCookie()
    for header in r.headers.get_list("set-cookie"):
        jar.load(header)
    morsel = jar.get(session_mod.SESSION_COOKIE_NAME)
    assert morsel is not None
    assert morsel["domain"] == ""


def test_session_cookie_round_trip(app, fake_user):
    client = TestClient(app)
    client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json() == {"user_id": 12345}


def _seed_auth_user(user_id: int, secret: str, password: str) -> None:
    """Insert a private user row into the scratch auth DB (auth_db
    fixture), with a web password and a session secret, so a real login
    and real token minting work against it."""
    config = {session_mod.WEB_SESSION_SECRET_KEY: secret}
    auth.set_web_password(config, password)
    with db.auth_conn() as c:
        c.execute(
            "INSERT INTO users (user_id, config) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (user_id) DO UPDATE SET config = EXCLUDED.config",
            (user_id, json.dumps(config)),
        )


def test_logout_revokes_the_session_row(auth_db):
    """The acceptance pin: after logout, the same token presented by a
    client that never saw the deletion response must be rejected.

    Asserting the logout response's status, redirect, or Set-Cookie
    headers passes in full while the session stays alive server-side —
    cookie clearing is invisible to a second client holding the same
    token, which stands in for a different service in the fleet. Only a
    server-side revocation of the token's own nonce can make its /api/me
    fail.

    This test lives in THIS file because the no-op-revocation mutants
    (the handler's revoke_session call removed, or revoke_session itself
    returning without writing) must kill it by assertion while every
    other test in the file stays green. The files that exercise
    revoke_session directly (test_sessions_repo.py, test_session.py,
    test_api_account.py) go red under the second mutant for their own
    reasons; here, every other test either runs on the fake_user fixture
    — which monkeypatches revoke_session over the mutant — or never
    touches revocation.
    """
    # Private user_id: 987001-987005 are taken elsewhere and the shared
    # fixture user 4242 is used across this module-scoped database.
    uid = 987010
    _seed_auth_user(uid, "task7a-acceptance-session-secret", "task7a-pw")

    client = TestClient(real_app)
    resp = client.post(
        "/login",
        data={"user_id": str(uid), "password": "task7a-pw"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    # Capture BEFORE the logout — the logout empties this client's jar.
    token = client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    assert token
    parsed = session_mod.parse_session_token(token)
    assert parsed is not None

    # A second client holding the same token, which the logout's deletion
    # response can never reach — a sibling service in the fleet.
    other = TestClient(real_app)
    other.cookies.set(session_mod.SESSION_COOKIE_NAME, token)
    # Precondition: the token really authenticates, or a later 401 proves
    # nothing — the token could never have worked at all.
    assert other.get("/api/me").status_code == 200

    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303

    # The second client's jar still holds the identical token — the 401
    # below cannot be a deletion that somehow propagated, only the
    # server-side revocation. (Iterate rather than .get(): the sliding
    # re-issue on the /api/me above leaves the jar holding the same value
    # under two keyings, which .get() reports as a CookieConflict.)
    jar_values = [
        c.value
        for c in other.cookies.jar
        if c.name == session_mod.SESSION_COOKIE_NAME
    ]
    assert jar_values and all(v == token for v in jar_values)
    assert other.get("/api/me").status_code == 401
    # Unit-level pin, localising a failure to "the row was not revoked"
    # versus "the middleware did not reject".
    assert sessions_repo.is_session_active(parsed[2]) is False


def test_logout_with_forged_cookie_cannot_revoke_a_victims_session(auth_db):
    """The revoked user_id must come from the VERIFIED resolution, never
    from parse_session_token.

    parse_session_token performs no signature check — every field it
    returns is attacker-controlled. /logout is on the public-path
    allowlist, so an attacker who learns a victim's nonce can forge
    <victim_uid>.<any_ts>.<victim_nonce>.garbage and hit /logout. A
    handler that revokes with the parsed (unverified) user_id kills the
    victim's live session across the whole fleet — revoke_session's
    AND user_id = %s filter cannot stop it, because the attacker supplied
    the matching user_id. The victim's session must survive.
    """
    victim_uid = 987011
    victim_secret = "task7a-victim-session-secret"
    _seed_auth_user(victim_uid, victim_secret, "task7a-victim-pw")

    victim_token = session_mod.make_session_token(victim_uid, victim_secret)
    parsed = session_mod.parse_session_token(victim_token)
    assert parsed is not None
    nonce = parsed[2]
    sessions_repo.record_session(victim_uid, nonce, "curl", "127.0.0.1")
    # Precondition: the victim's session is live, so its survival means
    # the forged logout was refused — not that there was nothing to kill.
    assert session_mod.resolve_session_user_id(victim_token) == victim_uid

    forged = f"{victim_uid}.{parsed[1]}.{nonce}.{'0' * 64}"
    attacker = TestClient(real_app)
    attacker.cookies.set(session_mod.SESSION_COOKIE_NAME, forged)
    r = attacker.get("/logout", follow_redirects=False)
    # A forged cookie is not an error: logout still succeeds locally.
    assert r.status_code == 303
    # ...but it must not have revoked the victim's row.
    assert sessions_repo.is_session_active(nonce) is True
    assert session_mod.resolve_session_user_id(victim_token) == victim_uid
