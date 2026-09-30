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
    old_config = fake_user[12345]
    new_config: dict = {}
    verified_configs: dict[str, dict] = {}
    auth.set_web_password(new_config, "new password")
    old_started = threading.Event()
    release_old = threading.Event()
    normalization_spent: list[int] = []

    def controlled_verify(config, password):
        if password == "old password":
            verified_configs["old"] = config
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
    # The generic failure page (#395): the message announced in its
    # role=alert element, nothing else about the answer changed.
    assert old_response.headers["content-type"].startswith("text/html")
    assert b'<div class="err" role="alert">Invalid credentials.</div>' \
        in old_response.body
    assert "set-cookie" not in old_response.headers
    assert verified_configs["old"] is old_config
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
    assert response.headers["content-type"].startswith("text/html")
    assert ('<div class="err" role="alert">Invalid credentials.</div>'
            in response.text)
    assert calls == [0]


def test_malformed_user_id_renders_full_signin_page(app, monkeypatch):
    """The malformed-id 400 is the FULL sign-in page (#427): the fixed
    text announced in its role=alert, the form back to retry from, and
    the body byte-identical across every malformed shape. It costs no
    query, no PBKDF2 run and no limiter accounting, answering before
    all three.
    """
    loads: list[int] = []
    verified: list[str] = []
    normalized: list[str] = []
    monkeypatch.setattr(session_mod, "load_user_config", loads.append)
    monkeypatch.setattr(
        auth,
        "verify_web_password",
        lambda config, password: verified.append(password),
    )
    monkeypatch.setattr(
        auth,
        "normalize_verification_timing",
        lambda password, spent: normalized.append(password),
    )
    client = TestClient(app, raise_server_exceptions=False)

    bodies = []
    for bad in ("abc", "", "0", "-5"):
        response = _post_login(client, bad, "pw")
        assert response.status_code == 400
        assert response.headers["content-type"].startswith("text/html")
        bodies.append(response.text)
    assert len(set(bodies)) == 1
    expected = client.get("/login").text.replace(
        '<div class="err" role="alert"></div>',
        '<div class="err" role="alert">Invalid user ID</div>',
    )
    assert bodies[0] == expected
    assert not loads
    assert not verified
    assert not normalized
    assert not login_mod._LOGIN_FAILURES  # pylint: disable=protected-access
    assert not login_mod._LOGIN_IP_FAILURES  # pylint: disable=protected-access
    assert not login_mod._LOGIN_INFLIGHT  # pylint: disable=protected-access
    assert not login_mod._LOGIN_IP_INFLIGHT  # pylint: disable=protected-access


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


def test_full_failure_dicts_keep_seen_history_and_reject_unseen_client(
    monkeypatch,
):  # pylint: disable=too-many-locals
    pair_history = login_mod._LOGIN_FAILURES  # pylint: disable=protected-access
    ip_history = login_mod._LOGIN_IP_FAILURES  # pylint: disable=protected-access
    ip = "198.51.100.92"
    uid = 71
    pair_key = login_mod._failure_key(ip, uid)  # pylint: disable=protected-access
    now = time.time()
    pair_history[pair_key] = [now] * 4
    for index in range(login_mod._LOGIN_MAX_KEYS - 1):  # pylint: disable=protected-access
        other_ip = f"192.0.{index // 256}.{index % 256}"
        other_uid = index + 1000
        key = login_mod._failure_key(  # pylint: disable=protected-access
            other_ip, other_uid
        )
        pair_history[key] = [now]
    ip_history[ip] = [now] * 19
    for index in range(login_mod._LOGIN_MAX_IP_KEYS - 1):  # pylint: disable=protected-access
        ip_history[f"198.18.{index // 256}.{index % 256}"] = [now]

    loads: list[int] = []
    config = {
        auth.WEB_PASSWORD_HASH_KEY: "stored-hash",
        auth.WEB_PASSWORD_SALT_KEY: "stored-salt",
    }
    monkeypatch.setattr(
        session_mod,
        "load_user_config",
        lambda user_id: loads.append(user_id) or config,
    )
    monkeypatch.setattr(auth, "verify_web_password", lambda *_args: False)
    monkeypatch.setattr(
        auth, "normalize_verification_timing", lambda *_args: None
    )

    first = asyncio.run(login_mod.login_post(
        _raw_login_request(ip), user_id=str(uid), password="wrong"
    ))
    second = asyncio.run(login_mod.login_post(
        _raw_login_request(ip), user_id=str(uid), password="wrong"
    ))
    unseen = asyncio.run(login_mod.login_post(
        _raw_login_request("203.0.113.250"),
        user_id="72",
        password="wrong",
    ))

    assert first.status_code == 401
    assert len(pair_history[pair_key]) == 5
    assert len(ip_history[ip]) == 20
    assert second.status_code == 429
    assert unseen.status_code == 429
    assert loads == [uid]
    assert len(pair_history) == login_mod._LOGIN_MAX_KEYS  # pylint: disable=protected-access
    assert len(ip_history) == login_mod._LOGIN_MAX_IP_KEYS  # pylint: disable=protected-access
    assert pair_key in pair_history
    assert ip in ip_history
    assert not login_mod._LOGIN_INFLIGHT  # pylint: disable=protected-access
    assert not login_mod._LOGIN_IP_INFLIGHT  # pylint: disable=protected-access


def test_unseen_admissions_fail_closed_when_reservation_maps_are_full():
    admitted: list[tuple[str, int]] = []
    last_limited = False
    try:
        for index in range(login_mod._LOGIN_MAX_KEYS + 1):  # pylint: disable=protected-access
            ip = f"198.18.{index // 256}.{index % 256}"
            uid = index + 1
            pair_limited = login_mod._check_login_rate_limit(  # pylint: disable=protected-access
                ip, uid
            )
            ip_limited = login_mod._check_login_ip_rate_limit(  # pylint: disable=protected-access
                ip
            )
            last_limited = pair_limited or ip_limited
            if not last_limited:
                login_mod._reserve_login_attempt(  # pylint: disable=protected-access
                    ip, uid
                )
                admitted.append((ip, uid))

        assert len(admitted) == 4096
        assert last_limited
        assert len(login_mod._LOGIN_INFLIGHT) == 4096  # pylint: disable=protected-access
        assert len(login_mod._LOGIN_IP_INFLIGHT) == 4096  # pylint: disable=protected-access
    finally:
        for ip, uid in admitted:
            login_mod._release_login_attempt(  # pylint: disable=protected-access
                ip, uid
            )

    assert not login_mod._LOGIN_INFLIGHT  # pylint: disable=protected-access
    assert not login_mod._LOGIN_IP_INFLIGHT  # pylint: disable=protected-access


def test_inflight_keys_reserve_failure_history_capacity():
    now = time.time()
    pair_history = login_mod._LOGIN_FAILURES  # pylint: disable=protected-access
    ip_history = login_mod._LOGIN_IP_FAILURES  # pylint: disable=protected-access
    for index in range(login_mod._LOGIN_MAX_KEYS - 1):  # pylint: disable=protected-access
        ip = f"192.0.{index // 256}.{index % 256}"
        pair_history[login_mod._failure_key(ip, index + 1)] = [now]  # pylint: disable=protected-access
    for index in range(login_mod._LOGIN_MAX_IP_KEYS - 1):  # pylint: disable=protected-access
        ip_history[f"198.18.{index // 256}.{index % 256}"] = [now]

    first_ip, first_uid = "203.0.113.1", 8001
    second_ip, second_uid = "203.0.113.2", 8002
    admitted = False
    try:
        assert not login_mod._check_login_rate_limit(  # pylint: disable=protected-access
            first_ip, first_uid
        )
        assert not login_mod._check_login_ip_rate_limit(first_ip)  # pylint: disable=protected-access
        login_mod._reserve_login_attempt(first_ip, first_uid)  # pylint: disable=protected-access
        admitted = True
        second_pair_limited = login_mod._check_login_rate_limit(  # pylint: disable=protected-access
            second_ip, second_uid
        )
        second_ip_limited = login_mod._check_login_ip_rate_limit(second_ip)  # pylint: disable=protected-access
        login_mod._record_login_failure(first_ip, first_uid)  # pylint: disable=protected-access
        login_mod._record_login_ip_failure(first_ip)  # pylint: disable=protected-access
    finally:
        if admitted:
            login_mod._release_login_attempt(first_ip, first_uid)  # pylint: disable=protected-access

    assert second_pair_limited
    assert second_ip_limited
    assert len(pair_history) == login_mod._LOGIN_MAX_KEYS  # pylint: disable=protected-access
    assert len(ip_history) == login_mod._LOGIN_MAX_IP_KEYS  # pylint: disable=protected-access


def test_cancelled_login_holds_reservations_until_workers_finish(
    fake_user, fake_session_store, monkeypatch
):  # pylint: disable=too-many-locals,too-many-statements
    ip = "198.51.100.93"
    uid = 12345
    workers_started = threading.Event()
    release_workers = threading.Event()
    workers_idle = threading.Event()
    workers_idle.set()
    worker_lock = threading.Lock()
    worker_state = {"started": 0, "active": 0}
    pair_checks = 0
    extra_checks_done = asyncio.Event()
    original_check = login_mod._check_login_rate_limit  # pylint: disable=protected-access

    def blocking_verification(_config, _password):
        with worker_lock:
            worker_state["started"] += 1
            worker_state["active"] += 1
            workers_idle.clear()
            if worker_state["started"] == 5:
                workers_started.set()
        try:
            if not release_workers.wait(timeout=10):
                raise TimeoutError("verification barrier was not released")
            return True
        finally:
            with worker_lock:
                worker_state["active"] -= 1
                if worker_state["active"] == 0:
                    workers_idle.set()

    def count_pair_checks(check_ip, check_uid):
        nonlocal pair_checks
        limited = original_check(check_ip, check_uid)
        pair_checks += 1
        if pair_checks == 10:
            extra_checks_done.set()
        return limited

    monkeypatch.setattr(auth, "verify_web_password", blocking_verification)
    monkeypatch.setattr(
        login_mod, "_check_login_rate_limit", count_pair_checks
    )

    async def _wait_maps_drained(
        timeout: float = 5.0, strict: bool = True
    ) -> None:
        """Slots release via task done-callbacks; wait for the drain.

        workers_idle only proves the thread functions returned — the
        done-callbacks still have to run on the loop before the maps
        empty, so snapshotting after a single sleep(0) races them.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (
            login_mod._LOGIN_INFLIGHT  # pylint: disable=protected-access
            or login_mod._LOGIN_IP_INFLIGHT  # pylint: disable=protected-access
        ):
            if loop.time() >= deadline:
                assert not strict, "login reservations outlived their workers"
                return
            await asyncio.sleep(0.01)

    async def run_cancellation_race():
        first_requests = [
            asyncio.create_task(login_mod.login_post(
                _raw_login_request(ip), user_id=str(uid), password="bad"
            ))
            for _ in range(5)
        ]
        extra_requests = []
        all_requests = list(first_requests)
        try:
            assert await asyncio.to_thread(workers_started.wait, 5)
            for task in first_requests:
                task.cancel()
            await asyncio.gather(*first_requests, return_exceptions=True)
            pair_key = login_mod._failure_key(ip, uid)  # pylint: disable=protected-access
            held_after_cancel = (
                login_mod._LOGIN_INFLIGHT.get(pair_key, 0),  # pylint: disable=protected-access
                login_mod._LOGIN_IP_INFLIGHT.get(ip, 0),  # pylint: disable=protected-access
            )
            extra_requests = [
                asyncio.create_task(login_mod.login_post(
                    _raw_login_request(ip),
                    user_id=str(uid),
                    password="bad",
                ))
                for _ in range(5)
            ]
            all_requests.extend(extra_requests)
            await asyncio.wait_for(extra_checks_done.wait(), timeout=5)
            with worker_lock:
                started_before_release = worker_state["started"]
            release_workers.set()
            extra_responses = await asyncio.gather(*extra_requests)
            assert await asyncio.to_thread(workers_idle.wait, 5)
            await _wait_maps_drained()
            maps_after_workers = (
                dict(login_mod._LOGIN_INFLIGHT),  # pylint: disable=protected-access
                dict(login_mod._LOGIN_IP_INFLIGHT),  # pylint: disable=protected-access
            )
            later_response = await login_mod.login_post(
                _raw_login_request(ip),
                user_id=str(uid),
                password="valid",
            )
            return (
                held_after_cancel,
                started_before_release,
                extra_responses,
                maps_after_workers,
                later_response,
            )
        finally:
            release_workers.set()
            for task in all_requests:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*all_requests, return_exceptions=True)
            await asyncio.to_thread(workers_idle.wait, 5)
            await _wait_maps_drained(strict=False)

    (
        held_after_cancel,
        started_before_release,
        extra_responses,
        maps_after_workers,
        later_response,
    ) = asyncio.run(run_cancellation_race())

    assert held_after_cancel == (5, 5)
    assert started_before_release == 5
    assert [response.status_code for response in extra_responses] == [
        429
    ] * 5
    assert maps_after_workers == ({}, {})
    assert later_response.status_code == 303
    assert not login_mod._LOGIN_INFLIGHT  # pylint: disable=protected-access
    assert not login_mod._LOGIN_IP_INFLIGHT  # pylint: disable=protected-access


def test_verification_worker_exception_returns_generic_failure_and_releases(
    fake_user, monkeypatch
):
    def fail_verification(_config, _password):
        raise RuntimeError("worker failed")

    normalized: list[int] = []
    monkeypatch.setattr(auth, "verify_web_password", fail_verification)
    monkeypatch.setattr(
        auth,
        "normalize_verification_timing",
        lambda _password, spent: normalized.append(spent),
    )

    response = asyncio.run(login_mod.login_post(
        _raw_login_request(), user_id="12345", password="bad"
    ))

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("text/html")
    assert b'<div class="err" role="alert">Invalid credentials.</div>' \
        in response.body
    assert normalized == [0]
    assert not login_mod._LOGIN_INFLIGHT  # pylint: disable=protected-access
    assert not login_mod._LOGIN_IP_INFLIGHT  # pylint: disable=protected-access
