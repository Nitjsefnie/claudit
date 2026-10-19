import copy
import secrets
import threading
import time as time_mod

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import auth
from backend import login as login_mod
from backend import session as session_mod
from tests import scratch_db
from tests.test_login_a11y import (
    _assert_generic_failure,
)

_ORIGIN = {"Origin": "http://testserver"}

# An 18-digit id — the auth DB's real user_id shape (bigint), far above
# the 2^31 an INTEGER column holds.
_BIG_UID = 123456789012345678


def _post_login(client, user_id, password, follow_redirects=False):
    return client.post(
        "/login",
        data={"user_id": str(user_id), "password": password},
        headers=_ORIGIN,
        follow_redirects=follow_redirects,
    )


def _clear_user_config_cache():
    session_mod._USER_CONFIG_CACHE.clear()  # pylint: disable=protected-access


def _signed_in_client(app):
    client = TestClient(app)
    assert _post_login(client, 12345, "hunter2").status_code == 303
    return client


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """The rate-limit dict is process-global; clear it around each test
    so limiter cases never leak failures into neighbouring cases on the
    same TestClient host."""
    login_mod.reset_login_rate_limits()
    _clear_user_config_cache()
    yield
    login_mod.reset_login_rate_limits()
    _clear_user_config_cache()


@pytest.fixture(name="app")
def _app_fixture():
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
    rows: dict[int, tuple[str, int, str | None]] = {}

    def _get_or_create(user_id, cred_fp):
        row = rows.get(user_id)
        new_secret = secrets.token_urlsafe(32)
        if row is None:
            secret, generation = new_secret, 0
        elif row[2] != cred_fp:
            secret, generation = new_secret, row[1] + 1
        else:
            secret, generation = row[:2]
        rows[user_id] = (secret, generation, cred_fp)
        return secret, generation

    def _load(user_id):
        return rows.get(user_id)

    def _bump(user_id):
        if user_id in rows:
            secret, generation, cred_fp = rows[user_id]
            rows[user_id] = (secret, generation + 1, cred_fp)

    monkeypatch.setattr(
        session_mod, "get_or_create_session_row", _get_or_create)
    monkeypatch.setattr(session_mod, "load_session_row", _load)
    monkeypatch.setattr(session_mod, "bump_session_generation", _bump)
    return rows


@pytest.fixture(name="fake_user")
def _fake_user_fixture(monkeypatch):
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


@pytest.mark.parametrize("path, data, statuses", [
    ("/login", {"user_id": "12345", "password": "hunter2"}, (302, 303)),
    ("/login/guest", None, (303,)),
], ids=["password", "guest"])
def test_login_sets_cookie_contract(app, fake_user, fake_session_store, path, data, statuses):
    response = TestClient(app).post(
        path, data=data, headers=_ORIGIN, follow_redirects=False,
    )
    assert response.status_code in statuses
    assert session_mod.SESSION_COOKIE_NAME in response.cookies
    _assert_session_cookie_contract(response)


def test_login_verification_runs_off_event_loop_thread(
    app, fake_user, fake_session_store, monkeypatch
):
    event_loop_threads: list[int] = []
    verification_threads: list[int] = []

    @app.middleware("http")
    async def capture_event_loop_thread(request, call_next):
        event_loop_threads.append(threading.get_ident())
        return await call_next(request)
    real_verify = auth.verify_web_password

    def capture_verification_thread(config: dict, password: str) -> bool:
        verification_threads.append(threading.get_ident())
        return real_verify(config, password)
    monkeypatch.setattr(auth, "verify_web_password", capture_verification_thread)
    r = _post_login(TestClient(app), 12345, "hunter2")
    assert r.status_code == 303
    assert len(event_loop_threads) == 1
    assert len(verification_threads) == 1
    assert verification_threads[0] != event_loop_threads[0]


@pytest.mark.parametrize("path, data", [
    ("/login", {"user_id": "12345", "password": "hunter2"}),
    ("/login/guest", None),
], ids=["password", "guest"])
def test_cross_origin_login_post_is_403(app, fake_user, fake_session_store, path, data):
    response = TestClient(app).post(
        path, data=data, headers={"Origin": "https://attacker.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "cross-origin" in response.text


def test_login_post_without_origin_is_403(app, fake_user):
    """Scripted clients must now declare an Origin; browsers always do."""
    r = TestClient(app).post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code == 403


@pytest.mark.parametrize("locked, status", [(False, 401), (True, 429)],
                         ids=["invalid-credentials", "rate-limited"])
def test_all_credential_failures_answer_identically(app, fake_user, locked, status):
    fake_user[777] = {}
    client = TestClient(app)
    attempts = [(999, "anything"), (777, "anything"), (12345, "wrong")]
    if locked:
        attempts = [(12345, "x"), (999, "x")]
    responses = []
    for user_id, password in attempts:
        if locked:
            for _ in range(5):
                _post_login(client, user_id, password)
        responses.append(_post_login(client, user_id, password))
    assert {response.status_code for response in responses} == {status}
    assert len({response.content for response in responses}) == 1
    if not locked:
        _assert_generic_failure(responses[0])


def test_normalization_runs_where_real_one_cannot(
    app, fake_user, fake_session_store, monkeypatch
):
    """The timing normalizer is consulted on every credential failure —
    from zero where nothing ran (unknown id, no configured password),
    with the spent count on the wrong-password path, where it tops up
    only a remainder — and never on a success."""
    calls: list[int] = []
    monkeypatch.setattr(
        auth, "normalize_verification_timing",
        lambda pw, spent: calls.append(spent),
    )
    store = fake_user
    store[777] = {}
    client = TestClient(app)
    _post_login(client, 999, "anything")    # unknown id
    _post_login(client, 777, "whatever")    # no password configured
    assert calls == [0, 0]
    _post_login(client, 12345, "wrong")     # real 600k verification runs
    assert calls == [0, 0, auth.PBKDF2_WRITE_ITERATIONS]
    _post_login(client, 12345, "hunter2")   # success
    assert calls == [0, 0, auth.PBKDF2_WRITE_ITERATIONS]


@pytest.mark.parametrize("stored_hash, stored_salt, spent", [
    (None, "11" * 16, auth.PBKDF2_ITERATIONS),
    ("ab" * 32, "not-hex", 0),
    (f"pbkdf2_sha256$abc${'22' * 16}${'ab' * 32}", "22" * 16, 0),
    (123, "ff" * 16, 0),
    ("ab" * 32, 123, 0),
], ids=["legacy-wrong-password", "non-hex-salt", "malformed-versioned-hash",
        "integer-hash", "integer-salt"])
def test_stored_credential_failure_normalizes_spent_work(
    app, fake_user, monkeypatch, stored_hash, stored_salt, spent
):
    if stored_hash is None:
        stored_hash = auth.pbkdf2("pw", stored_salt, auth.PBKDF2_ITERATIONS)
    fake_user[555] = {
        auth.WEB_PASSWORD_HASH_KEY: stored_hash,
        auth.WEB_PASSWORD_SALT_KEY: stored_salt,
    }
    calls: list[int] = []
    monkeypatch.setattr(auth, "normalize_verification_timing",
                        lambda password, count: calls.append(count))
    response = _post_login(
        TestClient(app, raise_server_exceptions=False), 555, "anything"
    )
    _assert_generic_failure(response)
    assert calls == [spent]


def test_wrong_password_at_target_spends_nothing_extra(
    app, fake_user, monkeypatch
):
    """Steady state: one wrong-password attempt against a modern
    (write-count) hash runs exactly ONE PBKDF2 — the real verification.
    The normalizer is skipped because the target is already spent, and
    the reference at the target is seeded at import."""
    seen: list[int] = []
    real = auth.pbkdf2

    def spy(password: str, salt_hex: str, iterations: int) -> str:
        seen.append(iterations)
        return real(password, salt_hex, iterations)

    monkeypatch.setattr(auth, "pbkdf2", spy)
    client = TestClient(app)
    r = _post_login(client, 12345, "wrong")
    assert r.status_code == 401
    assert seen == [auth.PBKDF2_WRITE_ITERATIONS]


def test_failures_for_one_user_do_not_lock_another(app, fake_user, fake_session_store):
    """the limiter is keyed per (ip, user) pair. User B's correct
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


@pytest.mark.parametrize("limiter", ["pair", "ip"], ids=["pair-limit", "ip-limit"])
def test_limited_429_does_not_reach_auth_work(app, fake_user, monkeypatch, limiter):
    client = TestClient(app)
    if limiter == "pair":
        for _ in range(5):
            _post_login(client, 12345, "x")
    else:
        failures = login_mod._LOGIN_IP_FAILURES  # pylint: disable=protected-access
        failures["testclient"] = [
            time_mod.time()
        ] * login_mod._LOGIN_MAX_IP_FAILURES  # pylint: disable=protected-access
    calls: list[int] = []
    monkeypatch.setattr(auth, "normalize_verification_timing",
                        lambda password, spent: calls.append(spent))
    monkeypatch.setattr(
        session_mod, "load_user_config",
        lambda uid: pytest.fail("429 must not reach the auth DB"),
    )
    response = _post_login(client, 12345, "x")
    assert response.status_code == 429
    assert not calls
    assert response.headers["content-type"].startswith("text/html")
    assert ('<div class="err" role="alert">Too many login attempts. '
            'Try again later.</div>') in response.text


@pytest.mark.parametrize("limiter", ["pair", "ip"], ids=["pair-limit", "ip-limit"])
def test_empty_key_is_dropped_on_access(limiter):
    if limiter == "pair":
        failures = login_mod._LOGIN_FAILURES  # pylint: disable=protected-access
        key = "192.0.2.1:7"
        failures[key] = []
        login_mod._check_login_rate_limit("192.0.2.1", 7)  # pylint: disable=protected-access
    else:
        failures = login_mod._LOGIN_IP_FAILURES  # pylint: disable=protected-access
        key = "192.0.2.1"
        failures[key] = []
        login_mod._check_login_ip_rate_limit("192.0.2.1")  # pylint: disable=protected-access
    assert key not in failures


def test_eviction_sweeps_aged_keys_when_over_cap():
    """Above _LOGIN_MAX_KEYS, a record/prune pass sweeps every key whose
    window has expired — the dict never grows without bound."""
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


def test_ip_rate_limit_counts_rotating_user_ids():
    ip = "192.0.2.50"
    for uid in range(1, 6):
        for _ in range(5):
            login_mod._record_login_failure(ip, uid)  # pylint: disable=protected-access
            login_mod._record_login_ip_failure(ip)  # pylint: disable=protected-access

    assert not login_mod._check_login_rate_limit(ip, 6)  # pylint: disable=protected-access
    assert login_mod._check_login_ip_rate_limit(ip)  # pylint: disable=protected-access
    assert not login_mod._check_login_rate_limit("192.0.2.51", 6)  # pylint: disable=protected-access
    assert not login_mod._check_login_ip_rate_limit("192.0.2.51")  # pylint: disable=protected-access


def test_ip_limiter_sweeps_aged_keys_when_over_cap():
    now = time_mod.time()
    failures = login_mod._LOGIN_IP_FAILURES  # pylint: disable=protected-access
    aged = now - login_mod._LOGIN_WINDOW_SECONDS - 1  # pylint: disable=protected-access
    for i in range(login_mod._LOGIN_MAX_IP_KEYS + 1):  # pylint: disable=protected-access
        failures[f"10.1.{i // 256}.{i % 256}"] = [aged]
    failures["198.51.100.7"] = [now]

    login_mod._record_login_ip_failure("198.51.100.8")  # pylint: disable=protected-access

    assert not any(key.startswith("10.1.") for key in failures)
    assert failures["198.51.100.7"] == [now]
    assert len(failures["198.51.100.8"]) == 1


def test_successful_login_does_not_write_the_auth_db(
    app, fake_user, fake_session_store
):
    """ regression: a successful login must leave the shared users
    table unchanged — the session secret went to claudit's own store,
    and no write path to the auth DB exists at all any more."""
    before = copy.deepcopy(fake_user[12345])
    client = TestClient(app)
    r = _post_login(client, 12345, "hunter2")
    assert r.status_code in (302, 303)
    assert fake_user[12345] == before
    assert "web_session_secret" not in fake_user[12345]
    assert not hasattr(session_mod, "write_user_config")


@pytest.mark.parametrize("state", ["user", "guest", "no-cookie", "store-down"],
                         ids=["signed-in", "guest", "no-cookie", "store-down"])
def test_logout_clears_cookie(app, fake_user, fake_session_store, monkeypatch, state):
    client = TestClient(app)
    if state in ("user", "store-down"):
        _post_login(client, 12345, "hunter2")
    elif state == "guest":
        client.post("/login/guest", headers=_ORIGIN, follow_redirects=False)
    if state == "store-down":
        assert client.get("/api/me").status_code == 200

        def store_down(user_id):
            raise RuntimeError("session store unavailable")

        monkeypatch.setattr(session_mod, "bump_session_generation", store_down)
    response = client.get("/logout", follow_redirects=False)
    assert response.status_code == 303
    set_cookie = "; ".join(response.headers.get_list("set-cookie")).lower()
    assert session_mod.SESSION_COOKIE_NAME in set_cookie
    assert "max-age=0" in set_cookie
    if state in ("guest", "no-cookie"):
        assert fake_session_store == {}
    elif state == "store-down":
        assert fake_session_store[12345][1] == 0


def test_logout_bumps_generation_and_invalidates_the_session(
    app, fake_user, fake_session_store
):
    """logout is server-side. The bumped generation kills every
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


def test_session_cookie_round_trip(app, fake_user, fake_session_store):
    client = TestClient(app)
    _post_login(client, 12345, "hunter2")
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json() == {"user_id": 12345}


@pytest.mark.parametrize("recreate", [False, True], ids=["deleted", "recreated"])
def test_deleted_auth_user_invalidates_session(
    app, fake_user, fake_session_store, recreate
):
    client = _signed_in_client(app)
    del fake_user[12345]
    if recreate:
        recreated = {}
        auth.set_web_password(recreated, "different password")
        fake_user[12345] = recreated
    _clear_user_config_cache()
    assert client.get("/api/me").status_code == 401


def test_login_after_password_change_rebinds_session_immediately(
    app, fake_user, fake_session_store
):
    client = _signed_in_client(app)
    assert client.get("/api/me").status_code == 200
    auth.set_web_password(fake_user[12345], "new password")
    assert _post_login(client, 12345, "new password").status_code == 303
    assert client.get("/api/me").status_code == 200


@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    """A fresh claudit DB with the startup schema applied — the real
    user_session table the big-id login writes through."""
    yield from scratch_db.scratch_viz_database(monkeypatch, "login")


def test_login_with_an_18_digit_user_id_round_trips(app, fake_user, fresh_db):
    """a real (non-guest) login whose user id needs bigint —
    the auth DB's actual shape — must persist its user_session row, mint
    the cookie, and have that cookie resolve back through the REAL store
    to the same id."""
    fake_user[_BIG_UID] = {}
    auth.set_web_password(fake_user[_BIG_UID], "hunter2")
    client = TestClient(app)
    r = _post_login(client, _BIG_UID, "hunter2")
    assert r.status_code in (302, 303)
    assert session_mod.SESSION_COOKIE_NAME in r.cookies
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json() == {"user_id": _BIG_UID}
