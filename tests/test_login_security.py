"""Login security regressions that exercise limiter and cookie state."""
import asyncio
import secrets
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import auth
from backend import login as login_mod
from backend import session as session_mod

_ORIGIN = {"Origin": "http://testserver"}


def _post_login(client, user_id, password):
    return client.post(
        "/login",
        data={"user_id": str(user_id), "password": password},
        headers=_ORIGIN,
        follow_redirects=False,
    )


def _clear_user_config_cache():
    session_mod._USER_CONFIG_CACHE.clear()  # pylint: disable=protected-access


@pytest.fixture(autouse=True)
def _reset_login_state():
    login_mod.reset_login_rate_limits()
    _clear_user_config_cache()
    yield
    login_mod.reset_login_rate_limits()
    _clear_user_config_cache()


@pytest.fixture(name="app")
def _app_fixture():
    app = FastAPI()
    app.middleware("http")(session_mod.auth_middleware)
    app.include_router(login_mod.router)

    @app.get("/api/me")
    def me(request: Request):
        return {"user_id": request.state.user_id}

    return app


@pytest.fixture(name="fake_user")
def _fake_user_fixture(monkeypatch):
    config: dict = {}
    auth.set_web_password(config, "hunter2")
    store = {12345: config}
    monkeypatch.setattr(session_mod, "load_user_config", store.get)
    return store


@pytest.fixture(name="fake_session_store")
def _fake_session_store_fixture(monkeypatch):
    rows: dict[int, tuple[str, int, str | None]] = {}

    def get_or_create(user_id, cred_fp):
        row = rows.get(user_id)
        fresh_secret = secrets.token_urlsafe(32)
        if row is None:
            secret, generation = fresh_secret, 0
        elif row[2] != cred_fp:
            secret, generation = fresh_secret, row[1] + 1
        else:
            secret, generation = row[:2]
        rows[user_id] = (secret, generation, cred_fp)
        return secret, generation

    def bump_generation(user_id):
        if user_id in rows:
            secret, generation, cred_fp = rows[user_id]
            rows[user_id] = (secret, generation + 1, cred_fp)

    monkeypatch.setattr(
        session_mod, "get_or_create_session_row", get_or_create
    )
    monkeypatch.setattr(session_mod, "load_session_row", rows.get)
    monkeypatch.setattr(
        session_mod, "bump_session_generation", bump_generation
    )
    return rows


def _signed_in_client(app):
    client = TestClient(app)
    assert _post_login(client, 12345, "hunter2").status_code == 303
    return client


def _raw_login_request(ip="198.51.100.91"):
    return Request({
        "type": "http",
        "method": "POST",
        "headers": [],
        "client": (ip, 1234),
    })


def test_stale_password_login_cannot_rebind_after_new_password_login(
    fake_user, fake_session_store, monkeypatch
):  # pylint: disable=too-many-locals
    """A paused old-password login cannot overwrite a newer credential bind."""
    old_config = dict(fake_user[12345])
    new_config: dict = {}
    auth.set_web_password(new_config, "new password")
    old_started = threading.Event()
    release_old = threading.Event()
    normalization_spent: list[int] = []

    def controlled_verify(config, password):
        if password == "old password":
            old_started.set()
            if not release_old.wait(timeout=5):
                raise TimeoutError("old login barrier was not released")
            return True
        return password == "new password" and config is new_config

    monkeypatch.setattr(auth, "verify_web_password", controlled_verify)
    monkeypatch.setattr(
        auth,
        "normalize_verification_timing",
        lambda password, spent: normalization_spent.append(spent),
    )

    async def run_race():
        old_task = asyncio.create_task(login_mod.login_post(
            _raw_login_request(), user_id="12345", password="old password"
        ))
        try:
            assert await asyncio.to_thread(old_started.wait, 5)
            fake_user[12345] = new_config
            new_response = await login_mod.login_post(
                _raw_login_request(),
                user_id="12345",
                password="new password",
            )
            new_cookie = new_response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
            release_old.set()
            old_response = await old_task
            return new_response, new_cookie, old_response
        finally:
            release_old.set()
            if not old_task.done():
                await old_task

    new_response, new_cookie, old_response = asyncio.run(run_race())

    assert new_response.status_code == 303
    assert old_response.status_code == 401
    assert old_response.body == b"Invalid credentials."
    assert "set-cookie" not in old_response.headers
    assert normalization_spent == [600_000]
    assert fake_session_store[12345][2] == session_mod.credential_fingerprint(
        new_config
    )
    assert session_mod.resolve_session_user_id(new_cookie) == 12345


@pytest.mark.parametrize(
    ("stored_hash", "stored_salt"),
    [
        (123, "ff" * 16),
        ("ab" * 32, 123),
    ],
    ids=("integer-hash", "integer-salt"),
)
def test_non_string_credential_material_is_generic_and_normalizes_from_zero(
    app, fake_user, monkeypatch, stored_hash, stored_salt
):
    fake_user[559] = {
        auth.WEB_PASSWORD_HASH_KEY: stored_hash,
        auth.WEB_PASSWORD_SALT_KEY: stored_salt,
    }
    calls: list[int] = []
    monkeypatch.setattr(
        auth,
        "normalize_verification_timing",
        lambda password, spent: calls.append(spent),
    )
    response = _post_login(
        TestClient(app, raise_server_exceptions=False), 559, "anything"
    )

    assert response.status_code == 401
    assert response.text == "Invalid credentials."
    assert calls == [0]


def test_password_change_relogin_does_not_resurrect_old_cookie(
    app, fake_user, fake_session_store
):
    client = _signed_in_client(app)
    old_cookie = client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    assert old_cookie

    auth.set_web_password(fake_user[12345], "new password")
    _clear_user_config_cache()
    old_client = TestClient(app)
    assert old_client.get(
        "/api/me", headers={"Cookie": f"session={old_cookie}"}
    ).status_code == 401

    login = _post_login(client, 12345, "new password")
    assert login.status_code == 303
    new_cookie = login.cookies.get(session_mod.SESSION_COOKIE_NAME)
    assert new_cookie and new_cookie != old_cookie

    old_after_relogin = old_client.get(
        "/api/me", headers={"Cookie": f"session={old_cookie}"}
    )
    assert old_after_relogin.status_code == 401
    new_client = TestClient(app)
    assert new_client.get(
        "/api/me", headers={"Cookie": f"session={new_cookie}"}
    ).status_code == 200


@pytest.mark.parametrize(
    ("user_ids", "seed_pair", "seed_ip", "expected_admissions"),
    [
        ([12345] * 25, 2, 3, 3),
        (list(range(200, 225)), 0, 3, 17),
    ],
)
def test_concurrent_login_reservations_enforce_pair_and_ip_caps(
    user_ids, seed_pair, seed_ip, expected_admissions, monkeypatch
):  # pylint: disable=too-many-locals,too-many-statements
    """Concurrent requests reserve slots until their blocked verify ends."""
    ip = "198.51.100.90"
    now = time.time()
    if seed_pair:
        pair_key = login_mod._failure_key(  # pylint: disable=protected-access
            ip, user_ids[0]
        )
        login_mod._LOGIN_FAILURES[pair_key] = [now] * seed_pair  # pylint: disable=protected-access
    if seed_ip:
        login_mod._LOGIN_IP_FAILURES[ip] = [now] * seed_ip  # pylint: disable=protected-access

    release_workers = threading.Event()
    worker_lock = threading.Lock()
    worker_state = {"started": 0}
    admissions = {"total": 0, "by_user": {}}
    config = {
        auth.WEB_PASSWORD_HASH_KEY: "stored-hash",
        auth.WEB_PASSWORD_SALT_KEY: "stored-salt",
    }

    def track_auth_load(uid):
        admissions["total"] += 1
        by_user = admissions["by_user"]
        by_user[uid] = by_user.get(uid, 0) + 1
        return config

    monkeypatch.setattr(session_mod, "load_user_config", track_auth_load)
    monkeypatch.setattr(
        auth, "normalize_verification_timing", lambda password, spent: None
    )

    async def drive_burst():
        all_checks_done = asyncio.Event()
        pair_checks = 0
        original_check = login_mod._check_login_rate_limit  # pylint: disable=protected-access

        def tracked_pair_check(check_ip, uid):
            nonlocal pair_checks
            limited = original_check(check_ip, uid)
            pair_checks += 1
            if pair_checks == len(user_ids):
                all_checks_done.set()
            return limited

        def blocking_verification(_config, _password):
            with worker_lock:
                worker_state["started"] += 1
            if not release_workers.wait(timeout=10):
                raise TimeoutError("login verification barrier was not released")
            return False

        monkeypatch.setattr(
            login_mod, "_check_login_rate_limit", tracked_pair_check
        )
        monkeypatch.setattr(auth, "verify_web_password", blocking_verification)
        tasks = [
            asyncio.create_task(login_mod.login_post(
                Request({
                    "type": "http",
                    "method": "POST",
                    "headers": [],
                    "client": (ip, 1234),
                }),
                user_id=str(uid),
                password=str(uid),
            ))
            for uid in user_ids
        ]
        try:
            await asyncio.wait_for(all_checks_done.wait(), timeout=5)
            await asyncio.sleep(0)
        finally:
            release_workers.set()
        return await asyncio.gather(*tasks)

    responses = asyncio.run(drive_burst())
    assert admissions["total"] == expected_admissions
    assert worker_state["started"] == expected_admissions
    assert sum(response.status_code == 429 for response in responses) == (
        len(user_ids) - expected_admissions
    )
    assert all(response.status_code in (401, 429) for response in responses)

    for uid in set(user_ids):
        pair_key = login_mod._failure_key(ip, uid)  # pylint: disable=protected-access
        pair_recorded = len(login_mod._LOGIN_FAILURES.get(pair_key, []))  # pylint: disable=protected-access
        assert pair_recorded <= login_mod._LOGIN_MAX_FAILURES  # pylint: disable=protected-access
        prior = seed_pair if uid == user_ids[0] else 0
        pair_peak = admissions["by_user"].get(uid, 0)
        assert prior + pair_peak <= login_mod._LOGIN_MAX_FAILURES  # pylint: disable=protected-access
    ip_recorded = len(login_mod._LOGIN_IP_FAILURES.get(ip, []))  # pylint: disable=protected-access
    assert ip_recorded <= login_mod._LOGIN_MAX_IP_FAILURES  # pylint: disable=protected-access
    assert seed_ip + admissions["total"] <= login_mod._LOGIN_MAX_IP_FAILURES  # pylint: disable=protected-access


def test_pair_failure_dict_evicts_oldest_active_keys_at_cap():
    failures = login_mod._LOGIN_FAILURES  # pylint: disable=protected-access
    keys = []
    for i in range(login_mod._LOGIN_MAX_KEYS + 2):  # pylint: disable=protected-access
        ip = f"192.0.2.{i}"
        uid = i + 1
        keys.append(login_mod._failure_key(ip, uid))  # pylint: disable=protected-access
        login_mod._record_login_failure(ip, uid)  # pylint: disable=protected-access

    assert len(failures) <= login_mod._LOGIN_MAX_KEYS  # pylint: disable=protected-access
    assert keys[-1] in failures
    assert keys[0] not in failures


def test_ip_failure_dict_evicts_oldest_active_keys_at_cap():
    failures = login_mod._LOGIN_IP_FAILURES  # pylint: disable=protected-access
    ips = []
    for i in range(login_mod._LOGIN_MAX_IP_KEYS + 2):  # pylint: disable=protected-access
        ip = f"198.51.100.{i}"
        ips.append(ip)
        login_mod._record_login_ip_failure(ip)  # pylint: disable=protected-access

    assert len(failures) <= login_mod._LOGIN_MAX_IP_KEYS  # pylint: disable=protected-access
    assert ips[-1] in failures
    assert ips[0] not in failures
