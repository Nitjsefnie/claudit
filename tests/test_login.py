"""Login flow tests.

Covers: one generic credential failure (issue #109), the dummy PBKDF2
verification run where the real one cannot, the malformed-id 400 kept
distinct, the per-(ip, user) rate limiter with eviction (issue #111),
and the session-secret store (issues #94, #108) — a successful login
touches nothing in the shared auth DB, and logout invalidates the
signed-in user's sessions server-side.
"""
import copy
import secrets
import time as time_mod

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import auth
from backend import login as login_mod
from backend import session as session_mod

_ORIGIN = {"Origin": "http://testserver"}


def _post_login(client, user_id, password, follow_redirects=False):
    return client.post(
        "/login",
        data={"user_id": str(user_id), "password": password},
        headers=_ORIGIN,
        follow_redirects=follow_redirects,
    )


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """The rate-limit dict is process-global; clear it around each test
    so limiter cases never leak failures into neighbouring cases on the
    same TestClient host."""
    login_mod.reset_login_rate_limits()
    yield
    login_mod.reset_login_rate_limits()


@pytest.fixture(name="app")
def _app_fixture():
    """Build a fresh FastAPI app per test, with the auth DB mocked."""
    a = FastAPI()
    a.middleware("http")(session_mod.auth_middleware)
    a.include_router(login_mod.router)

    @a.get("/api/me")
    def me(request: Request):
        return {"user_id": request.state.user_id}

    return a


@pytest.fixture(name="fake_session_store")
def _fake_session_store_fixture(monkeypatch):
    """In-memory stand-in for claudit's user_session table, wired into
    the flow through the same two functions the real store backs."""
    rows: dict[int, tuple[str, int]] = {}

    def _get_or_create(user_id):
        return rows.setdefault(user_id, (secrets.token_urlsafe(32), 0))

    def _load(user_id):
        return rows.get(user_id)

    def _bump(user_id):
        if user_id in rows:
            secret, generation = rows[user_id]
            rows[user_id] = (secret, generation + 1)

    monkeypatch.setattr(
        session_mod, "get_or_create_session_row", _get_or_create)
    monkeypatch.setattr(session_mod, "load_session_row", _load)
    monkeypatch.setattr(session_mod, "bump_session_generation", _bump)
    return rows


@pytest.fixture(name="fake_user")
def _fake_user_fixture(monkeypatch):
    """Stub the auth DB with one user that has a known password."""
    config: dict = {}
    auth.set_web_password(config, "hunter2")
    store = {12345: config}

    def _load(user_id):
        return store.get(user_id)

    monkeypatch.setattr(session_mod, "load_user_config", _load)
    return store


def test_login_page_is_html(app):
    client = TestClient(app)
    r = client.get("/login")
    assert r.status_code == 200
    assert "<form" in r.text and "user_id" in r.text


def _assert_session_cookie_contract(response):
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=strict" in header
    assert f"max-age={session_mod.SESSION_COOKIE_MAX_AGE}" in header
    assert "path=/" in header
    assert "domain=" not in header


def test_successful_login_sets_cookie(app, fake_user, fake_session_store):
    client = TestClient(app)
    r = client.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        headers=_ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    assert session_mod.SESSION_COOKIE_NAME in r.cookies
    _assert_session_cookie_contract(r)


def test_guest_login_sets_same_cookie_contract(app):
    r = TestClient(app).post(
        "/login/guest",
        headers=_ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert session_mod.SESSION_COOKIE_NAME in r.cookies
    _assert_session_cookie_contract(r)


def test_cross_origin_login_post_is_403(app, fake_user):
    r = TestClient(app).post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        headers={"Origin": "https://attacker.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "cross-origin" in r.text


def test_cross_origin_guest_login_is_403(app):
    r = TestClient(app).post(
        "/login/guest",
        headers={"Origin": "https://attacker.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "cross-origin" in r.text


def test_login_post_without_origin_is_403(app, fake_user):
    """Scripted clients must now declare an Origin; browsers always do."""
    r = TestClient(app).post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_wrong_password_is_generic_401(app, fake_user):
    client = TestClient(app)
    r = _post_login(client, 12345, "wrong")
    assert r.status_code == 401
    assert r.text == "Invalid credentials."


def test_unknown_user_is_generic_401(app, fake_user):
    """An unknown id answers exactly what a wrong password answers —
    not its own status or body (issue #109)."""
    client = TestClient(app)
    r = _post_login(client, 999, "anything")
    assert r.status_code == 401
    assert r.text == "Invalid credentials."


def test_no_password_user_is_generic_401(app, fake_user):
    store = fake_user
    store[777] = {}  # known id, no web password configured
    client = TestClient(app)
    r = _post_login(client, 777, "anything")
    assert r.status_code == 401
    assert r.text == "Invalid credentials."


def test_all_credential_failures_answer_identically(app, fake_user):
    """#109: unknown id, no configured password and wrong password must
    be indistinguishable — identical status AND identical body."""
    store = fake_user
    store[777] = {}
    client = TestClient(app)
    modes = [
        _post_login(client, 999, "anything"),   # unknown id
        _post_login(client, 777, "anything"),   # no password configured
        _post_login(client, 12345, "wrong"),    # wrong password
    ]
    statuses = {r.status_code for r in modes}
    bodies = {r.text for r in modes}
    assert statuses == {401}
    assert bodies == {"Invalid credentials."}


def test_dummy_verification_runs_where_real_one_cannot(
    app, fake_user, fake_session_store, monkeypatch
):
    """The dummy helper is invoked on the unknown-id and no-password
    paths, and nowhere else (the real verification runs instead)."""
    calls: list[str] = []
    monkeypatch.setattr(auth, "run_dummy_verification", calls.append)
    store = fake_user
    store[777] = {}
    client = TestClient(app)
    _post_login(client, 999, "anything")    # unknown id
    assert len(calls) == 1
    _post_login(client, 777, "whatever")    # no password configured
    assert len(calls) == 2
    _post_login(client, 12345, "wrong")     # real verification runs
    assert len(calls) == 2
    _post_login(client, 12345, "hunter2")   # successful login
    assert len(calls) == 2


def test_malformed_user_id_is_still_400(app, fake_user):
    """The malformed-id 400 reveals nothing about accounts and stays."""
    client = TestClient(app)
    for bad in ("abc", "", "0", "-3"):
        r = _post_login(client, bad, "bad id")
        assert r.status_code == 400
        assert r.text == "Invalid user ID"


def test_rate_limit_after_5_failures(app, fake_user):
    client = TestClient(app)
    for _ in range(5):
        _post_login(client, 12345, "x")
    r = _post_login(client, 12345, "x")
    assert r.status_code == 429


def test_failures_for_one_user_do_not_lock_another(app, fake_user, fake_session_store):
    """#111: the limiter is keyed per (ip, user) pair. User B's correct
    login survives user A's failures; user A's own pair stays locked."""
    store = fake_user
    store[777] = {}
    auth.set_web_password(store[777], "correct horse")
    client = TestClient(app)
    for _ in range(5):
        _post_login(client, 12345, "wrong")
    r = _post_login(client, 777, "correct horse")
    assert r.status_code in (302, 303)
    r = _post_login(client, 12345, "hunter2")
    assert r.status_code == 429


def test_every_failure_mode_counts_toward_the_limit(app, fake_user):
    """Failure accounting covers all three generic-401 modes."""
    store = fake_user
    store[777] = {}  # no password configured
    client = TestClient(app)
    for mode_uid, password in ((999, "x"), (777, "x")):
        login_mod.reset_login_rate_limits()
        for _ in range(5):
            _post_login(client, mode_uid, password)
        r = _post_login(client, mode_uid, password)
        assert r.status_code == 429, mode_uid


def test_locked_pair_429_does_not_burn_pbkdf2(app, fake_user, monkeypatch):
    """The 429 path must stay cheap — no dummy run, no DB hit."""
    calls: list[str] = []
    monkeypatch.setattr(auth, "run_dummy_verification", calls.append)
    client = TestClient(app)
    for _ in range(5):
        _post_login(client, 12345, "x")
    monkeypatch.setattr(
        session_mod, "load_user_config",
        lambda uid: pytest.fail("429 must not reach the auth DB"),
    )
    r = _post_login(client, 12345, "x")
    assert r.status_code == 429
    assert not calls


def test_empty_key_is_dropped_on_access():
    login_mod._LOGIN_FAILURES["192.0.2.1:7"] = []  # pylint: disable=protected-access
    login_mod._check_login_rate_limit("192.0.2.1", 7)  # pylint: disable=protected-access
    assert "192.0.2.1:7" not in login_mod._LOGIN_FAILURES  # pylint: disable=protected-access


def test_eviction_sweeps_aged_keys_when_over_cap():
    """Above _LOGIN_MAX_KEYS, a record/prune pass sweeps every key whose
    window has expired — the dict never grows without bound (#111)."""
    now = time_mod.time()
    failures = login_mod._LOGIN_FAILURES  # pylint: disable=protected-access
    aged = now - login_mod._LOGIN_WINDOW_SECONDS - 1  # pylint: disable=protected-access
    for i in range(login_mod._LOGIN_MAX_KEYS + 1):  # pylint: disable=protected-access
        failures[f"10.0.0.{i}:1"] = [aged]
    failures["198.51.100.7:2"] = [now]
    login_mod._record_login_failure("198.51.100.7", 3)  # pylint: disable=protected-access
    assert not any(k.startswith("10.0.0.") for k in failures)
    assert failures["198.51.100.7:2"] == [now]
    recorded = failures["198.51.100.7:3"]
    assert len(recorded) == 1 and recorded[0] >= now


def test_under_cap_no_sweep_runs():
    """Below the cap only the touched key is pruned — aged entries
    elsewhere survive (the sweep is not a standing full scan)."""
    now = time_mod.time()
    failures = login_mod._LOGIN_FAILURES  # pylint: disable=protected-access
    failures["198.51.100.7:2"] = [now - 10_000]
    login_mod._record_login_failure("198.51.100.7", 3)  # pylint: disable=protected-access
    assert failures["198.51.100.7:2"] == [now - 10_000]


def test_successful_login_does_not_write_the_auth_db(
    app, fake_user, fake_session_store
):
    """#94 regression: a successful login must leave the shared users
    table unchanged — the session secret went to claudit's own store,
    and no write path to the auth DB exists at all any more."""
    before = copy.deepcopy(fake_user[12345])
    client = TestClient(app)
    r = _post_login(client, 12345, "hunter2")
    assert r.status_code in (302, 303)
    assert fake_user[12345] == before
    assert "web_session_secret" not in fake_user[12345]
    assert not hasattr(session_mod, "write_user_config")


def test_logout_clears_cookie(app, fake_user, fake_session_store):
    client = TestClient(app)
    _post_login(client, 12345, "hunter2")
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert any(
        session_mod.SESSION_COOKIE_NAME in v
        for v in r.headers.get_list("set-cookie")
    )


def test_logout_bumps_generation_and_invalidates_the_session(
    app, fake_user, fake_session_store
):
    """#108: logout is server-side. The bumped generation kills every
    token the user holds, so a CAPTURED cookie — the threat model — no
    longer verifies, not just the one the logout response clears."""
    client = TestClient(app)
    _post_login(client, 12345, "hunter2")
    captured = client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    assert captured
    assert client.get("/api/me").status_code == 200

    r = client.get("/logout", follow_redirects=False)

    assert r.status_code in (302, 303)
    assert fake_session_store[12345][1] == 1
    client.cookies.set(session_mod.SESSION_COOKIE_NAME, captured)
    assert client.get("/api/me").status_code == 401


def test_logout_with_guest_cookie_does_not_touch_user_session(
    app, fake_session_store
):
    client = TestClient(app)
    client.post("/login/guest", headers=_ORIGIN, follow_redirects=False)
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert fake_session_store == {}


def test_logout_with_no_cookie_is_a_plain_redirect(app, fake_session_store):
    r = TestClient(app).get("/logout", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert any(
        session_mod.SESSION_COOKIE_NAME in v
        for v in r.headers.get_list("set-cookie")
    )
    assert fake_session_store == {}


def test_session_cookie_round_trip(app, fake_user, fake_session_store):
    client = TestClient(app)
    _post_login(client, 12345, "hunter2")
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json() == {"user_id": 12345}
