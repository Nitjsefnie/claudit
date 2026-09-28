"""Failure recovery tests for derived-state finalization."""
from __future__ import annotations

import pytest

from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture as ingest_fresh_db_fixture,
    _mini_r2_env_fixture as ingest_mini_r2_env,
)
from backend import db, ingest, ingest_scope, timing


@pytest.mark.parametrize("phase", ("notify", "finish"))
@pytest.mark.parametrize("timed", (False, True))
def test_finalization_error_leaves_incomplete_and_next_run_full(
        fresh_db, mini_r2_env, monkeypatch, phase, timed):
    """A failed finalization leaves a marker that forces full recovery."""
    monkeypatch.setattr(timing, "TIMING_ON", timed)
    assert ingest.run_ingest(trigger="manual")["error"] is None
    path = mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")

    def crash(*args, **kwargs):
        raise RuntimeError("review finalization failure")

    with monkeypatch.context() as patch:
        if phase == "notify":
            patch.setattr(ingest.events, "broadcast_threadsafe", crash)
        else:
            patch.setattr(ingest, "failed_public_keys", crash)
        with pytest.raises(RuntimeError, match="review finalization"):
            ingest.run_ingest(trigger="manual")
    with db.viz_conn() as conn:
        assert conn.execute(
            "SELECT complete FROM ingest_derived_state WHERE singleton"
        ).fetchone() == (False,)
    scope = ingest_scope.begin_scope()
    try:
        assert scope.full
    finally:
        ingest_scope.finish_scope()
