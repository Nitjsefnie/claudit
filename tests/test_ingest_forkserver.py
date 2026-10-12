"""Forkserver parentage, environment refresh and service-death guarantees."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

import pytest

from backend import ingest_workers
from backend.ingest_workers import parse_pool
from tests.ingest_process_helpers import configuration


def test_reused_server_refreshes_configuration(monkeypatch, tmp_path):
    """A second pool sees added, changed and removed settings after preload."""
    monkeypatch.setenv("R2_ENDPOINT", "file:///first/")
    monkeypatch.setenv("R2_BUCKET", "claude")
    monkeypatch.setenv("CLAUDIT_TIMING", "1")
    monkeypatch.delenv("R2_BLOB_CACHE", raising=False)
    results = []

    def first_pool():
        with parse_pool(1, os.getpid()) as pool:
            results.append(pool.submit(configuration).result(timeout=60))

    thread = threading.Thread(target=first_pool)
    thread.start()
    thread.join(timeout=90)
    assert not thread.is_alive()
    first = results[0]
    assert first["timing"] is True
    assert first["env"]["R2_ENDPOINT"] == "file:///first/"
    assert first["env"]["R2_BLOB_CACHE"] is None

    monkeypatch.setenv("R2_ENDPOINT", "file:///second/")
    monkeypatch.setenv("R2_BLOB_CACHE", str(tmp_path))
    monkeypatch.delenv("R2_BUCKET")
    monkeypatch.delenv("CLAUDIT_TIMING")
    with parse_pool(1, os.getpid()) as pool:
        second = pool.submit(configuration).result(timeout=60)
    assert second["parent"] == first["parent"], "the server was not reused"
    assert second["env"] == {
        "R2_ENDPOINT": "file:///second/", "R2_BUCKET": None,
        "R2_BLOB_CACHE": str(tmp_path), "CLAUDIT_TIMING": None}
    assert second["timing"] is False


def test_worker_rejects_wrong_server_parent(monkeypatch):
    """A lost/replaced OS parent must exit even with a live service sentinel."""
    def wrong_parent(**kwargs):
        kwargs["initargs"] = (-1, *kwargs["initargs"][1:])
        return ProcessPoolExecutor(**kwargs)

    monkeypatch.setattr(ingest_workers, "ProcessPoolExecutor", wrong_parent)
    with parse_pool(1, os.getpid()) as pool:
        with pytest.raises(BrokenProcessPool):
            pool.submit(int).result(timeout=60)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux parent-death signals")
@pytest.mark.parametrize("mode", ["service-kill", "server-term"])
def test_service_server_worker_death_chain(mode):
    """Kill the service without cleanup, or SIGTERM an otherwise-ignored server."""
    proc = subprocess.run(
        [sys.executable, "-m", "tests.ingest_lifetime_probe", mode],
        capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout) == {
        "server_dead": True, "worker_dead": True, "mode": mode}
