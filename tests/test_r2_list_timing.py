"""CLAUDIT_DEBUG_LIST_TIMING: the S3 listing's per-range and per-pass lines."""
from __future__ import annotations

import importlib
import logging
import re
import threading

import pytest

from backend import r2, r2_list, r2_list_timing
from tests.test_r2 import _BUCKET_KEYS, _SPLITS, _ListingS3, _ranges


class _Clock:
    """perf_counter that moves only when the listing double answers."""

    def __init__(self) -> None:
        self.now = 100.0
        self.lock = threading.Lock()

    def read(self) -> float:
        with self.lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.now += seconds


class _TimedListing(_ListingS3):
    """Records the keys each page returned, per range origin; each request costs one second
    (a client, a quarter second)."""

    def __init__(self, clock: _Clock) -> None:
        super().__init__(_BUCKET_KEYS, page=3)
        self.clock = clock
        self.returned: dict[tuple[str, str | None], list[int]] = {}

    def list_objects_v2(self, **kwargs) -> dict:
        page = super().list_objects_v2(**kwargs)
        token = kwargs.get("ContinuationToken")
        origin = self.tokens[token][1] if token is not None else kwargs.get("StartAfter")
        with self.lock:
            self.returned.setdefault((kwargs["Bucket"], origin), []).append(
                len(page.get("Contents", [])))
        self.clock.advance(1.0)
        return page


def _lines(caplog, kind: str) -> list[dict[str, str]]:
    return [dict(re.findall(r"(\w+)=(\S+)", r.getMessage())) for r in caplog.records
            if r.name == "claudit.r2" and r.getMessage().startswith(f"TIMING {kind} ")]


def _kept(bucket: str, lower: str | None, upper: str | None) -> int:
    return sum((lower is None or k > lower) and (upper is None or k <= upper)
               for k in _BUCKET_KEYS[bucket])


def _patch_on(monkeypatch, client: _ListingS3) -> None:
    monkeypatch.setenv("CLAUDIT_R2_ENDPOINT", "https://r2.invalid/s3")
    monkeypatch.setenv("CLAUDIT_R2_BUCKET", "+".join(client.names))
    monkeypatch.setattr(r2, "_boto_client", lambda: client)
    monkeypatch.setattr(r2_list_timing, "LIST_TIMING_ON", True)


def _fake_clocks(monkeypatch, clock: _Clock) -> None:
    """Wall from `clock`; process CPU 10.0 then 12.5; one thread CPU second per range."""
    monkeypatch.setattr(r2_list_timing, "perf_counter", clock.read)
    monkeypatch.setattr(r2_list_timing, "process_time", iter([10.0, 12.5]).__next__)
    per_thread = threading.local()

    def thread_time() -> float:
        per_thread.n = getattr(per_thread, "n", 0.0) + 1.0
        return per_thread.n
    monkeypatch.setattr(r2_list_timing, "thread_time", thread_time)


def _assert_bucket(caplog, client: _TimedListing, bucket: str) -> None:
    lowers = sorted(o for b, o in client.returned if b == bucket and o is not None)
    bounds = list(zip([None, *lowers], [*lowers, None]))
    mine = [line for line in _lines(caplog, "list_range") if line["bucket"] == bucket]
    assert sorted((line["lower"], line["upper"]) for line in mine) == sorted(
        (repr(lo), repr(hi)) for lo, hi in bounds)
    for lower, upper in bounds:
        line = next(m for m in mine if m["lower"] == repr(lower))
        pages = client.returned[(bucket, lower)]
        assert line["pages"] == str(len(pages))
        assert line["page_keys"] == ",".join(map(str, pages))
        assert line["request_ms"] == ",".join(["1000"] * len(pages))
        assert line["kept"] == str(_kept(bucket, lower, upper))
        assert (line["setup_ms"], line["wall_ms"], line["cpu_s"]) == (
            "250", str(250 + 1000 * len(pages)), "1.000")
    assert _lines(caplog, "list_bucket")[client.names.index(bucket)] == {
        "bucket": bucket, "ranges": str(len(bounds)),
        "requests": str(sum(len(client.returned[(bucket, lo)]) for lo, _ in bounds)),
        "longest_chain": str(max(len(client.returned[(bucket, lo)]) for lo, _ in bounds)),
        "max_in_flight": "1"}


def test_flag_on_logs_every_range_bucket_and_pass(monkeypatch, caplog) -> None:
    clock = _Clock()
    client = _TimedListing(clock)
    expected, _ = client.plain_walk()
    client.returned.clear()
    _patch_on(monkeypatch, client)
    monkeypatch.setattr(r2_list, "LIST_WORKERS", 1)
    monkeypatch.setattr(r2, "_boto_client", lambda: (clock.advance(0.25), client)[1])
    _fake_clocks(monkeypatch, clock)
    splits = _SPLITS["existing"]

    def split_points() -> list[str]:
        clock.advance(5.0)
        return splits
    caplog.set_level(logging.INFO, logger="claudit.r2")
    assert list(r2.list_keys(split_points=split_points)) == expected

    ranges = _lines(caplog, "list_range")
    assert len(ranges) == _ranges(splits) == len(client.returned)
    for bucket in client.names:
        _assert_bucket(caplog, client, bucket)
    assert _lines(caplog, "list_pass") == [{
        "buckets": "3", "ranges": str(len(ranges)), "requests": str(len(client.calls)),
        "longest_chain": str(max(map(len, client.returned.values()))), "max_in_flight": "1",
        "split_ms": "5000", "wall_ms": str(5000 + 250 * len(ranges) + 1000 * len(client.calls)),
        "cpu_s": "2.500"}]


def test_flag_on_counts_the_requests_in_flight_together(monkeypatch, caplog) -> None:
    client = _ListingS3(_BUCKET_KEYS, page=3)
    expected, _ = client.plain_walk()
    _patch_on(monkeypatch, client)
    client.barrier = threading.Barrier(3, timeout=10)
    caplog.set_level(logging.INFO, logger="claudit.r2")
    assert list(r2.list_keys(split_points=lambda: _SPLITS["existing"])) == expected
    (line,) = _lines(caplog, "list_pass")
    assert 3 <= int(line["max_in_flight"]) <= r2_list.LIST_WORKERS


def _refuse(*_args, **_kwargs):
    raise AssertionError("list timing ran with the flag off")


def test_flag_off_logs_nothing_and_times_nothing(monkeypatch, caplog) -> None:
    client = _ListingS3(_BUCKET_KEYS, page=3)
    expected, _ = client.plain_walk()
    _patch_on(monkeypatch, client)
    monkeypatch.setattr(r2_list_timing, "LIST_TIMING_ON", False)
    for name in ("ListTiming", "perf_counter", "process_time", "thread_time"):
        monkeypatch.setattr(r2_list_timing, name, _refuse)
    caplog.set_level(logging.INFO, logger="claudit.r2")
    assert list(r2.list_keys(split_points=lambda: _SPLITS["existing"])) == expected
    assert not [r for r in caplog.records if r.name == "claudit.r2"]
    monkeypatch.undo()
    _patch_on(monkeypatch, client)
    assert list(r2.list_keys(split_points=lambda: _SPLITS["existing"])) == expected
    assert len(_lines(caplog, "list_pass")) == 1


@pytest.mark.parametrize("value, on", [
    (None, False), ("", False), ("0", False), ("false", False), ("NO", False),
    ("1", True), ("off", True), (" 0", True),
])
def test_flag_grammar_matches_claudit_timing(monkeypatch, value, on) -> None:
    if value is None:
        monkeypatch.delenv("CLAUDIT_DEBUG_LIST_TIMING", raising=False)
    else:
        monkeypatch.setenv("CLAUDIT_DEBUG_LIST_TIMING", value)
    try:
        assert importlib.reload(r2_list_timing).LIST_TIMING_ON is on
    finally:
        monkeypatch.undo()
        importlib.reload(r2_list_timing)
