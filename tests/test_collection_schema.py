"""Optional storage accepts NULL without changing old-writer defaults."""
from __future__ import annotations

import psycopg
import pytest
from psycopg import sql

from backend import db, flags, turn_flag_bits
from tests import scratch_db

# The collection registry names TRANSPORT fields; stores several of
# them as dim FK ids, one as an INT4 mask and one as a boolean. Each is
# written through its own translation and read back through it too, so
# the round-trip still proves the column accepts the value and NULL.
_DIM_FIELDS = {
    "records": {"cli_version": "dim_cli_version",
                "stop_reason": "dim_stop_reason",
                "effort": "dim_effort"},
    "tool_uses": {"error_kind": "dim_error_kind",
                  "agent_model": "dim_agent_model"},
}
_MASK_FIELDS = {("records", "turn_flags")}
_TEXT_BOOL_FIELDS = {("tool_uses", "read_kind"): "whole"}


def _where(table: str):
    """files keeps file_key; records/tool_uses key on file_id."""
    if table == "files":
        return sql.SQL("file_key = %s")
    return sql.SQL(
        "file_id = (SELECT file_id FROM files WHERE file_key = %s)")


def _stored_value(table: str, field: str, value):
    """The (column, value) this shape stores one registry field as."""
    dim = _DIM_FIELDS.get(table, {}).get(field)
    if dim is not None:
        return field + "_id", value          # resolved through the dim
    if (table, field) in _MASK_FIELDS:
        return field, turn_flag_bits.flags_to_mask(value)
    if (table, field) in _TEXT_BOOL_FIELDS:
        return field, value == _TEXT_BOOL_FIELDS[(table, field)]
    return field, value


def _getter(table: str, field: str):
    """The expression that reads one stored field back as its transport
    value (a dim id reads as the dim's TEXT)."""
    dim = _DIM_FIELDS.get(table, {}).get(field)
    if dim is not None:
        return sql.SQL("(SELECT value FROM {} WHERE id = t.{})").format(
            sql.Identifier(dim), sql.Identifier(field + "_id"))
    return sql.Identifier(field)


def _roundtrip_fields(conn, table: str, fields: dict) -> None:
    """Write every registry field, read it back, then NULL it and read
    that — each through the translation this shape stores it with."""
    setters, getters, on_params, off_params, expected = [], [], [], [], []
    for field, value in fields.items():
        dim = _DIM_FIELDS.get(table, {}).get(field)
        column, stored = _stored_value(table, field, value)
        if dim is not None:
            setters.append(sql.SQL(
                "{} = (SELECT id FROM {} WHERE value = %s)"
            ).format(sql.Identifier(column), sql.Identifier(dim)))
        else:
            setters.append(sql.SQL("{} = %s").format(sql.Identifier(column)))
        getters.append(_getter(table, field))
        on_params.append(stored)
        off_params.append(None)
        expected.append(stored)
    update = sql.SQL("UPDATE {} SET {} WHERE {}").format(
        sql.Identifier(table), sql.SQL(", ").join(setters), _where(table))
    conn.execute(update, (*on_params, "on"))
    conn.execute(update, (*off_params, "off"))
    query = sql.SQL("SELECT {} FROM {} t WHERE {}").format(
        sql.SQL(", ").join(getters), sql.Identifier(table), _where(table))
    assert conn.execute(query, ("on",)).fetchone() == tuple(expected)
    assert conn.execute(query, ("off",)).fetchone() == (None,) * len(fields)


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
            conn.execute(
                "INSERT INTO dim_model (value) VALUES ('synthetic') "
                "ON CONFLICT (value) DO NOTHING")
            conn.execute(
                "INSERT INTO dim_tool_name (value) VALUES ('Read') "
                "ON CONFLICT (value) DO NOTHING")
            # every dim value the round-trip below writes must exist to
            # resolve to an id (a missing one would store NULL)
            for dim, value in (
                    ("dim_cli_version", "example"),
                    ("dim_stop_reason", "end_turn"),
                    ("dim_effort", "high"),
                    ("dim_error_kind", "tool_error"),
                    ("dim_agent_model", "synthetic")):
                conn.execute(sql.SQL(
                    "INSERT INTO {} (value) VALUES (%s) "
                    "ON CONFLICT (value) DO NOTHING").format(
                        sql.Identifier(dim)), (value,))
            for key in ("on", "off", "old"):
                conn.execute(
                    "INSERT INTO files (file_key, project_id, session_id, is_main, "
                    "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, parser_version) "
                    "VALUES (%s, 'p', 's', TRUE, 'e', 10, now(), now(), %s)",
                    (key, "legacy-writer"))
                conn.execute(
                    "INSERT INTO records (file_id, line_num, ts, model_id) "
                    "SELECT file_id, 1, now(), "
                    "(SELECT id FROM dim_model WHERE value = 'synthetic') "
                    "FROM files WHERE file_key = %s", (key,))
                conn.execute(
                    "INSERT INTO tool_uses (file_id, line_num, idx, ts, "
                    "tool_name_id) "
                    "SELECT file_id, 1, 0, now(), "
                    "(SELECT id FROM dim_tool_name WHERE value = 'Read') "
                    "FROM files WHERE file_key = %s", (key,))

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
                _roundtrip_fields(conn, table, fields)

            conn.execute("UPDATE files SET collection_flags = %s WHERE file_key = 'on'",
                         (["turn_context"],))
            assert conn.execute(
                "SELECT collection_flags FROM files WHERE file_key = 'on'").fetchone() == (["turn_context"],)
            assert conn.execute(
                "SELECT collection_flags FROM files WHERE file_key = 'off'").fetchone() == (None,)
            assert conn.execute(
                "SELECT models, prompt_count, turn_count, ctx_ts, ctx_line, ctx_input, ctx_output, prompt_ts, agent_type "
                "FROM files WHERE file_key = 'old'").fetchone() == ([], 0, 0, [], [], [], [], [], "general-purpose")
            assert conn.execute(
                "SELECT turn_flags, turn_tool_results, cache_write_tokens, request_id, "
                "fresh_tokens, output_tokens, is_canonical FROM records "
                "WHERE file_id = (SELECT file_id FROM files WHERE file_key = 'old')"
                ).fetchone() == (0, 0, 0, "", 0, 0, True)
            assert conn.execute(
                "SELECT f.file_key, dm.value, r.is_canonical FROM records r "
                "JOIN files f ON f.file_id = r.file_id "
                "JOIN dim_model dm ON dm.id = r.model_id "
                "WHERE f.file_key = 'off'").fetchone() == ("off", "synthetic", True)
            assert conn.execute(
                "SELECT f.file_key, dt.value, t.lines_added, t.lines_deleted "
                "FROM tool_uses t JOIN files f ON f.file_id = t.file_id "
                "JOIN dim_tool_name dt ON dt.id = t.tool_name_id "
                "WHERE f.file_key = 'off'").fetchone() == ("off", "Read", 0, 0)
            # Retained NOT NULL constraints remain enforced (the dim FK).
            with pytest.raises(psycopg.errors.NotNullViolation):
                conn.execute(
                    "UPDATE records SET model_id = NULL "
                    "WHERE file_id = (SELECT file_id FROM files "
                    "                 WHERE file_key = 'off')")
            conn.rollback()
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)
