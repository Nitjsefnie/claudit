"""Importable parse units for tests that run in isolated worker processes."""
from __future__ import annotations

import lzma
import multiprocessing
import os
import signal
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from backend import ingest_fetch, pricing, r2, rate_fingerprint

_DECOMPRESS = lzma.decompress


def noop_parse(key: str, sidecar_key: str | None = None,
               etag: str | None = None, size: int | None = None) -> dict:
    """A parse unit with no side effects, for pipeline plumbing tests."""
    return {}


def worker_report() -> tuple:
    """Run inside the worker: its handlers and parent pid."""
    return (signal.getsignal(signal.SIGTERM),
            signal.getsignal(signal.SIGINT), os.getppid())


def server_report_parse(key: str, sidecar_key: str | None = None,
                        etag: str | None = None, size: int | None = None) -> dict:
    """Report the actual forking parent's threads, not the client's threads."""
    parent = os.getppid()
    return {"parent": parent, "method": multiprocessing.get_start_method(),
            "threads": len(list(Path(f"/proc/{parent}/task").iterdir()))}


def configuration() -> dict:
    """Configuration read at use time plus the preloaded timing flag."""
    from backend import timing  # pylint: disable=import-outside-toplevel
    keys = ("R2_ENDPOINT", "R2_BUCKET", "R2_BLOB_CACHE", "CLAUDIT_TIMING")
    return {"env": {key: os.environ.get(key) for key in keys},
            "timing": timing.TIMING_ON, "parent": os.getppid()}


def priced_parse(key: str, sidecar_key: str | None,
                 etag: str | None = None, size: int | None = None, *,
                 tables: dict) -> dict:
    """Give a fresh child the equality test's active pricing fixture.

    The suite bench rebinds tables after the server preload. Carry those
    bindings with the parse unit, like failure hooks.
    """
    with ExitStack() as stack:
        for name, value in tables.items():
            stack.enter_context(patch.object(pricing, name, value))
        rate_fingerprint.clear_fingerprint_cache()
        try:
            return ingest_fetch.fetch_and_parse(
                key, sidecar_key, ingest_fetch.fetch_with_retry,
                etag=etag, size=size)
        finally:
            rate_fingerprint.clear_fingerprint_cache()


def fetch_failure(key: str, sidecar_key: str | None,
                  etag: str | None = None, size: int | None = None, *,
                  failed_key: str, error: type[Exception]) -> dict:
    """Fail one object's fetch inside the child; its peers parse normally."""
    def fetch(file_key: str, *rest) -> bytes:
        if file_key.endswith(failed_key):
            raise error("connection dropped" if error is OSError else file_key)
        return ingest_fetch.fetch_with_retry(file_key, *rest)

    return ingest_fetch.fetch_and_parse(
        key, sidecar_key, fetch, etag=etag, size=size)


def _broken_get_object(_key: str) -> bytes:
    raise TypeError("no client at all")


def fatal_fetch(key: str, sidecar_key: str | None,
                etag: str | None = None, size: int | None = None) -> dict:
    """Exercise the real retry wrapper's fatal path inside the child."""
    with patch.object(r2, "get_object", _broken_get_object):
        return ingest_fetch.fetch_and_parse(
            key, sidecar_key, ingest_fetch.fetch_with_retry,
            etag=etag, size=size)


def _padded_decompress(data: bytes) -> bytes:
    out = _DECOMPRESS(data)
    time.sleep(0.05)
    return out


def padded_inflate(key: str, sidecar_key: str | None,
                   etag: str | None = None, size: int | None = None) -> dict:
    """Book a controlled inflate span in the child through the real GET."""
    with patch.object(lzma, "decompress", _padded_decompress):
        return ingest_fetch.fetch_and_parse(
            key, sidecar_key, ingest_fetch.fetch_with_retry,
            etag=etag, size=size)
