from __future__ import annotations

import http.client
import json
import queue
import sys
import threading
import time
import urllib.error
from typing import Callable

FETCH_DEADLINE_S = 480
FETCH_ATTEMPT_S = 60
FETCH_NOTICE_S = 30

_deadline_at: float | None = None
_failed_url: str | None = None
_last_url: str | None = None
_now = time.monotonic


class FetchDeadlineError(TimeoutError):
    pass


class _IncompleteReadWithURL(http.client.IncompleteRead):
    def __init__(self, error: http.client.IncompleteRead, url: str) -> None:
        super().__init__(error.partial, error.expected)
        self.url = url

    def __str__(self) -> str:
        return f"{self.url}: {super().__str__()}"


def _expired(url: str, *, already: bool = False) -> FetchDeadlineError:
    global _failed_url  # pylint: disable=global-statement
    if _failed_url is None:
        _failed_url = url
    state = "already spent" if already else "expired"
    return FetchDeadlineError(f"{url}: fetch deadline {state}")


def deadline_error() -> FetchDeadlineError | None:
    if _failed_url is not None:
        return FetchDeadlineError(f"{_failed_url}: fetch deadline expired")
    if _deadline_at is not None and _last_url is not None and _now() >= _deadline_at:
        return _expired(_last_url)
    return None


def _remaining(url: str) -> float:
    assert _deadline_at is not None
    remaining = _deadline_at - _now()
    if remaining <= 0:
        raise _expired(url, already=True)
    return remaining


def _load(url: str, open_url: Callable, result: queue.Queue) -> None:
    try:
        with open_url(url) as response:
            value = json.load(response)
    except BaseException as exc:  # the caller must receive even non-Exception failures
        result.put((_now(), None, exc))
    else:
        result.put((_now(), value, None))


def _attempt(url: str, open_url: Callable) -> object:
    start = _now()
    end = start + min(FETCH_ATTEMPT_S, _remaining(url))
    notice = start + FETCH_NOTICE_S
    result: queue.Queue = queue.Queue(maxsize=1)
    worker = threading.Thread(target=_load, args=(url, open_url, result), daemon=True)
    worker.start()
    while True:
        worker.join(max(0.0, min(end, notice) - _now()))
        if not worker.is_alive():
            completed, value, error = result.get_nowait()
            if completed < end:
                if error is not None:
                    raise error
                return value
        now = _now()
        if now >= end:
            if _deadline_at is not None and now >= _deadline_at:
                raise _expired(url)
            raise TimeoutError(f"{url}: fetch attempt timed out after {FETCH_ATTEMPT_S:g} seconds")
        if now >= notice:
            print(f"fetching {url}: {now - start:.1f} seconds elapsed", file=sys.stderr, flush=True)
            notice += FETCH_NOTICE_S


def _with_url(url: str, exc: Exception) -> Exception:
    if url in str(exc):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        exc.msg = f"{url}: {exc.msg}"
    elif isinstance(exc, urllib.error.URLError):
        exc.reason = f"{url}: {exc.reason}"
    elif isinstance(exc, http.client.IncompleteRead):
        return _IncompleteReadWithURL(exc, url)
    elif isinstance(exc, (UnicodeDecodeError, UnicodeEncodeError, UnicodeTranslateError)):
        exc.reason = f"{url}: {exc.reason}"
    elif isinstance(exc, OSError) and exc.strerror is not None:
        exc.strerror = f"{url}: {exc.strerror}"
    else:
        message = f"{url}: {exc}"
        exc.args = (message,)
        if url not in str(exc):
            try:
                wrapped = type(exc)(message)
            except Exception:  # pylint: disable=broad-exception-caught
                return RuntimeError(message)
            if url not in str(wrapped):
                return RuntimeError(message)
            return wrapped
    return exc


def fetch_json(url: str, open_url: Callable, sleep: Callable,
               transient: Callable, attempts: int, backoff: float) -> object:
    global _deadline_at, _last_url  # pylint: disable=global-statement
    if _deadline_at is None:
        _deadline_at = _now() + FETCH_DEADLINE_S
    try:
        _remaining(url)
        _last_url = url
        for attempt in range(attempts):
            try:
                return _attempt(url, open_url)
            except FetchDeadlineError:
                raise
            except Exception as exc:
                if not transient(exc) or attempt + 1 == attempts:
                    raise
                sleep(min(backoff * 2 ** attempt, _remaining(url)))
        raise AssertionError("unreachable: every attempt returns or raises")
    except Exception as exc:
        wrapped = _with_url(url, exc)
        if wrapped is exc:
            raise
        raise wrapped from exc
