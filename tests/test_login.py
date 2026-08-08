import contextlib
import json
from http.cookies import SimpleCookie

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

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


def _session_cookie_morsel(response):
    """Return the final session-cookie Morsel from a response."""
    return _session_cookie_morsels(response)[-1]


def _session_cookie_morsels(response):
    """Return session-cookie Morsels in response-header order."""
    morsels = []
    get_list = getattr(response.headers, "get_list", None) or response.headers.getlist
    for header in get_list("set-cookie"):
        jar = SimpleCookie()
        jar.load(header)
        morsel = jar.get(session_mod.SESSION_COOKIE_NAME)
        if morsel is not None:
            morsels.append(morsel)
    if not morsels:
        raise AssertionError("no session cookie in Set-Cookie headers")
    return morsels


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
    # The dotted configured domain is needed for http.cookiejar's host
    # matching: testserver.local is accepted, while a dotless
    # Domain=testserver cookie is not returned to host testserver.
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "testserver.local")
    client.get("/logout", follow_redirects=False)
    # The surviving host-only cookie must NOT authenticate — this is the
    # assertion that fails when the host-only deletion is dropped.
    r = client.get("/api/me")
    assert r.status_code == 401
    assert not client.cookies.get(session_mod.SESSION_COOKIE_NAME)


def test_guest_logout_with_shared_domain_rejects_the_next_request(app, monkeypatch):
    """Guest logout must clear a domain-keyed guest cookie.

    A matching GUEST_SESSION_SECRET lets a recipient service verify a guest
    token issued with the configured shared domain, so logout must clear
    that domain-keyed cookie. The post-logout request is the assertion that
    matters — header inspection cannot see a surviving cookie that still
    resolves.
    """
    # The dotted configured domain matches this explicit client URL; a
    # dotless Domain=testserver cookie is not returned by http.cookiejar.
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "testserver.local")
    client = TestClient(app, base_url="http://testserver.local")
    response = client.post("/login/guest", follow_redirects=False)
    assert _session_cookie_morsel(response)["domain"] == "testserver.local"
    assert client.get("/api/me").status_code == 200
    client.get("/logout", follow_redirects=False)
    # The surviving domain-keyed guest cookie must NOT authenticate — this is
    # the assertion that fails when the host-only deletion is dropped.
    r = client.get("/api/me")
    assert r.status_code == 401
    assert not client.cookies.get(session_mod.SESSION_COOKIE_NAME)


def test_guest_cookie_gets_shared_domain(app, monkeypatch):
    """A guest cookie uses the configured shared domain.

    A shared GUEST_SESSION_SECRET lets a receiving service verify the
    domain-keyed guest cookie when its cookie-domain setting matches.
    """
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "example.test")
    client = TestClient(app, base_url="http://example.test")
    r = client.post("/login/guest", follow_redirects=False)
    assert r.status_code in (302, 303)
    morsels = _session_cookie_morsels(r)
    assert len(morsels) == 3
    assert sorted(m["domain"] for m in morsels[:2]) == ["", "example.test"]
    assert all(m["max-age"] == "0" for m in morsels[:2])
    assert morsels[-1]["domain"] == "example.test"
    assert morsels[-1]["max-age"] == str(session_mod.SESSION_COOKIE_MAX_AGE)
    # The jar check records the live value retained from this response; it
    # does not measure browser ordering for duplicate cookie names.
    assert client.cookies.get(session_mod.SESSION_COOKIE_NAME) == morsels[-1].value


def test_guest_and_user_cookies_use_the_same_domain_accessor(monkeypatch):
    """Guest and authenticated cookies read one domain configuration."""
    expected = "from-accessor.example"
    monkeypatch.setattr(session_mod, "_session_cookie_domain", lambda: expected)

    guest = Response()
    session_mod.set_session_cookie(guest, "guest-token")
    user = Response()
    session_mod.set_session_cookie(user, "user-token")

    assert _session_cookie_morsel(guest)["domain"] == expected
    assert _session_cookie_morsel(user)["domain"] == expected


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


def test_logout_fails_loudly_when_the_revocation_raises(
    app, fake_user, monkeypatch
):
    """A logout that cannot revoke must not report success.

    Clearing the cookie while the row survives is the exact defect the
    revocation exists to remove: the user is told they signed out, the
    local cookie is gone, and the token stays valid for every sibling
    service with no cookie left to retry the revocation with. So the
    revocation runs BEFORE the response is built, and its failure
    propagates. Both assertions are load-bearing: the 500 kills the
    swallow (a swallowed revocation returns the normal 303), and the
    absent Set-Cookie pins that no cookie deletion is handed out
    alongside the failure.
    """
    client = TestClient(app)
    client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    token = client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    assert token

    def _boom(*_args, **_kwargs):
        raise RuntimeError("auth DB unreachable")

    monkeypatch.setattr(sessions_repo, "revoke_session", _boom)

    # raise_server_exceptions=False so the server error surfaces as a
    # response to assert on instead of propagating into the test.
    quiet = TestClient(app, raise_server_exceptions=False)
    quiet.cookies.set(session_mod.SESSION_COOKIE_NAME, token)
    r = quiet.get("/logout", follow_redirects=False)
    assert r.status_code == 500
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_guest_logout_never_touches_the_auth_db(app, monkeypatch):
    """The guest exclusion must hold with the auth DB down — by assertion.

    Guests have no web_sessions rows by design, so logout returns before
    ANY auth-DB access for them, and a total auth-DB outage must not
    break guest logout. Removing the exclusion makes the handler call
    revoke_session, which reaches for the (here: raising) pool and the
    logout 500s — until now that mutant was caught only by an incidental
    PoolTimeout from a scratch auth DB that happened not to exist, which
    is a crash, and one that depends on test ordering.

    Status alone cannot pin the guest path: a no-op handler that skips
    clear_session_cookie ALSO returns 303. The clearing assertions are
    what fail there — under a no-op the jar still holds the live guest
    cookie and the next request keeps authenticating.

    Deliberate overlap: the domain-set half of this test re-covers
    test_guest_logout_with_shared_domain_rejects_the_next_request. Both are
    kept — this one pins zero auth-DB access BY ASSERTION (the other
    would catch a removed guest exclusion only via an incidental
    PoolTimeout crash, dependent on test ordering), while that one runs
    with no db.auth_conn monkeypatch at all, so it alone sees a
    regression in the interplay between the guest path and the real
    pool.
    """
    def _down():
        raise RuntimeError("auth DB down")

    monkeypatch.setattr(db, "auth_conn", _down)
    # Domain set: also pins that guest logout clears BOTH keyings while
    # the guest cookie uses the configured domain.
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "testserver.local")
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/login/guest", follow_redirects=False)
    # Precondition: the guest cookie really authenticates, or a later
    # 401 proves nothing.
    assert client.get("/api/me").status_code == 200
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    # The guest path must still CLEAR the cookie — the assertions a bare
    # 303-redirect handler fails.
    _assert_both_deletion_legs(r)
    assert not client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    assert client.get("/api/me").status_code == 401


def _assert_both_deletion_legs(response):
    """Both (name, domain, path) keyings must get a real DELETION header.

    The full key is asserted, not just the domain half: the two legs must
    differ on Domain= (one keyed to the shared domain, one host-only),
    both must carry the same Path=/ the setter uses — a deletion keyed
    (name, domain, /other-path) matches nothing the browser holds — and
    both must actually DELETE (empty value, expiry in the past), not
    re-issue a live cookie.
    """
    morsels = []
    for header in response.headers.get_list("set-cookie"):
        jar = SimpleCookie()
        jar.load(header)
        morsel = jar.get(session_mod.SESSION_COOKIE_NAME)
        if morsel is not None:
            morsels.append(morsel)
    assert any(m["domain"] for m in morsels)
    assert any(not m["domain"] for m in morsels)
    for m in morsels:
        assert m["path"] == "/"
        assert m.value.strip('"') == ""
        assert "1970" in m["expires"] or m["max-age"] == "0"


def test_logout_without_a_cookie_still_clears_both_keyings(app, monkeypatch):
    """No cookie at all: the same 303 with both deletion legs, never a 500.

    Nothing exercised this leg — an unguarded index into a None parse
    result would 500 here while every existing logout test stayed green.
    """
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "example.test")
    client = TestClient(app)
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    _assert_both_deletion_legs(r)


def test_logout_with_a_malformed_cookie_still_clears_both_keyings(
    app, monkeypatch
):
    """A cookie that does not parse: the same 303 with both deletion legs.

    parse_session_token returns None for garbage; the handler must skip
    the revocation quietly and still clear, or a corrupted cookie would
    make logout 500 — and nothing checked that.
    """
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "example.test")
    client = TestClient(app)
    client.cookies.set(session_mod.SESSION_COOKIE_NAME, "not.a.real.token")
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    _assert_both_deletion_legs(r)


def test_logout_revokes_only_the_presented_session(auth_db):
    """Logging out revokes THIS session, never the user's other sessions.

    A blanket revoke keyed on user_id alone signs the user out on every
    other device — and was measured to leave the whole suite green on
    the sibling service this shape is the template for. Two logins, two
    nonces: logging out of the first must revoke exactly it, and the
    second must stay active and keep authenticating.
    """
    # Private user_id: 987001-987005, 987010 and 987011 are taken.
    uid = 987012
    _seed_auth_user(uid, "task7a-second-device-secret", "task7a-devices-pw")

    client1 = TestClient(real_app)
    r1 = client1.post(
        "/login",
        data={"user_id": str(uid), "password": "task7a-devices-pw"},
        follow_redirects=False,
    )
    assert r1.status_code == 303, r1.text
    token1 = client1.cookies.get(session_mod.SESSION_COOKIE_NAME)
    assert token1

    # A second device: its own login, its own nonce.
    client2 = TestClient(real_app)
    r2 = client2.post(
        "/login",
        data={"user_id": str(uid), "password": "task7a-devices-pw"},
        follow_redirects=False,
    )
    assert r2.status_code == 303, r2.text
    token2 = client2.cookies.get(session_mod.SESSION_COOKIE_NAME)
    assert token2

    parsed1 = session_mod.parse_session_token(token1)
    parsed2 = session_mod.parse_session_token(token2)
    assert parsed1 is not None and parsed2 is not None
    assert parsed1[2] != parsed2[2]
    # Precondition: both sessions are live, so the second's survival
    # means the revoke was scoped — not that there was nothing to kill.
    assert sessions_repo.is_session_active(parsed1[2]) is True
    assert sessions_repo.is_session_active(parsed2[2]) is True

    r = client1.get("/logout", follow_redirects=False)
    assert r.status_code == 303

    assert sessions_repo.is_session_active(parsed1[2]) is False
    # `client2.get("/api/me").status_code == 200` fails a blanket
    # UPDATE ... WHERE user_id mutant: both tokens use user_id 987012,
    # so revoking client1's nonce also invalidates client2's cookie.
    assert sessions_repo.is_session_active(parsed2[2]) is True
    assert client2.get("/api/me").status_code == 200


def test_two_nonce_rollout_boundary_logout_leaves_the_other_session_live(
    auth_db,
):
    """The rollout-boundary test uses two live session nonces.

    It sends one explicit Cookie header with token1 followed by token2.
    Starlette's cookie_parser in starlette/requests.py stores the final
    value for a duplicate name, so the handler resolves and revokes
    token2. The test then checks that token1 remains active and still
    authenticates from a separate client.

    This explicit-header setup measures request parsing and server-side
    revocation. The test client does not expose browser-side last-wins
    ordering for cookies received through Set-Cookie headers.
    """
    # Private user_id: 987001-987005, 987010, 987011 and 987012 are taken.
    uid = 987013
    _seed_auth_user(uid, "task7a-rollout-secret", "task7a-rollout-pw")

    # Two real logins provide the distinct nonces used in the explicit header.
    def _login() -> str:
        client = TestClient(real_app)
        r = client.post(
            "/login",
            data={"user_id": str(uid), "password": "task7a-rollout-pw"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        token = client.cookies.get(session_mod.SESSION_COOKIE_NAME)
        assert token
        return token

    token1 = _login()
    token2 = _login()

    parsed1 = session_mod.parse_session_token(token1)
    parsed2 = session_mod.parse_session_token(token2)
    assert parsed1 is not None and parsed2 is not None
    # Two distinct live sessions — without this the test could pass
    # because one session was never valid.
    assert parsed1[2] != parsed2[2]
    assert sessions_repo.is_session_active(parsed1[2]) is True
    assert sessions_repo.is_session_active(parsed2[2]) is True

    # One logout receives an explicit duplicate-name Cookie header. The
    # client has an empty jar, so the request uses that header without jar
    # cookies.
    boundary = TestClient(real_app)
    r = boundary.get(
        "/logout",
        headers={
            "Cookie": (
                f"{session_mod.SESSION_COOKIE_NAME}={token1}; "
                f"{session_mod.SESSION_COOKIE_NAME}={token2}"
            )
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    # The parser-selected nonce is revoked; the other remains usable.
    assert sessions_repo.is_session_active(parsed2[2]) is False
    assert sessions_repo.is_session_active(parsed1[2]) is True
    holder = TestClient(real_app)
    holder.cookies.set(session_mod.SESSION_COOKIE_NAME, token1)
    assert holder.get("/api/me").status_code == 200


def test_guest_cookie_surviving_a_boundary_logout_still_authenticates(
    auth_db, monkeypatch
):
    """Guest-token resolution is independent of authenticated revocation.

    The guest token has no web_sessions row, so revoking the presented
    authenticated nonce does not revoke that guest token. The assertions
    `is_session_active(parsed_guest[2]) is False` and `/api/me == 200`
    measure that distinction before and after the boundary logout.

    The response-header assertions record a domain-keyed deletion and the
    unconditional host-only deletion. The test client cannot establish
    browser-side last-wins ordering; the explicit Cookie header exercises
    the server's duplicate-name parsing.
    """
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", "testserver.local")
    # Private user_id: 987001-987005 and 987010-987015 are taken.
    uid = 987016
    _seed_auth_user(uid, "task7a-guestwit-secret", "task7a-guestwit-pw")

    # The guest cookie is issued with the configured domain. http.cookiejar
    # accepts it for the default testserver client via
    # http.cookiejar.eff_request_host, which maps the bare host to
    # testserver.local.
    guest = TestClient(real_app)
    r = guest.post("/login/guest", follow_redirects=False)
    assert r.status_code == 303, r.text
    guest_token = guest.cookies.get(session_mod.SESSION_COOKIE_NAME)
    assert guest_token
    parsed_guest = session_mod.parse_session_token(guest_token)
    assert parsed_guest is not None
    assert parsed_guest[0] == session_mod.GUEST_USER_ID
    # `sessions_repo.is_session_active(parsed_guest[2]) is False` records
    # that this nonce is not active; tests/test_session.py::test_guest_session_has_no_web_session_row
    # pins the separate no-row fact. `guest.get("/api/me").status_code == 200`
    # records that the cookie authenticates.
    assert sessions_repo.is_session_active(parsed_guest[2]) is False
    assert guest.get("/api/me").status_code == 200

    # The presented session is a real authenticated login. The default
    # testserver host is matched to testserver.local by
    # http.cookiejar.eff_request_host; read the token from the response
    # header for the explicit combined Cookie header.
    client = TestClient(real_app)
    r = client.post(
        "/login",
        data={"user_id": str(uid), "password": "task7a-guestwit-pw"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    jar = SimpleCookie()
    for header in r.headers.get_list("set-cookie"):
        jar.load(header)
    morsel = jar.get(session_mod.SESSION_COOKIE_NAME)
    assert morsel is not None
    presented = morsel.value
    parsed_presented = session_mod.parse_session_token(presented)
    assert parsed_presented is not None
    assert sessions_repo.is_session_active(parsed_presented[2]) is True

    # One boundary logout receives an explicit duplicate-name Cookie header.
    # Starlette's cookie_parser resolves the final occurrence, so `presented`
    # is what the handler resolves and revokes while `guest_token` is the
    # other value. A fresh client sends this explicit header as its Cookie.
    boundary = TestClient(real_app)
    r = boundary.get(
        "/logout",
        headers={
            "Cookie": (
                f"{session_mod.SESSION_COOKIE_NAME}={guest_token}; "
                f"{session_mod.SESSION_COOKIE_NAME}={presented}"
            )
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert sessions_repo.is_session_active(parsed_presented[2]) is False

    # The logout response includes a domain-keyed deletion and an
    # unconditional host-only deletion.
    deletions = [
        h
        for h in r.headers.get_list("set-cookie")
        if h.lower().startswith(f"{session_mod.SESSION_COOKIE_NAME}=")
    ]
    assert any("domain=testserver.local" in h.lower() for h in deletions)
    assert any("domain=" not in h.lower() for h in deletions)

    # After logout, `sessions_repo.is_session_active(parsed_guest[2]) is False`
    # still records that the nonce is not active, while
    # `guest.get("/api/me").status_code == 200` records that the cookie
    # authenticates.
    assert sessions_repo.is_session_active(parsed_guest[2]) is False
    assert guest.get("/api/me").status_code == 200


def test_surviving_cookie_with_an_unrecorded_nonce_does_not_authenticate(
    auth_db,
):
    """A surviving cookie whose nonce was never recorded, measured end to end.

    The surviving cookie is validly signed (same user, same secret as
    the presented session) but its nonce has no web_sessions row and
    never did; is_session_active on it is False before the logout. The
    boundary logout revokes the OTHER (presented) session, and
    afterwards /api/me carrying this cookie returns 401. The expressions
    `sessions_repo.is_session_active(parsed_orphan[2]) is False` and
    `holder.get("/api/me").status_code == 401` measure the unrecorded-nonce
    path against the earlier "ceases to hold only when the surviving cookie
    carries the same nonce" wording: the orphan nonce is a DIFFERENT nonce
    from the one the logout just revoked, and it still fails.
    """
    # Private user_id: 987001-987005 and 987010-987013 are taken.
    uid = 987014
    secret = "task7a-unrecorded-secret"
    _seed_auth_user(uid, secret, "task7a-unrecorded-pw")

    # The presented session: a real login, so a real web_sessions row.
    client = TestClient(real_app)
    r = client.post(
        "/login",
        data={"user_id": str(uid), "password": "task7a-unrecorded-pw"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    presented = client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    assert presented
    parsed_presented = session_mod.parse_session_token(presented)
    assert parsed_presented is not None
    assert sessions_repo.is_session_active(parsed_presented[2]) is True

    # The surviving cookie: VALIDLY SIGNED (same user, same secret) but
    # its nonce has no web_sessions row and never did.
    orphan = session_mod.make_session_token(uid, secret)
    parsed_orphan = session_mod.parse_session_token(orphan)
    assert parsed_orphan is not None
    assert parsed_orphan[2] != parsed_presented[2]
    # Precondition: signature-valid but NOT live. This liveness
    # assertion is what attributes the post-logout 401 below to the
    # missing row rather than to the logout — a pre-logout /api/me
    # assertion could never fail independently of the final one (the
    # boundary logout touches the presented session's row, and the
    # orphan has no row a revocation could change), so it is folded
    # into this one.
    assert sessions_repo.is_session_active(parsed_orphan[2]) is False

    # ONE logout carrying BOTH cookies, as the rollout boundary produces.
    # Starlette's cookie_parser resolves a duplicated name to its LAST
    # occurrence, so `presented` is what the handler resolves and revokes.
    boundary = TestClient(real_app)
    r = boundary.get(
        "/logout",
        headers={
            "Cookie": (
                f"{session_mod.SESSION_COOKIE_NAME}={orphan}; "
                f"{session_mod.SESSION_COOKIE_NAME}={presented}"
            )
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert sessions_repo.is_session_active(parsed_presented[2]) is False

    # The surviving cookie names a DIFFERENT nonce than the one just
    # revoked — and still does not authenticate, because its session
    # has no web_sessions row (asserted above), which the boundary
    # logout cannot change.
    holder = TestClient(real_app)
    holder.cookies.set(session_mod.SESSION_COOKIE_NAME, orphan)
    assert holder.get("/api/me").status_code == 401


def test_surviving_cookie_revoked_earlier_does_not_authenticate(auth_db):
    """A surviving cookie revoked EARLIER by another path, not this logout.

    `tests/test_login.py::test_surviving_cookie_revoked_earlier_does_not_authenticate`
    measures the earlier-revoked path for the "same nonce" wording. Two
    real logins, two live sessions; the first is revoked through the
    account self-service path BEFORE the boundary logout — via
    sessions_repo.revoke_session, the function the
    /account/sessions/revoke endpoint calls (going through the HTTP
    endpoint would add only cookie plumbing). The surviving cookie then
    names a session that is not live, so it must NOT authenticate after
    the logout — even though its nonce is DIFFERENT from the one this
    logout revoked.
    """
    # Private user_id: 987001-987005 and 987010-987014 are taken.
    uid = 987015
    _seed_auth_user(uid, "task7a-earlier-secret", "task7a-earlier-pw")

    def _login() -> str:
        client = TestClient(real_app)
        r = client.post(
            "/login",
            data={"user_id": str(uid), "password": "task7a-earlier-pw"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        token = client.cookies.get(session_mod.SESSION_COOKIE_NAME)
        assert token
        return token

    earlier = _login()
    presented = _login()
    parsed_earlier = session_mod.parse_session_token(earlier)
    parsed_presented = session_mod.parse_session_token(presented)
    assert parsed_earlier is not None and parsed_presented is not None
    assert parsed_earlier[2] != parsed_presented[2]
    assert sessions_repo.is_session_active(parsed_earlier[2]) is True
    assert sessions_repo.is_session_active(parsed_presented[2]) is True

    holder = TestClient(real_app)
    holder.cookies.set(session_mod.SESSION_COOKIE_NAME, earlier)
    # Precondition: the surviving cookie really authenticates before the
    # earlier revocation, or the final 401 proves nothing.
    assert holder.get("/api/me").status_code == 200

    # The EARLIER revocation, by another path than the logout below.
    assert sessions_repo.revoke_session(uid, parsed_earlier[2]) is True
    assert sessions_repo.is_session_active(parsed_earlier[2]) is False
    assert sessions_repo.is_session_active(parsed_presented[2]) is True

    # The boundary logout revokes the OTHER (presented) session.
    boundary = TestClient(real_app)
    r = boundary.get(
        "/logout",
        headers={
            "Cookie": (
                f"{session_mod.SESSION_COOKIE_NAME}={earlier}; "
                f"{session_mod.SESSION_COOKIE_NAME}={presented}"
            )
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert sessions_repo.is_session_active(parsed_presented[2]) is False

    # `holder.get("/api/me").status_code == 401` records that the surviving
    # cookie names a different, EARLIER-REVOKED session and does not
    # authenticate.
    assert holder.get("/api/me").status_code == 401
