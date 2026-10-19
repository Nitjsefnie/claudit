from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture as ingest_fresh_db_fixture,
    _mini_r2_env_fixture as ingest_mini_r2_env,
)
from test_ingest_incremental import _derived_snapshot, _timing_lines
from backend import db, ingest, ingest_scope, timing
from backend.ingest_rollup_latency import _overlapping_bucket_starts
from tests import dims_seed


def _rebuild_rollups(scope: ingest_scope.Scope | None = None) -> None:
    for name in (
        "rebuild_rollup", "rebuild_tool_rollup", "rebuild_tool_error_rollup",
        "rebuild_dispatch_rollup", "rebuild_dispatch_brief_rollup",
        "rebuild_latency_rollup", "rebuild_ctx_cost_rollup",
        "rebuild_agent_rollup",
    ):
        getattr(ingest, name)(scope)


def _seed_rollups() -> None:
    """Seed two files whose canonical winner and tool use span projects."""
    with db.viz_conn() as conn:
        conn.execute(
            "INSERT INTO projects VALUES "
            "('p', 'p', now(), now()), ('q', 'q', now(), now())"
        )
        for file_key, project_id in (("a", "p"), ("z", "q")):
            conn.execute(
                "INSERT INTO files (file_key, project_id, session_id, is_main, "
                "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
                "parser_version) VALUES (%s, %s, %s, TRUE, 'e', 1, now(), "
                "now(), 'v')",
                (file_key, project_id, file_key),
            )
        model_id = dims_seed.dim_id(conn, "dim_model", "model")
        agent_tool = dims_seed.dim_id(conn, "dim_tool_name", "Agent")
        read_tool = dims_seed.dim_id(conn, "dim_tool_name", "Read")
        failed_kind = dims_seed.dim_id(conn, "dim_error_kind", "failed")
        worker_type = dims_seed.dim_id(conn, "dim_agent_type", "worker")
        for file_key, ts in (
            ("a", "2026-01-01T13:15:00Z"),
            ("z", "2026-01-02T14:45:00Z"),
        ):
            fid = dims_seed.file_id(conn, file_key)
            conn.execute(
                "INSERT INTO records (file_id, line_num, fallback, ts, model_id, "
                "reply_latency_s) VALUES "
                "(%s, 1, 'shared', %s, %s, 5), "
                "(%s, 2, NULL, %s, %s, NULL)",
                (fid, ts, model_id, fid, ts, model_id),
            )
            conn.execute(
                "INSERT INTO records_price (file_id, line_num) "
                "VALUES (%s, 1), (%s, 2)", (fid, fid))
            conn.execute(
                "INSERT INTO tool_uses (file_id, line_num, idx, "
                "tool_name_id, tool_use_id, ts, model_id, is_error, "
                "error_kind_id, agent_type_id, dispatch_brief_ref, "
                "dispatch_prompt_chars) VALUES "
                "(%s, 3, 0, %s, 'shared-call', %s, %s, TRUE, "
                "%s, %s, TRUE, 50), "
                "(%s, 4, 0, %s, NULL, NULL, %s, FALSE, NULL, NULL, "
                "NULL, NULL)",
                (fid, agent_tool, ts, model_id, failed_kind, worker_type,
                 fid, read_tool, model_id),
            )
        conn.commit()
    ingest.recompute_canonical()
    _rebuild_rollups()


def _apply_shape(scope: ingest_scope.Scope, shape: str) -> set[str]:
    dirty = {"0"} if shape == "new_winner" else {"a"}
    with db.viz_conn() as conn:
        ingest_scope.capture_and_add(scope, conn, dirty)
        if shape == "delete_winner":
            conn.execute("DELETE FROM files WHERE file_key = 'a'")
        elif shape == "new_winner":
            conn.execute(
                "INSERT INTO files (file_key, project_id, session_id, is_main, "
                "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
                "parser_version) VALUES ('0', 'q', '0', TRUE, 'e', 1, now(), "
                "now(), 'v')"
            )
            zero_fid = dims_seed.file_id(conn, "0")
            model_id = dims_seed.dim_id(conn, "dim_model", "model")
            read_tool = dims_seed.dim_id(conn, "dim_tool_name", "Read")
            conn.execute(
                "INSERT INTO records (file_id, line_num, fallback, ts, model_id, "
                "reply_latency_s) VALUES (%s, 1, 'shared', "
                "'2026-01-03T15:00Z', %s, 9)",
                (zero_fid, model_id),
            )
            conn.execute(
                "INSERT INTO records_price (file_id, line_num) "
                "VALUES (%s, 1)", (zero_fid,))
            conn.execute(
                "INSERT INTO tool_uses (file_id, line_num, idx, "
                "tool_name_id, tool_use_id, ts, model_id) VALUES "
                "(%s, 3, 0, %s, 'shared-call', '2026-01-03T15:00Z', %s)",
                (zero_fid, read_tool, model_id),
            )
        elif shape == "move_remove":
            conn.execute(
                "UPDATE files SET project_id = 'q', is_main = FALSE, "
                "agent_type = 'worker' WHERE file_key = 'a'"
            )
            conn.execute(
                "UPDATE records r SET ts = '2026-01-04T16:00Z' "
                "FROM files f WHERE r.file_id = f.file_id "
                "AND f.file_key = 'a'"
            )
            conn.execute(
                "DELETE FROM tool_uses t USING files f "
                "WHERE t.file_id = f.file_id AND f.file_key = 'a'")
        elif shape == "null_ts":
            conn.execute(
                "UPDATE records r SET ts = NULL, reply_latency_s = NULL "
                "FROM files f WHERE r.file_id = f.file_id "
                "AND f.file_key = 'a'"
            )
        else:
            conn.execute("INSERT INTO suppressed_models VALUES ('suppressed')")
            suppressed_id = dims_seed.dim_id(conn, "dim_model", "suppressed")
            conn.execute(
                "UPDATE records r SET model_id = %s "
                "FROM files f WHERE r.file_id = f.file_id "
                "AND f.file_key = 'a'", (suppressed_id,)
            )
            conn.execute(
                "UPDATE tool_uses t SET model_id = %s "
                "FROM files f WHERE t.file_id = f.file_id "
                "AND f.file_key = 'a'", (suppressed_id,)
            )
        conn.commit()
        ingest_scope.capture_and_add(scope, conn, dirty)
    return dirty


@pytest.mark.parametrize(
    "shape", ("delete_winner", "new_winner", "move_remove", "null_ts", "suppressed")
)
def test_differential_shapes(fresh_db, shape):
    """Incremental promotions and moved/deleted contributions equal full."""
    _seed_rollups()
    scope = ingest_scope.Scope(False, "test", 2000)
    _apply_shape(scope, shape)
    ingest.purge_suppressed(scope)
    ingest.recompute_canonical(scope)
    ingest.resolve_teammate_agent_types(scope)
    _rebuild_rollups(scope)
    incremental = _derived_snapshot()

    ingest.purge_suppressed()
    ingest.recompute_canonical()
    ingest.resolve_teammate_agent_types()
    _rebuild_rollups()
    assert incremental == _derived_snapshot()


def test_dirty_hour_bucket_endpoint_is_half_open():
    """A bucket at the expanded dirty interval's end is not touched."""
    hour = datetime(2026, 1, 1, 13, tzinfo=timezone.utc)
    assert _overlapping_bucket_starts(hour, 3600) == [
        hour, hour + timedelta(hours=1)]


def test_incremental_alias_phase_keeps_zero_argument_seam(
        fresh_db, mini_r2_env, monkeypatch):
    """The alias phase resolves its patch seam through ingest globals."""
    assert ingest.run_ingest(trigger="manual")["error"] is None
    scope = ingest_scope.begin_scope()
    assert not scope.full
    calls = []
    monkeypatch.setattr(ingest, "rekey_folded_projects", lambda: calls.append(1) or 0)
    try:
        ingest._rebuild_derived_state()  # pylint: disable=protected-access
    finally:
        ingest_scope.finish_scope()
    assert calls == [1]


def test_late_full_promotion_does_not_complete_incremental_rollups(
        fresh_db, mini_r2_env, monkeypatch, caplog):
    """Promotion after the first rollup cannot claim a full rebuild."""
    monkeypatch.setattr(timing, "TIMING_ON", True)
    assert ingest.run_ingest(trigger="manual")["error"] is None
    path = mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    original = ingest.rebuild_rollup
    with db.viz_conn() as conn:
        previous_full_at = conn.execute(
            "SELECT last_full_at FROM ingest_derived_state WHERE singleton"
        ).fetchone()
    assert previous_full_at is not None

    def promote_after_rollups_start():
        rows = original()
        scope = ingest_scope.current_scope()
        assert scope is not None and not scope.full
        scope.promote_full("late test promotion")
        return rows

    monkeypatch.setattr(ingest, "rebuild_rollup", promote_after_rollups_start)
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["reparsed"] == 1
    with db.viz_conn() as conn:
        state = conn.execute(
            "SELECT complete, last_full_at FROM ingest_derived_state "
            "WHERE singleton"
        ).fetchone()
    assert state is not None and state[0] is False
    assert state[1] == previous_full_at[0]

    monkeypatch.setattr(ingest, "rebuild_rollup", original)
    caplog.clear()
    with caplog.at_level("INFO", logger="claudit.ingest"):
        recovered = ingest.run_ingest(trigger="manual")
    assert recovered["error"] is None
    assert "scope=full" in _timing_lines(caplog)[-1]


def test_reserved_project_reseed_never_attempts_an_insert_when_present(
        fresh_db):
    """The latency rebuild's reserved-projects re-seed (proj_id 0, R8's
    all-projects row) must do NOTHING when the row already stands — the
    usual case. A BEFORE INSERT probe counts ATTEMPTS (it fires before
    ON CONFLICT DO NOTHING can resolve the conflict), so the unguarded
    INSERT ... VALUES form reddens here with 1 attempt."""
    with db.viz_conn() as conn:
        conn.execute("CREATE TABLE _reseed_probe (n int NOT NULL)")
        conn.execute(
            "CREATE FUNCTION _count_reseed() RETURNS trigger AS $fn$ "
            "BEGIN INSERT INTO _reseed_probe VALUES (1); RETURN NEW; END; "
            "$fn$ LANGUAGE plpgsql")
        conn.execute(
            "CREATE TRIGGER _reseed_attempts BEFORE INSERT ON projects "
            "FOR EACH ROW EXECUTE FUNCTION _count_reseed()")
        conn.commit()
    ingest.rebuild_latency_rollup()
    with db.viz_conn() as conn:
        attempts = conn.execute(
            "SELECT COUNT(*) FROM _reseed_probe").fetchone()
    assert attempts is not None and attempts[0] == 0, (
        "the re-seed attempted an insert although proj_id 0 exists")


def test_reserved_project_reseed_restores_the_row_when_missing(fresh_db):
    """End state the guard must not change: a wholesale projects wipe
    takes the reserved row, and the next latency rebuild puts it back."""
    with db.viz_conn() as conn:
        conn.execute("DELETE FROM projects WHERE proj_id = 0")
        count = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE proj_id = 0"
        ).fetchone()
        assert count is not None and count[0] == 0
        conn.commit()
    ingest.rebuild_latency_rollup()
    with db.viz_conn() as conn:
        row = conn.execute(
            "SELECT project_id FROM projects WHERE proj_id = 0"
        ).fetchone()
    assert row == ("",), row
