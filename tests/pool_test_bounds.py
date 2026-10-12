"""Bound process-pool controls outside the process that owns the executor."""
from __future__ import annotations

import functools
import multiprocessing
import subprocess
import sys

import pytest

ISOLATED = False

requires_forkserver = pytest.mark.skipif(
    "forkserver" not in multiprocessing.get_all_start_methods(),
    reason="forkserver context unavailable")


def bounded_pool_test(function):
    """Keep fixture setup/assertions intact in a disposable pytest process."""
    @functools.wraps(function)
    def bounded(*args, **kwargs):
        if ISOLATED:
            return function(*args, **kwargs)
        node = function.__module__.removeprefix("tests.")
        target = f"tests/{node}.py::{function.__name__}"
        proc = subprocess.run(
            [sys.executable, "-m", "tests.ingest_lifetime_probe", "check", target],
            capture_output=True, text=True, timeout=120, check=False)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not proc.stderr, proc.stderr
        return None
    return bounded
