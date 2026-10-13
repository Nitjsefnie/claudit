"""Optional storage accepts NULL without changing old-writer defaults."""
from __future__ import annotations

import psycopg
import pytest
from psycopg import sql

from backend import db, flags
from tests import scratch_db


@pytest.mark.db
def test_optional_fields_nullable_and_old_defaults_compatible(monkeypatch):
    name = scratch_db.create_database("collection_schema")
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
    monkeypatch.setattr(flags, "CONFIG", flags.resolve_config({}))
    db.reset_viz_pool()
    try:
        db.apply_schema()
        db.apply_schema()
        with db.viz_conn() as conn:
            conn.execute(
                "INSERT INTO projects VALUES ('p', 'Project', now(), now())")
            for key in ("on", "off", "old"):
                conn.execute(
                    "INSERT INTO files (file_key, project_id, session_id, is_main, "
                    "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, parser_version) "
                    "VALUES (%s, 'p', 's', TRUE, 'e', 10, now(), now(), %s)",
                    (key, "legacy-writer"))
                conn.execute(
                    "INSERT INTO records (file_key, line_num, model) "
                    "VALUES (%s, 1, 'synthetic')", (key,))
                conn.execute(
                    "INSERT INTO tool_uses (file_key, line_num, idx, tool_name) "
                    "VALUES (%s, 1, 0, 'Read')", (key,))

            values = {
                "records": {
                    "turn_flags": ["user_prompt"], "turn_tool_results": 2,
                    "cli_version": "example", "stop_reason": "end_turn",
                    "effort": "high", "cache_write_tokens": 12,
                    "generation_s": 2.5, "output_tokens_per_s": 10.0,
                },
                "tool_uses": {
                    "result_chars": 15, "read_kind": "whole",
                    "read_targets": ["sample.py"], "write_targets": ["out.py"],
                    "is_reread": True, "error_text": "failure",
                    "error_kind": "tool_error", "agent_model": "synthetic",
                    "dispatch_prompt_chars": 21, "dispatch_brief_ref": True,
                },
                "files": {"models": ["synthetic"], "prompt_count": 3, "turn_count": 4},
            }
            registered = {table: set() for table in values}
            for spec in flags.ALL_COLLECTIONS_ON.specs:
                registered["records"].update(spec.record_fields)
                registered["tool_uses"].update(spec.tool_fields)
                registered["files"].update(spec.file_fields)
            assert registered == {table: set(fields) for table, fields in values.items()}
            for table, fields in values.items():
                assignments = sql.SQL(", ").join(
                    sql.SQL("{} = %s").format(sql.Identifier(field)) for field in fields)
                update = sql.SQL("UPDATE {} SET {} WHERE file_key = %s").format(
                    sql.Identifier(table), assignments)
                conn.execute(update, (*fields.values(), "on"))
                conn.execute(update, (*(None for _ in fields), "off"))
                query = sql.SQL("SELECT {} FROM {} WHERE file_key = %s").format(
                    sql.SQL(", ").join(map(sql.Identifier, fields)), sql.Identifier(table))
                assert conn.execute(query, ("on",)).fetchone() == tuple(fields.values())
                assert conn.execute(query, ("off",)).fetchone() == (None,) * len(fields)

            conn.execute("UPDATE files SET collection_flags = %s WHERE file_key = 'on'",
                         (["turn_context"],))
            assert conn.execute(
                "SELECT collection_flags FROM files WHERE file_key = 'on'").fetchone() == (["turn_context"],)
            assert conn.execute(
                "SELECT collection_flags FROM files WHERE file_key = 'off'").fetchone() == (None,)
            assert conn.execute(
                "SELECT models, prompt_count, turn_count, ctx_turns, prompt_ts, agent_type "
                "FROM files WHERE file_key = 'old'").fetchone() == ([], 0, 0, [], [], "general-purpose")
            assert conn.execute(
                "SELECT turn_flags, turn_tool_results, cache_write_tokens, request_id, "
                "fresh_tokens, output_tokens, is_canonical FROM records "
                "WHERE file_key = 'old'").fetchone() == ([], 0, 0, "", 0, 0, True)
            assert conn.execute(
                "SELECT file_key, model, is_canonical FROM records "
                "WHERE file_key = 'off'").fetchone() == ("off", "synthetic", True)
            assert conn.execute(
                "SELECT file_key, tool_name, lines_added, lines_deleted FROM tool_uses "
                "WHERE file_key = 'off'").fetchone() == ("off", "Read", 0, 0)
            # Retained NOT NULL constraints remain enforced.
            with pytest.raises(psycopg.errors.NotNullViolation):
                conn.execute("UPDATE records SET model = NULL WHERE file_key = 'off'")
            conn.rollback()
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)
