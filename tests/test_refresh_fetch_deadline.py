from __future__ import annotations

import email.message
import http.client
import io
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest

from tests.test_refresh_pricelog import pricelog


def _limits(monkeypatch, *, deadline=0.12, attempt=0.3, notice=1.0):
    guard = getattr(pricelog, "refresh_fetch_deadline", None)
    if guard is not None:
        monkeypatch.setattr(guard, "FETCH_DEADLINE_S", deadline)
        monkeypatch.setattr(guard, "FETCH_ATTEMPT_S", attempt)
        monkeypatch.setattr(guard, "FETCH_NOTICE_S", notice)
        monkeypatch.setattr(guard, "_deadline_at", None)
        monkeypatch.setattr(guard, "_failed_url", None)
    return guard


def test_deadline_is_cleared_around_existing_retry_tests(tmp_path):
    probe = tmp_path / "deadline_isolation_probe.py"
    probe.write_text('''
import importlib
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path.cwd() / "scripts" / "ci"))
now = [100000.0]
count = 0
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    global count
    count += 1
    if count in (2, 4):
        guard = sys.modules["refresh_fetch_deadline"]
        assert (guard._deadline_at, guard._failed_url, guard._last_url) == (None, None, None)
    if count == 3:
        guard = sys.modules["refresh_fetch_deadline"]
        guard._now = lambda: now[0]
        guard._deadline_at = 99999.0
        guard._failed_url = "old failure"
        guard._last_url = "old request"
    if count == 4:
        now[0] += 481.0
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    if count == 3:
        sys.modules["refresh_fetch_deadline"]._failed_url = "failure from first test"
def test_no_import_then_late_import():
    assert "refresh_fetch_deadline" not in sys.modules
    guard = importlib.import_module("refresh_fetch_deadline")
    guard._deadline_at = 123.0
    guard._failed_url = "late failure"
    guard._last_url = "late request"
    from tests.test_cache import test_ttlcache_clear
    test_ttlcache_clear()
def test_late_import_was_reset():
    from tests.test_cache import test_cache_response_decorator_caches_and_bypasses
    test_cache_response_decorator_caches_and_bypasses()
@pytest.mark.parametrize(("retry", "expected_time"), [
    ("test_a_server_error_gets_its_retry", 100000.0),
    ("test_a_rate_limit_answer_gets_its_retry", 100481.0)])
def test_retry_after_reset(monkeypatch, retry, expected_time):
    assert sys.modules["refresh_fetch_deadline"]._now() == expected_time
    from tests import test_refresh_pricelog
    getattr(test_refresh_pricelog, retry)(monkeypatch)
''', encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-s", "--tb=short", "-p",
         "tests.conftest", "-p", "deadline_isolation_probe", str(probe)],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout


def test_deadline_stops_a_trickling_body_and_later_urls(monkeypatch):
    _limits(monkeypatch, attempt=5.5)
    release = threading.Event()
    opened = []

    class Trickle(io.BytesIO):
        def read(self, *args):
            for _ in range(10):
                release.wait(0.5)
            return super().read(*args)

    def open_response(url):
        opened.append(url)
        return Trickle(b'{}')

    monkeypatch.setattr(pricelog, "_open", open_response)
    monkeypatch.setattr(pricelog, "_sleep", lambda _seconds: None)
    url = "https://example.invalid/trickle"
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError) as error:
            pricelog._fetch_json(url)  # pylint: disable=protected-access
        assert url in str(error.value) and "expired" in str(error.value)
        assert time.monotonic() - started < 3.0
        later = "https://example.invalid/later"
        with pytest.raises(TimeoutError) as spent:
            pricelog._fetch_json(later)  # pylint: disable=protected-access
        assert later in str(spent.value) and "already" in str(spent.value)
        assert opened == [url]
        guard = getattr(pricelog, "refresh_fetch_deadline", None)
        assert guard is not None and url in str(guard.deadline_error())
    finally:
        release.set()


def test_deadline_stops_a_real_loopback_server_waiting_on_headers(monkeypatch):
    _limits(monkeypatch, attempt=5.5)
    monkeypatch.setattr(pricelog, "_sleep", lambda _seconds: None)
    release = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            release.wait(5.0)
            self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/stall"
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError) as error:
            pricelog._fetch_json(url)  # pylint: disable=protected-access
        assert url in str(error.value)
        assert time.monotonic() - started < 3.0
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
    assert not thread.is_alive()


def test_in_flight_notices_are_flushed_each_interval(monkeypatch):
    _limits(monkeypatch, deadline=15.0, attempt=10.0, notice=0.04)
    release = threading.Event()
    flushes = []

    class Notices(io.StringIO):
        def flush(self):
            super().flush()
            flushes.append(True)
            if len(flushes) >= 2:
                release.set()

    notices = Notices()
    monkeypatch.setattr("sys.stderr", notices)

    def delayed_open(_url):
        release.wait(5.0)
        return io.BytesIO(b'{}')

    monkeypatch.setattr(pricelog, "_open", delayed_open)
    url = "https://example.invalid/notices"
    try:
        assert pricelog._fetch_json(url) == {}  # pylint: disable=protected-access
        lines = notices.getvalue().splitlines()
        assert len(flushes) >= 2 and len(lines) >= 2
        assert all(url in line and "second" in line for line in lines)
        assert float(lines[1].rsplit(": ", 1)[1].split()[0]) > 0
    finally:
        release.set()


def test_default_notice_cadence_names_each_elapsed_interval(monkeypatch, capsys):
    guard = getattr(pricelog, "refresh_fetch_deadline", None)
    now = [100.0]
    if guard is not None:
        monkeypatch.setattr(guard, "_deadline_at", None)
        monkeypatch.setattr(guard, "_failed_url", None)
        monkeypatch.setattr(guard, "FETCH_ATTEMPT_S", 100.0)
        monkeypatch.setattr(guard, "_now", lambda: now[0])

        class Worker:
            def __init__(self, target, args, daemon):
                self.target, self.args = target, args
                self.alive = True
                self.calls = 0
                assert daemon

            def start(self):
                pass

            def join(self, timeout):
                self.calls += 1
                now[0] = (175.0 if self.calls > 5 else min(175.0, now[0] + timeout))
                if now[0] == 175.0:
                    self.target(*self.args)
                    self.alive = False

            def is_alive(self):
                return self.alive

        monkeypatch.setattr(guard.threading, "Thread", Worker)
    monkeypatch.setattr(pricelog, "_open", lambda _url: io.BytesIO(b'{}'))
    url = "https://example.invalid/cadence"
    assert pricelog._fetch_json(url) == {}  # pylint: disable=protected-access
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    assert url in lines[0] and "30.0 seconds" in lines[0]
    assert url in lines[1] and "60.0 seconds" in lines[1]


@pytest.mark.parametrize("error", [
    TimeoutError("silent"),
    urllib.error.URLError("resolution failed"),
    urllib.error.HTTPError("https://example.invalid/http", 404, "missing",
                           email.message.Message(), None),
    http.client.IncompleteRead(b"x", 8),
    json.JSONDecodeError("invalid", "x", 0),
    ValueError("bad input"),
])
def test_every_final_error_names_its_url(monkeypatch, error):
    _limits(monkeypatch, deadline=1.0)

    def fail(_url):
        raise error

    monkeypatch.setattr(pricelog, "_open", fail)
    monkeypatch.setattr(pricelog, "_sleep", lambda _seconds: None)
    url = "https://example.invalid/failure"
    with pytest.raises(type(error)) as caught:
        pricelog._fetch_json(url)  # pylint: disable=protected-access
    assert url in str(caught.value)


def test_backoff_is_clamped_to_the_remaining_run_deadline(monkeypatch):
    guard = _limits(monkeypatch, deadline=3.0)
    now = [100.0]
    sleeps = []
    opened = []
    if guard is not None:
        monkeypatch.setattr(guard, "_now", lambda: now[0])

    def fail(url):
        opened.append(url)
        raise TimeoutError("offline")

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(pricelog, "_open", fail)
    monkeypatch.setattr(pricelog, "_sleep", sleep)
    url = "https://example.invalid/backoff"
    with pytest.raises(TimeoutError) as caught:
        pricelog._fetch_json(url)  # pylint: disable=protected-access
    assert url in str(caught.value)
    assert sleeps == [2.0, 1.0]
    assert opened == [url, url]


def test_a_backoff_error_names_the_fetch_url(monkeypatch):
    _limits(monkeypatch, deadline=10.0)

    def fail_open(_url):
        raise TimeoutError("offline")

    def fail_sleep(_seconds):
        raise OSError("sleep failed")

    monkeypatch.setattr(pricelog, "_open", fail_open)
    monkeypatch.setattr(pricelog, "_sleep", fail_sleep)
    url = "https://example.invalid/backoff-error"
    with pytest.raises(OSError) as caught:
        pricelog._fetch_json(url)  # pylint: disable=protected-access
    assert url in str(caught.value)


def test_deadline_is_shared_across_fetches_and_armed_at_the_first(monkeypatch):
    guard = getattr(pricelog, "refresh_fetch_deadline", None)
    now = [10000.0]
    if guard is not None:
        monkeypatch.setattr(guard, "_deadline_at", None)
        monkeypatch.setattr(guard, "_failed_url", None)
        monkeypatch.setattr(guard, "_now", lambda: now[0])
    opened = []

    def response(url):
        opened.append(url)
        return io.BytesIO(b'{}')

    monkeypatch.setattr(pricelog, "_open", response)
    first = "https://example.invalid/first"
    second = "https://example.invalid/second"
    third = "https://example.invalid/third"
    assert pricelog._fetch_json(first) == {}  # pylint: disable=protected-access
    now[0] += 479.0
    assert pricelog._fetch_json(second) == {}  # pylint: disable=protected-access
    now[0] += 1.0
    with pytest.raises(TimeoutError) as caught:
        pricelog._fetch_json(third)  # pylint: disable=protected-access
    assert third in str(caught.value) and "already" in str(caught.value)
    assert opened == [first, second]


def test_deadline_can_expire_after_the_last_successful_fetch(monkeypatch):
    guard = _limits(monkeypatch, deadline=480.0)
    now = [10000.0]
    if guard is not None:
        monkeypatch.setattr(guard, "_now", lambda: now[0])
    monkeypatch.setattr(pricelog, "_open", lambda _url: io.BytesIO(b'{}'))
    url = "https://example.invalid/last-fetch"
    assert pricelog._fetch_json(url) == {}  # pylint: disable=protected-access
    now[0] += 480.0
    assert guard is not None
    assert str(guard.deadline_error()) == f"fetch deadline expired after {url}"


def test_a_worker_base_exception_reaches_its_caller(monkeypatch):
    _limits(monkeypatch, deadline=1.0)

    def stop(_url):
        raise SystemExit(17)

    monkeypatch.setattr(pricelog, "_open", stop)
    with pytest.raises(SystemExit) as caught:
        pricelog._fetch_json("https://example.invalid/interrupted")  # pylint: disable=protected-access
    assert caught.value.code == 17


def test_a_completion_after_its_attempt_limit_is_refused(monkeypatch):
    guard = _limits(monkeypatch, deadline=10.0, attempt=1.0)
    now = [100.0]
    if guard is not None:
        monkeypatch.setattr(guard, "_now", lambda: now[0])
    monkeypatch.setattr(pricelog, "FETCH_ATTEMPTS", 1)

    def too_late(_url):
        now[0] = 101.1
        return io.BytesIO(b'{}')

    monkeypatch.setattr(pricelog, "_open", too_late)
    url = "https://example.invalid/late"
    with pytest.raises(TimeoutError) as caught:
        pricelog._fetch_json(url)  # pylint: disable=protected-access
    assert url in str(caught.value)


def test_a_fetch_attempt_is_bounded_at_sixty_seconds(monkeypatch):
    guard = getattr(pricelog, "refresh_fetch_deadline", None)
    now = [100.0]
    if guard is not None:
        monkeypatch.setattr(guard, "_deadline_at", None)
        monkeypatch.setattr(guard, "_failed_url", None)
        monkeypatch.setattr(guard, "_now", lambda: now[0])
    monkeypatch.setattr(pricelog, "FETCH_ATTEMPTS", 1)

    def too_late(_url):
        now[0] += 61.0
        return io.BytesIO(b'{}')

    monkeypatch.setattr(pricelog, "_open", too_late)
    with pytest.raises(TimeoutError, match="timed out after 60 seconds"):
        pricelog._fetch_json("https://example.invalid/sixty")  # pylint: disable=protected-access


def test_an_attempt_timeout_can_retry_and_succeed(monkeypatch):
    guard = _limits(monkeypatch, deadline=10.0, attempt=0.05)
    release = threading.Event()
    opened = []
    sleeps = []

    def open_response(url):
        opened.append(url)
        if len(opened) == 1:
            release.wait(0.4)
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(pricelog, "_open", open_response)
    monkeypatch.setattr(pricelog, "_sleep", sleeps.append)
    url = "https://example.invalid/attempt-retry"
    try:
        assert pricelog._fetch_json(url) == {"ok": True}  # pylint: disable=protected-access
        assert opened == [url, url] and sleeps == [2.0]
        assert guard is not None and guard.deadline_error() is None
    finally:
        release.set()


def test_invalid_utf8_keeps_its_error_type_and_names_the_url(monkeypatch):
    _limits(monkeypatch, deadline=1.0)
    monkeypatch.setattr(pricelog, "_open", lambda _url: io.BytesIO(b'\xff'))
    url = "https://example.invalid/invalid-body"
    with pytest.raises(UnicodeDecodeError) as caught:
        pricelog._fetch_json(url)  # pylint: disable=protected-access
    assert url in str(caught.value)


def test_an_exception_with_its_own_string_still_names_the_url(monkeypatch):
    _limits(monkeypatch, deadline=1.0)

    class OpaqueError(Exception):
        def __str__(self):
            return "opaque failure"

    def fail(_url):
        raise OpaqueError()

    monkeypatch.setattr(pricelog, "_open", fail)
    url = "https://example.invalid/opaque"
    with pytest.raises(Exception) as caught:
        pricelog._fetch_json(url)  # pylint: disable=protected-access
    assert url in str(caught.value)


def test_a_custom_error_with_a_multi_argument_constructor_still_names_the_url(monkeypatch):
    _limits(monkeypatch, deadline=1.0)

    class OpaqueError(Exception):
        def __init__(self, first, second):
            super().__init__(first, second)

        def __str__(self):
            return "opaque failure"

    def fail(_url):
        raise OpaqueError(1, 2)

    monkeypatch.setattr(pricelog, "_open", fail)
    url = "https://example.invalid/custom"
    with pytest.raises(Exception) as caught:
        pricelog._fetch_json(url)  # pylint: disable=protected-access
    assert url in str(caught.value)


def test_a_custom_error_that_refuses_reconstruction_still_names_the_url(monkeypatch):
    _limits(monkeypatch, deadline=1.0)

    class RefusesCopy(Exception):
        def __init__(self, message):
            if message.startswith("https://"):
                raise RuntimeError("cannot reconstruct")
            super().__init__(message)

        def __str__(self):
            return "opaque failure"

    def fail(_url):
        raise RefusesCopy("offline")

    monkeypatch.setattr(pricelog, "_open", fail)
    url = "https://example.invalid/reconstruction"
    with pytest.raises(Exception) as caught:
        pricelog._fetch_json(url)  # pylint: disable=protected-access
    assert url in str(caught.value)


def test_a_custom_error_with_stored_message_keeps_its_type(monkeypatch):
    _limits(monkeypatch, deadline=1.0)

    class StoredMessage(Exception):
        def __init__(self, message):
            super().__init__(message)
            self.message = message

        def __str__(self):
            return self.message

    def fail(_url):
        raise StoredMessage("offline")

    monkeypatch.setattr(pricelog, "_open", fail)
    url = "https://example.invalid/stored-message"
    with pytest.raises(StoredMessage) as caught:
        pricelog._fetch_json(url)  # pylint: disable=protected-access
    assert url in str(caught.value)


def test_error_annotations_keep_errno_and_filename(monkeypatch):
    _limits(monkeypatch, deadline=1.0)

    def fail(_url):
        raise FileNotFoundError(2, "missing", "/missing")

    monkeypatch.setattr(pricelog, "_open", fail)
    url = "https://example.invalid/filesystem"
    with pytest.raises(FileNotFoundError) as caught:
        pricelog._fetch_json(url)  # pylint: disable=protected-access
    assert url in str(caught.value)
    assert caught.value.errno == 2 and caught.value.filename == "/missing"


def test_url_error_keeps_the_underlying_reason_readable(monkeypatch):
    _limits(monkeypatch, deadline=1.0)

    def fail(_url):
        raise urllib.error.URLError(OSError(5, "unavailable"))

    monkeypatch.setattr(pricelog, "_open", fail)
    monkeypatch.setattr(pricelog, "_sleep", lambda _seconds: None)
    url = "https://example.invalid/reason"
    with pytest.raises(urllib.error.URLError) as caught:
        pricelog._fetch_json(url)  # pylint: disable=protected-access
    assert caught.value.reason == f"{url}: [Errno 5] unavailable"


def test_a_url_already_in_the_error_is_not_duplicated(monkeypatch):
    _limits(monkeypatch, deadline=1.0)
    url = "https://example.invalid/named"

    def fail(_url):
        raise ValueError(f"{url}: broken")

    monkeypatch.setattr(pricelog, "_open", fail)
    with pytest.raises(ValueError) as caught:
        pricelog._fetch_json(url)  # pylint: disable=protected-access
    assert str(caught.value).count(url) == 1


def test_success_near_attempt_limit_is_not_rejected(monkeypatch, capsys):
    guard = _limits(monkeypatch, deadline=10.0, attempt=1.0, notice=2.0)
    now = [100.0]
    if guard is not None:
        monkeypatch.setattr(guard, "_now", lambda: now[0])
    monkeypatch.setattr(pricelog, "FETCH_ATTEMPTS", 1)

    def slow_success(_url):
        now[0] = 100.9
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(pricelog, "_open", slow_success)
    assert pricelog._fetch_json("https://example.invalid/success") == {"ok": True}  # pylint: disable=protected-access
    assert not capsys.readouterr().err


def test_abandoned_worker_does_not_delay_process_exit():
    script = """
import io, sys, threading
sys.path.insert(0, 'scripts/ci')
import refresh_pricelog as p
guard = getattr(p, 'refresh_fetch_deadline', None)
if guard is not None:
    guard.FETCH_DEADLINE_S = 0.05
    guard.FETCH_ATTEMPT_S = 0.05
def stalled(_url):
    threading.Event().wait(10)
    return io.BytesIO(b'{}')
p._open = stalled
try:
    p._fetch_json('https://example.invalid/process')
except TimeoutError as exc:
    print(exc)
    raise SystemExit(0)
raise SystemExit(1)
"""
    try:
        result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                text=True, timeout=3, check=False)
    except subprocess.TimeoutExpired:
        pytest.fail("process did not exit while its finite worker was stalled")
    assert result.returncode == 0, result.stderr
    assert "https://example.invalid/process" in result.stdout
