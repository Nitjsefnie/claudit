"""CLAUDIT_DEBUG_POOL_RESET: who left a viz-pool connection non-idle."""
from __future__ import annotations

import logging
import threading
from contextlib import ExitStack

import pytest
from psycopg_pool import ConnectionPool

from backend import db, pool_reset_debug
from tests import scratch_db

_STRAY = "SELECT 'stray statement'"


@pytest.fixture(name="viz")
def _viz_fixture(monkeypatch):
    yield from scratch_db.scratch_viz_database(monkeypatch, "poolreset")


def test_a_statement_after_return_is_named_where_the_pool_warns(monkeypatch, caplog, viz) -> None:
    check, pool_log = ConnectionPool.check_connection, logging.getLogger("psycopg.pool")
    assert pool_reset_debug.pool_options(check) == {"check": check} and pool_reset_debug.FILTER not in pool_log.filters
    monkeypatch.setenv("CLAUDIT_DEBUG_POOL_RESET", "1")
    monkeypatch.setattr(pool_log, "filters", list(pool_log.filters))
    caplog.set_level(logging.WARNING)
    with db.viz_conn() as conn:
        pass
    stray = threading.Thread(target=conn.execute, args=(_STRAY,), name="pool-stray")
    stray.start()
    stray.join(30)
    with ExitStack() as stack:  # the pool hands out FIFO: holding all of them reaches the stray one
        for _ in range(db.viz_pool().get_stats()["pool_size"]):
            stack.enter_context(db.viz_conn())
    assert [r for r in caplog.records if r.name == "psycopg.pool" and "rolling back" in r.getMessage()]
    [line] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("POOL_RESET ")]
    now, moved = line.split("moved out of IDLE by")
    assert "test_pool_reset_debug.py" in now and moved.startswith(f" thread=pool-stray statement={_STRAY!r}\n") and ", in execute\n" in moved
