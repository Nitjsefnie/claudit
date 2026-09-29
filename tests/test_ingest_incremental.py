"""Incremental derived-state equivalence and scope selection tests."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture as ingest_fresh_db_fixture,
    _mini_r2_env_fixture as ingest_mini_r2_env,
)
from backend import constants, db, ingest, pricing, timing

_ROOT = Path(__file__).resolve().parent.parent
_PARSER_FIXTURES = _ROOT / "fixtures" / "parser"
_ROLLUPS = {
    "usage_rollup": "session_id, hour, model, provider, is_main, long_context",
    "tool_rollup": "hour, project_id, model, tool_name",
    "tool_error_rollup": "hour, project_id, model, tool_name, error_kind",
    "dispatch_rollup": "hour, project_id, agent_type, agent_model",
    "dispatch_brief_rollup": "hour, project_id, agent_type, brief_ref",
    "latency_rollup": "bucket_s, bucket, project_id, model",
    "ctx_cost_rollup": "hour, project_id, model, ctx_bucket",
    "agent_rollup": "hour, project_id, model, agent_type",
}
_MEMBER_RELATIVE = (
    "projT/sess-t/subagents/"
    "agent-amigrate-rest-0123456789abcdef.jsonl"
)
_TEAMMATE_SIDECAR = json.dumps({
    "agentType": "migrate-rest",
    "name": "migrate-rest",
    "taskKind": "in_process_teammate",
    "teamName": "session-sess-t",
}).encode()


def _single_turn(record_uuid: str, session: str, request_id: str,
                 user_uuid: str) -> bytes:
    """Specialize the small checked-in JSONL fixture for one mirror file."""
    raw = (_PARSER_FIXTURES / "single_turn.jsonl").read_bytes()
    return (raw.replace(b'"u1"', f'"{user_uuid}"'.encode())
            .replace(b'"a1"', f'"{record_uuid}"'.encode())
            .replace(b'"req-1"', f'"{request_id}"'.encode())
            .replace(b'"sess-1"', f'"{session}"'.encode()))


def _timing_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records
            if record.name == "claudit.ingest"
            and record.getMessage().startswith("TIMING ingest ")]


def _derived_snapshot() -> dict[str, list[tuple]]:
    """Return every rollup row and the canonical/agent flags, all ordered."""
    with db.viz_conn() as conn:
        snapshot = {
            table: conn.execute(
                db.sql_text(f"SELECT * FROM {table} ORDER BY {order_by}")
            ).fetchall()
            for table, order_by in _ROLLUPS.items()
        }
        snapshot["records.is_canonical"] = conn.execute(
            "SELECT file_key, line_num, is_canonical FROM records "
            "ORDER BY file_key, line_num"
        ).fetchall()
        snapshot["tool_uses.is_canonical"] = conn.execute(
            "SELECT file_key, line_num, idx, is_canonical FROM tool_uses "
            "ORDER BY file_key, line_num, idx"
        ).fetchall()
        snapshot["files.agent_type"] = conn.execute(
            "SELECT file_key, agent_type FROM files ORDER BY file_key"
        ).fetchall()
    return snapshot


def _prepare_mixed_change_mirror(mini_r2_env: Path) -> None:
    """Seed a project fold and teammate member before the initial ingest."""
    with db.viz_conn() as conn:
        conn.execute(
            "INSERT INTO project_aliases (pattern, project_id) "
            "VALUES (%s, %s)", ("raw-project-%", "projA"))
        conn.commit()

    # These clean files keep five changed files below the 20% dirty-file
    # threshold while staying entirely within fixture-backed input data.
    for index in range(20):
        session = f"support-{index:02d}"
        path = mini_r2_env / "projZ" / session / f"{session}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes(_single_turn(
            f"support-record-{index}", session,
            f"support-request-{index}", f"support-user-{index}"))

    member = mini_r2_env / _MEMBER_RELATIVE
    member.parent.mkdir(parents=True)
    member.write_bytes(
        (_PARSER_FIXTURES / "teammate_member.jsonl").read_bytes())
    member.with_name(member.stem + ".meta.json").write_bytes(
        _TEAMMATE_SIDECAR)


def _run_and_assert_scope(caplog: pytest.LogCaptureFixture,
                          expected_scope: str) -> dict[str, object]:
    """Run ingest with timing enabled and check its reported scope."""
    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert f"scope={expected_scope}" in _timing_lines(caplog)[-1]
    return result


def _canonical_file(record_uuid: str) -> tuple[str, ...] | None:
    """Return the current canonical file for one UUID."""
    with db.viz_conn() as conn:
        return conn.execute(
            "SELECT file_key FROM records WHERE uuid = %s AND is_canonical",
            (record_uuid,),
        ).fetchone()


def _apply_mixed_changes(mini_r2_env: Path, old_winner_key: str) -> None:
    """Delete and reparse files, add a new winner, alias file and teammate."""
    old_winner_path = mini_r2_env / old_winner_key.removeprefix("claude/")
    old_winner_path.unlink()

    changed = mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl"
    changed.write_bytes(
        changed.read_bytes().rstrip(b"\n") + b"\n" + _single_turn(
            "appended-record", "sess-A", "appended-request", "appended-user"))

    new_winner = mini_r2_env / "projB" / "000-first" / "000-first.jsonl"
    new_winner.parent.mkdir(parents=True)
    new_winner.write_bytes(_single_turn(
        "shared-uuid-1", "sess-C", "new-shared-request", "new-shared-user"))

    alias_file = mini_r2_env / "raw-project-new" / "sess-alias" / "sess-alias.jsonl"
    alias_file.parent.mkdir(parents=True)
    alias_file.write_bytes(_single_turn(
        "alias-record", "sess-alias", "alias-request", "alias-user"))

    lead = mini_r2_env / "projT" / "sess-t" / "sess-t.jsonl"
    lead.parent.mkdir(parents=True, exist_ok=True)
    lead.write_bytes((_PARSER_FIXTURES / "teammate_lead.jsonl").read_bytes())


def _assert_incremental_mixed_rows() -> None:
    """Check canonical promotion, alias folding and teammate resolution."""
    winner = _canonical_file("shared-uuid-1")
    with db.viz_conn() as conn:
        alias_project = conn.execute(
            "SELECT project_id FROM files WHERE file_key = %s",
            ("claude/raw-project-new/sess-alias/sess-alias.jsonl",),
        ).fetchone()
        teammate_type = conn.execute(
            "SELECT agent_type FROM files WHERE file_key = %s",
            ("claude/" + _MEMBER_RELATIVE,),
        ).fetchone()
    assert winner == ("claude/projB/000-first/000-first.jsonl",)
    assert alias_project == ("projA",)
    assert teammate_type == ("implementer",)


def _force_full_and_compare(caplog: pytest.LogCaptureFixture,
                            incremental_snapshot: dict[str, list[tuple]]) -> None:
    """Clear derived state to force full mode and compare every derived row."""
    with db.viz_conn() as conn:
        conn.execute("DELETE FROM ingest_derived_state")
        conn.commit()
    caplog.clear()
    _run_and_assert_scope(caplog, "full")
    assert _derived_snapshot() == incremental_snapshot


def test_incremental_rebuild_matches_full_after_mixed_changes(
        fresh_db, mini_r2_env, monkeypatch, caplog):
    """Every affected derived row must equal the full rebuild reference."""
    monkeypatch.setattr(timing, "TIMING_ON", True)
    _prepare_mixed_change_mirror(mini_r2_env)
    _run_and_assert_scope(caplog, "full")
    old_winner = _canonical_file("shared-uuid-1")
    assert old_winner is not None

    _apply_mixed_changes(mini_r2_env, old_winner[0])
    caplog.clear()
    incremental = _run_and_assert_scope(caplog, "incremental")
    assert incremental["inserted"] == 3
    assert incremental["reparsed"] == 1
    assert incremental["deleted"] == 1
    _assert_incremental_mixed_rows()
    _force_full_and_compare(caplog, _derived_snapshot())


@pytest.mark.parametrize("changed_input", ("suppressed", "aliases", "version"))
def test_fingerprint_changes_force_full_rebuild(
        fresh_db, mini_r2_env, monkeypatch, caplog, changed_input):
    """Every input to the derived fingerprint invalidates incremental state."""
    monkeypatch.setattr(timing, "TIMING_ON", True)
    assert ingest.run_ingest(trigger="manual")["error"] is None
    with db.viz_conn() as conn:
        if changed_input == "suppressed":
            conn.execute(
                "INSERT INTO suppressed_models (pattern) VALUES (%s)",
                ("unmatched-model-%",),
            )
        elif changed_input == "aliases":
            conn.execute(
                "INSERT INTO project_aliases (pattern, project_id) "
                "VALUES (%s, %s)", ("unmatched-project-%", "projA"),
            )
        conn.commit()
    if changed_input == "version":
        monkeypatch.setattr(
            constants, "DERIVED_STATE_VERSION",
            constants.DERIVED_STATE_VERSION + "-test",
        )

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert "scope=full" in _timing_lines(caplog)[-1]


@pytest.mark.parametrize("change", ("incomplete", "stale"))
def test_incomplete_or_old_full_state_forces_full(
        fresh_db, mini_r2_env, monkeypatch, caplog, change):
    monkeypatch.setattr(timing, "TIMING_ON", True)
    assert ingest.run_ingest(trigger="manual")["error"] is None
    with db.viz_conn() as conn:
        if change == "incomplete":
            conn.execute(
                "UPDATE ingest_derived_state SET complete = FALSE "
                "WHERE singleton"
            )
        else:
            conn.execute(
                "UPDATE ingest_derived_state "
                "SET last_full_at = now() - interval '25 hours' "
                "WHERE singleton"
            )
        conn.commit()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert "scope=full" in _timing_lines(caplog)[-1]


def test_dirty_file_threshold_forces_full(fresh_db, mini_r2_env,
                                          monkeypatch, caplog):
    monkeypatch.setattr(timing, "TIMING_ON", True)
    assert ingest.run_ingest(trigger="manual")["error"] is None
    for session in ("sess-A", "sess-B"):
        path = mini_r2_env / "projA" / session / f"{session}.jsonl"
        path.write_bytes(path.read_bytes().rstrip(b"\n") + b"\n")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert "scope=full" in _timing_lines(caplog)[-1]


def test_reprice_changes_force_full_rebuild(fresh_db, mini_r2_env,
                                            monkeypatch, caplog):
    # Issue #339: only rows whose rate-derived data moved count as
    # reprice changes, so the simulated bump mutates the loaded rate
    # tables (a restamp-only bump would rebuild nothing).
    monkeypatch.setattr(timing, "TIMING_ON", True)
    assert ingest.run_ingest(trigger="manual")["error"] is None
    monkeypatch.setattr(pricing, "MODEL_RATES", {
        **pricing.MODEL_RATES,
        "claude-opus-4-7": {"fresh": 9.0, "create_5m": 9.5, "create_1h": 9.75,
                            "read": 0.9, "output": 19.0},
    })
    monkeypatch.setattr(constants, "PRICING_VERSION",
                        str(int(constants.PRICING_VERSION) + 1))
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert "scope=full" in _timing_lines(caplog)[-1]


def test_dirty_latency_with_null_timestamp_forces_full(
        fresh_db, mini_r2_env, monkeypatch, caplog):
    monkeypatch.setattr(timing, "TIMING_ON", True)
    assert ingest.run_ingest(trigger="manual")["error"] is None
    file_key = "claude/projA/sess-A/sess-A.jsonl"
    with db.viz_conn() as conn:
        conn.execute(
            "INSERT INTO records (file_key, line_num, uuid, model, "
            "reply_latency_s) VALUES (%s, %s, %s, %s, %s)",
            (file_key, 9000, "null-time-latency", "test-model", 5.0),
        )
        conn.commit()
    changed = mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl"
    changed.write_bytes(changed.read_bytes().rstrip(b"\n") + b"\n")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert "scope=full" in _timing_lines(caplog)[-1]


def test_abort_after_persist_leaves_incomplete_state_for_full_recovery(
        fresh_db, mini_r2_env, monkeypatch, caplog):  # pylint: disable=too-many-locals
    monkeypatch.setattr(timing, "TIMING_ON", True)
    assert ingest.run_ingest(trigger="manual")["error"] is None
    added = mini_r2_env / "projA" / "sess-new" / "sess-new.jsonl"
    added.parent.mkdir()
    added.write_bytes(_single_turn(
        "after-abort", "sess-new", "after-abort-request", "after-abort-user"))
    real = ingest._fetch_parse_persist  # pylint: disable=protected-access

    def persist_then_abort(todo, parser_version, failed, seen_keys):
        persisted = real(todo, parser_version, failed, seen_keys)
        ingest._SHUTDOWN.set()  # pylint: disable=protected-access
        return persisted

    monkeypatch.setattr(ingest, "_fetch_parse_persist", persist_then_abort)
    try:
        aborted = ingest.run_ingest(trigger="manual")
    finally:
        ingest._SHUTDOWN.clear()  # pylint: disable=protected-access
    assert aborted["aborted"] is True
    with db.viz_conn() as conn:
        complete = conn.execute(
            "SELECT complete FROM ingest_derived_state WHERE singleton"
        ).fetchone()
    assert complete == (False,)

    monkeypatch.setattr(ingest, "_fetch_parse_persist", real)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        recovered = ingest.run_ingest(trigger="manual")
    assert recovered["error"] is None
    assert "scope=full" in _timing_lines(caplog)[-1]
    after_full = _derived_snapshot()
    assert ingest._rebuild_derived_state() >= 0  # pylint: disable=protected-access
    assert _derived_snapshot() == after_full


def test_tied_latency_outliers_are_deterministic(
        fresh_db, mini_r2_env):
    assert ingest.run_ingest(trigger="manual")["error"] is None
    with db.viz_conn() as conn:
        source = conn.execute(
            "SELECT file_key, project_id FROM files ORDER BY file_key LIMIT 1"
        ).fetchone()
        assert source is not None
        file_key, project_id = source
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO records (file_key, line_num, ts, model, "
                "reply_latency_s) VALUES (%s, %s, %s, %s, %s)",
                [(file_key, 1000 + index, "2026-01-01T12:00:00Z",
                  "tied-model", 5.0) for index in range(100)],
            )
        conn.commit()
    ingest.rebuild_latency_rollup()
    with db.viz_conn() as conn:
        first = conn.execute(
            "SELECT outliers FROM latency_rollup WHERE bucket_s = 3600 "
            "AND project_id = %s AND model = 'tied-model'", (project_id,),
        ).fetchone()
    ingest.rebuild_latency_rollup()
    with db.viz_conn() as conn:
        second = conn.execute(
            "SELECT outliers FROM latency_rollup WHERE bucket_s = 3600 "
            "AND project_id = %s AND model = 'tied-model'", (project_id,),
        ).fetchone()
    assert first is not None and second == first
