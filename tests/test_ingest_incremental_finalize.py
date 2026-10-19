from __future__ import annotations

import pytest

from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture as ingest_fresh_db_fixture,
    _mini_r2_env_fixture as ingest_mini_r2_env,
)
from test_ingest_incremental import _timing_lines
from backend import db, ingest, ingest_scope, timing


@pytest.mark.parametrize("phase,timed", [
    ("_close_run", True),
    ("warm_common", True),
    ("notify", False),
    ("notify", True),
    ("finish", False),
    ("finish", True),
], ids=["close-run", "warm-cache", "notify-untimed", "notify-timed",
        "finish-untimed", "finish-timed"])
def test_finalization_error_leaves_incomplete_and_next_run_full(
        fresh_db, mini_r2_env, monkeypatch, caplog, phase, timed):
    """A failed finalization leaves a marker that forces full recovery."""
    monkeypatch.setattr(timing, "TIMING_ON", timed)
    assert ingest.run_ingest(trigger="manual")["error"] is None
    if phase in ("notify", "finish"):
        path = mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl"
        path.write_bytes(path.read_bytes() + b"\n")

    def crash(*args, **kwargs):
        raise RuntimeError("review finalization failure")

    with monkeypatch.context() as patch:
        if phase == "notify":
            patch.setattr(ingest.events, "broadcast_threadsafe", crash)
        elif phase == "finish":
            patch.setattr(ingest, "failed_public_keys", crash)
        else:
            patch.setattr(ingest, phase, crash)
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

    caplog.clear()
    with caplog.at_level("INFO", logger="claudit.ingest"):
        recovered = ingest.run_ingest(trigger="manual")
    assert recovered["error"] is None
    if timed:
        assert "scope=full" in _timing_lines(caplog)[-1]
