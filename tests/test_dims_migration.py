"""The startup migration: old text shape -> surrogate ids.

Every assertion here is about what `db.apply_schema()` leaves behind on
a database that carried the legacy shape with data in it — the deploy
path R1 sanctions: fact and rollup tables drop-and-recreate (refilled by
the PARSER_VERSION bump's full reparse), `projects`/`files` migrate in
place keeping their rows, and re-running the whole schema is a no-op.
"""
from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest

from backend import db
from tests import scratch_db

# The legacy chain (every schema file but the four migration
# files) lives beside SCHEMA: scratch_db.OLD_SCHEMA.

_OLD_FILE_KEY = "bucket/projX/sess-1/main.jsonl"
_OLD_SESSION = "sess-1"
_OLD_PROJECT = "projX"


def _columns(conn, table: str) -> set[str]:
    return scratch_db.table_columns(conn, table)


def _pk_def(conn, table: str) -> str:
    row = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = %s::regclass AND contype = 'p'",
        (table,),
    ).fetchone()
    assert row is not None, f"{table} has no primary key"
    return row[0]


def _index_names(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
    ).fetchall()}


def _seed_old_shape(conn) -> None:
    """One project, one file, one record, one call, one rollup row each.

    Build the legacy TEXT dimension columns here: current schema.sql must
    not add retired columns only for the migration to drop them in the same
    transaction, consuming attnums. A legacy deployment already has them.
    """
    for ddl in (
        "ALTER TABLE records ADD COLUMN IF NOT EXISTS provider TEXT",
        "ALTER TABLE records ADD COLUMN IF NOT EXISTS stop_reason TEXT",
        "ALTER TABLE records ADD COLUMN IF NOT EXISTS effort TEXT",
        "ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS model TEXT",
        "ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS error_kind TEXT",
        "ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS agent_type TEXT",
        "ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS agent_model TEXT",
    ):
        conn.execute(ddl)
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO projects (project_id, display_name, "
        "first_seen_at, last_seen_at) VALUES (%s, %s, %s, %s)",
        (_OLD_PROJECT, _OLD_PROJECT, now, now),
    )
    conn.execute(
        "INSERT INTO files (file_key, project_id, session_id, is_main, "
        "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
        "parser_version) VALUES (%s, %s, %s, TRUE, 'etag', 1, %s, %s, '0')",
        (_OLD_FILE_KEY, _OLD_PROJECT, _OLD_SESSION, now, now),
    )
    conn.execute(
        "INSERT INTO records (file_key, line_num, uuid, ts, model, "
        "provider, stop_reason, effort, cost_usd) "
        "VALUES (%s, 1, 'u-1', %s, 'model-a', 'prov-x', 'end_turn', "
        "'high', 1.5)",
        (_OLD_FILE_KEY, now),
    )
    conn.execute(
        "INSERT INTO tool_uses (file_key, line_num, idx, tool_name, "
        "model, error_kind, agent_type, agent_model) "
        "VALUES (%s, 1, 0, 'Bash', 'model-a', 'failed', 'explore', "
        "'model-b')",
        (_OLD_FILE_KEY,),
    )
    conn.execute(
        "INSERT INTO usage_rollup (session_id, project_id, hour, model, "
        "provider, first_ts, last_ts, requests) "
        "VALUES (%s, %s, date_trunc('hour', %s), 'model-a', '', %s, %s, 1)",
        (_OLD_SESSION, _OLD_PROJECT, now, now, now),
    )
    conn.execute(
        "INSERT INTO latency_rollup (bucket_s, bucket, project_id, model, "
        "n) VALUES (3600, %s, '', 'model-a', 5)",
        (now,),
    )
    conn.commit()


@pytest.mark.db
def test_migration_rebuilds_facts_and_migrates_identity_in_place(
        monkeypatch,
):
    """Old-shape deploy -> the shape, with projects/files kept."""
    name = scratch_db.create_database("dims_migrate", schema=scratch_db.OLD_SCHEMA)
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
    db.reset_viz_pool()
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            _seed_old_shape(conn)
            cols = _columns(conn, "records")
            assert "file_key" in cols and "file_id" not in cols

        db.apply_schema()

        with psycopg.connect(f"postgresql:///{name}") as conn:
            # Identity migrated IN PLACE (R1): rows kept, surrogate PK,
            # file_key still the UNIQUE stored identity (SV-FILES-RECORDS).
            row = conn.execute(
                "SELECT file_id, file_key, project_id, session_id "
                "FROM files WHERE file_key = %s", (_OLD_FILE_KEY,),
            ).fetchone()
            assert row is not None and row[1] == _OLD_FILE_KEY
            assert isinstance(row[0], int)
            files_cols = _columns(conn, "files")
            assert {"file_id", "file_key", "project_id", "session_id"} <= files_cols
            assert "PRIMARY KEY (file_id)" in _pk_def(conn, "files")
            uniq = conn.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'files'::regclass AND contype = 'u'"
            ).fetchall()
            assert any("file_key" in d[0] for d in uniq), uniq

            proj = conn.execute(
                "SELECT proj_id, project_id FROM projects "
                "WHERE project_id = %s", (_OLD_PROJECT,),
            ).fetchone()
            assert proj is not None and isinstance(proj[0], int)

            # Facts rebuilt empty (refilled by the forced full reparse),
            # in the NEW shape.
            assert (conn.execute(
                "SELECT COUNT(*) FROM records").fetchone() or (-1,))[0] == 0
            assert (conn.execute(
                "SELECT COUNT(*) FROM tool_uses").fetchone() or (-1,))[0] == 0
            rec_cols = _columns(conn, "records")
            assert "file_key" not in rec_cols
            assert {"file_id", "model_id", "provider_id", "stop_reason_id",
                    "effort_id"} <= rec_cols
            # The staleness markers ride the records_price sidecar (
            # R4), nullable beside the fact table's own nullable FKs.
            assert {"file_id", "line_num", "pricing_version_id",
                    "rate_fingerprint_id",
                    "cost_usd"} <= _columns(conn, "records_price")
            assert "PRIMARY KEY (file_id, line_num)" in _pk_def(conn, "records")
            tu_cols = _columns(conn, "tool_uses")
            assert "file_key" not in tu_cols and "tool_name" not in tu_cols
            assert {"file_id", "tool_name_id", "model_id", "error_kind_id",
                    "agent_type_id", "agent_model_id"} <= tu_cols
            assert "PRIMARY KEY (file_id, line_num, idx)" in _pk_def(
                conn, "tool_uses")

            # Rollups recreated with dim-FK grains (same semantic columns).
            assert "PRIMARY KEY (ses_id, hour, model_id, provider_id, " \
                   "is_main, long_context)" == _pk_def(conn, "usage_rollup")
            usage_cols = _columns(conn, "usage_rollup")
            assert "session_id" not in usage_cols and "provider" not in usage_cols
            assert {"ses_id", "proj_id", "model_id", "provider_id"} <= usage_cols
            for table, cols_wanted in (
                ("tool_rollup", {"proj_id", "model_id", "tool_name_id"}),
                ("tool_error_rollup", {"proj_id", "error_kind_id"}),
                ("dispatch_rollup", {"proj_id", "agent_type_id",
                                     "agent_model_id"}),
                ("dispatch_brief_rollup", {"proj_id", "agent_type_id"}),
                ("ctx_cost_rollup", {"proj_id", "model_id"}),
                ("agent_rollup", {"proj_id", "model_id", "agent_type_id"}),
                ("latency_rollup", {"proj_id", "model_id"}),
            ):
                got = _columns(conn, table)
                assert "project_id" not in got, table
                assert cols_wanted <= got, (table, got)
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)


@pytest.mark.db
def test_reserved_rows_stand_for_the_empty_grain_keys(monkeypatch):
    """R8: the all-projects row and the '' dimension vocabularies."""
    name = scratch_db.create_database("dims_reserved", schema=scratch_db.OLD_SCHEMA)
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
    db.reset_viz_pool()
    try:
        db.apply_schema()
        with psycopg.connect(f"postgresql:///{name}") as conn:
            zero_project = conn.execute(
                "SELECT project_id FROM projects WHERE proj_id = 0"
            ).fetchone()
            assert zero_project is not None and zero_project[0] == ""
        # The reserved project row survives the real orphan sweep: no
        # file ever references it, and that sweep is what deletes
        # file-less project rows on every ingest.
        from backend import ingest_orphans  # pylint: disable=import-outside-toplevel
        ingest_orphans._delete_orphan_projects()  # pylint: disable=protected-access
        with psycopg.connect(f"postgresql:///{name}") as conn:
            still = conn.execute(
                "SELECT 1 FROM projects WHERE proj_id = 0").fetchone()
            assert still is not None
            for table in ("dim_model", "dim_provider", "dim_agent_type",
                          "dim_agent_model"):
                row = conn.execute(
                    f"SELECT value FROM {table} WHERE id = 0"  # noqa: S608
                ).fetchone()
                assert row is not None and row[0] == "", table
            for table in ("dim_model", "dim_provider", "dim_tool_name",
                          "dim_agent_type", "dim_agent_model",
                          "dim_error_kind", "dim_stop_reason", "dim_effort",
                          "dim_rate_fingerprint", "dim_pricing_version"):
                assert conn.execute(
                    f"SELECT 1 FROM {table} LIMIT 1"  # noqa: S608
                ) is not None, table
            # The all-projects latency grain inserts against the reserved row.
            now = datetime.now(timezone.utc)
            conn.execute(
                "INSERT INTO latency_rollup (bucket_s, bucket, proj_id, "
                "model_id, n) SELECT 600, %s, 0, id, 1 FROM dim_model "
                "WHERE value = ''", (now,),
            )
            conn.commit()
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)


@pytest.mark.db
def test_dim_fk_columns_stay_nullable_for_switched_off_writers(
        monkeypatch,
):
    """#951 tolerance: a NULL dim FK inserts and reads as NULL."""
    name = scratch_db.create_database("dims_null", schema=scratch_db.OLD_SCHEMA)
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
    db.reset_viz_pool()
    try:
        db.apply_schema()
        now = datetime.now(timezone.utc)
        with psycopg.connect(f"postgresql:///{name}") as conn:
            conn.execute(
                "INSERT INTO projects (project_id, display_name, "
                "first_seen_at, last_seen_at) VALUES ('p', 'p', %s, %s)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO files (file_key, project_id, session_id, "
                "is_main, r2_etag, r2_size_bytes, r2_last_modified, "
                "parsed_at, parser_version) VALUES ('k/p/s/m.jsonl', 'p', "
                "'s', TRUE, 'e', 1, %s, %s, '0')", (now, now),
            )
            conn.execute(
                "INSERT INTO dim_model (value) VALUES ('m') "
                "ON CONFLICT (value) DO UPDATE SET value = EXCLUDED.value"
            )
            # Every nullable dim FK omitted: the row still inserts, and
            # the NULLs come back as NULL.
            conn.execute(
                "INSERT INTO records (file_id, line_num, model_id, ts) "
                "SELECT file_id, 1, id, %s FROM files, dim_model "
                "WHERE files.file_key = 'k/p/s/m.jsonl' AND "
                "dim_model.value = 'm'", (now,),
            )
            conn.execute("INSERT INTO dim_tool_name (value) VALUES ('Bash')")
            conn.execute(
                "INSERT INTO tool_uses (file_id, line_num, idx, "
                "tool_name_id, ts) "
                "SELECT file_id, 1, 0, id, %s FROM files, dim_tool_name "
                "WHERE files.file_key = 'k/p/s/m.jsonl'", (now,),
            )
            conn.execute(
                "INSERT INTO records_price (file_id, line_num) "
                "SELECT file_id, 1 FROM files "
                "WHERE files.file_key = 'k/p/s/m.jsonl'"
            )
            row = conn.execute(
                "SELECT provider_id, stop_reason_id, effort_id "
                "FROM records WHERE line_num = 1"
            ).fetchone()
            assert row == (None, None, None)
            side = conn.execute(
                "SELECT pricing_version_id, rate_fingerprint_id, "
                "long_context "
                "FROM records_price rp JOIN records r "
                "ON r.file_id = rp.file_id AND r.line_num = rp.line_num "
                "WHERE rp.line_num = 1"
            ).fetchone()
            assert side == (None, None, None)
            tu = conn.execute(
                "SELECT model_id, error_kind_id, agent_type_id, "
                "agent_model_id FROM tool_uses WHERE line_num = 1"
            ).fetchone()
            assert tu == (None, None, None, None)
            conn.commit()
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)


@pytest.mark.db
def test_reapplying_the_schema_preserves_rows_and_leaves_no_strays(
        monkeypatch,
):
    """Idempotent (SV-SCHEMA-AUTOAPPLY): a stamp change re-runs every
    file, and the retired TEXT columns its ADD COLUMNs would re-add are
    dropped again in the same transaction."""
    name = scratch_db.create_database("dims_idem", schema=scratch_db.OLD_SCHEMA)
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
    db.reset_viz_pool()
    try:
        db.apply_schema()
        now = datetime.now(timezone.utc)
        with psycopg.connect(f"postgresql:///{name}") as conn:
            conn.execute(
                "INSERT INTO projects (project_id, display_name, "
                "first_seen_at, last_seen_at) VALUES ('p2', 'p2', %s, %s)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO dim_model (value) VALUES ('m2') "
                "ON CONFLICT (value) DO UPDATE SET value = EXCLUDED.value"
            )
            conn.execute(
                "INSERT INTO files (file_key, project_id, session_id, "
                "is_main, r2_etag, r2_size_bytes, r2_last_modified, "
                "parsed_at, parser_version) VALUES ('k/p2/s/m.jsonl', 'p2', "
                "'s2', TRUE, 'e', 1, %s, %s, '0')", (now, now),
            )
            conn.execute(
                "INSERT INTO records (file_id, line_num, model_id) "
                "SELECT file_id, 7, id FROM files, dim_model "
                "WHERE files.file_key = 'k/p2/s/m.jsonl' "
                "AND dim_model.value = 'm2'"
            )
            conn.execute("DELETE FROM schema_stamp")
            conn.commit()

        db.apply_schema()
        db.apply_schema()

        with psycopg.connect(f"postgresql:///{name}") as conn:
            assert (conn.execute(
                "SELECT COUNT(*) FROM records WHERE line_num = 7"
            ).fetchone() or (-1,))[0] == 1
            assert (conn.execute(
                "SELECT COUNT(*) FROM files").fetchone() or (-1,))[0] == 1
            # No retired TEXT dimension column survived the re-apply.
            assert "provider" not in _columns(conn, "records")
            assert "stop_reason" not in _columns(conn, "records")
            assert "model" not in _columns(conn, "tool_uses")
            assert "error_kind" not in _columns(conn, "tool_uses")
            assert "provider" not in _columns(conn, "usage_rollup")
            # index policy now owns the analysis indexes: the
            # obsolete two and turn_flags' GIN (no integer opclass)
            # stay absent; read_targets/reread exist
            # while their collection group is on (default).
            names = _index_names(conn)
            for gone in ("records_request_idx", "tool_uses_tool_idx",
                         "records_turn_flags_idx"):
                assert gone not in names, gone
            # The indexes that moved keep existing under their names.
            for kept in ("records_model_idx", "records_latency_idx",
                         "tool_uses_read_targets_idx",
                         "tool_uses_reread_idx",
                         "tool_uses_error_kind_idx",
                         "tool_uses_agent_type_idx",
                         "usage_rollup_project_idx"):
                assert kept in names, kept
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)


# ---------------------------------------------------------------------------
# The THIRD deployment state: a dev database stamped by this branch at the
# EARLIER widths — BIGINT surrogate ids everywhere, a TEXT cli_version, and
# the narrow-last declaration order. Rebuilt below by the same guarded path
# as the legacy shape, because a stamped database must not keep the old
# column order forever.
# ---------------------------------------------------------------------------

# records / tool_uses as the branch declared them before task 7: same live
# columns, different order, BIGINT ids, cli_version as TEXT. The reprice
# columns are absent — schema_records_price.sql had already moved them to
# the sidecar — so this is the stamped REST state, not a mid-chain one.
_STAMPED_FACTS = (
    """
    CREATE TABLE records (
      file_id                  BIGINT     NOT NULL
                                          REFERENCES files(file_id)
                                          ON DELETE CASCADE,
      line_num                 INT        NOT NULL,
      uuid                     TEXT,
      request_id               TEXT       NOT NULL DEFAULT '',
      ts                       TIMESTAMPTZ,
      model_id                 BIGINT     NOT NULL
                                          REFERENCES dim_model(id),
      fresh_tokens             INT        NOT NULL DEFAULT 0,
      cache_creation_tokens    INT        NOT NULL DEFAULT 0,
      cache_read_tokens        INT        NOT NULL DEFAULT 0,
      cache_write_tokens       INT        NOT NULL DEFAULT 0,
      output_tokens            INT        NOT NULL DEFAULT 0,
      eph5_tokens              INT        NOT NULL DEFAULT 0,
      eph1h_tokens             INT        NOT NULL DEFAULT 0,
      web_search_requests      INTEGER,
      text_chars               INT        NOT NULL DEFAULT 0,
      reply_latency_s          NUMERIC(10,3),
      stop_reason_id           BIGINT     REFERENCES dim_stop_reason(id),
      effort_id                BIGINT     REFERENCES dim_effort(id),
      thinking_tokens          INT        NOT NULL DEFAULT 0,
      provider_id              BIGINT     REFERENCES dim_provider(id),
      cli_version              TEXT,
      turn_flags               INT        NOT NULL DEFAULT 0,
      turn_tool_results        INT        NOT NULL DEFAULT 0,
      is_canonical             BOOLEAN    NOT NULL DEFAULT TRUE,
      is_replay                BOOLEAN,
      PRIMARY KEY (file_id, line_num)
    ) WITH (fillfactor = 100)
    """,
    """
    CREATE TABLE tool_uses (
      file_id               BIGINT     NOT NULL REFERENCES files(file_id)
                                        ON DELETE CASCADE,
      line_num              INT        NOT NULL,
      idx                   INT        NOT NULL,
      ts                    TIMESTAMPTZ,
      tool_name_id          BIGINT     NOT NULL
                                        REFERENCES dim_tool_name(id),
      tool_use_id           TEXT,
      model_id              BIGINT     REFERENCES dim_model(id),
      is_error              BOOLEAN,
      error_kind_id         BIGINT     REFERENCES dim_error_kind(id),
      error_text            TEXT,
      lines_added           INT        NOT NULL DEFAULT 0,
      lines_deleted         INT        NOT NULL DEFAULT 0,
      agent_type_id         BIGINT     REFERENCES dim_agent_type(id),
      agent_model_id        BIGINT     REFERENCES dim_agent_model(id),
      dispatch_prompt_chars INT,
      dispatch_brief_ref    BOOLEAN,
      dispatch_name         TEXT,
      result_chars          INT,
      read_kind             TEXT,
      read_targets          TEXT[],
      write_targets         TEXT[],
      is_reread             BOOLEAN,
      is_replay             BOOLEAN,
      is_canonical          BOOLEAN    NOT NULL DEFAULT TRUE,
      PRIMARY KEY (file_id, line_num, idx)
    )
    """,
    """
    CREATE TABLE records_price (
      file_id                  BIGINT     NOT NULL,
      line_num                 INT        NOT NULL,
      cost_usd                 NUMERIC(12,6) NOT NULL DEFAULT 0,
      long_context             BOOLEAN,
      long_context_input_mult  DOUBLE PRECISION,
      long_context_output_mult DOUBLE PRECISION,
      request_fee_usd          NUMERIC(12,6),
      pricing_version_id       BIGINT     REFERENCES dim_pricing_version(id),
      rate_fingerprint_id      BIGINT     REFERENCES dim_rate_fingerprint(id),
      PRIMARY KEY (file_id, line_num),
      FOREIGN KEY (file_id, line_num) REFERENCES records (file_id, line_num)
        ON DELETE CASCADE
    ) WITH (fillfactor = 50)
    """,
)

# The in-place width narrowing, reverted: every identity, dimension and
# rollup key column back to BIGINT. Widening the PARENT while a child FK
# still references it leaves the FK in place (PostgreSQL keeps it — the
# two integer types share an equality operator), which is why this runs
# with the rollups still present, exactly as a stamped database has them.
_STAMPED_WIDEN = (
    "ALTER TABLE files ALTER COLUMN file_id TYPE BIGINT",
    "ALTER TABLE projects ALTER COLUMN proj_id TYPE BIGINT",
    "ALTER TABLE sessions ALTER COLUMN ses_id TYPE BIGINT,"
    " ALTER COLUMN proj_id TYPE BIGINT",
    "ALTER TABLE dim_model ALTER COLUMN id TYPE BIGINT",
    "ALTER TABLE dim_provider ALTER COLUMN id TYPE BIGINT",
    "ALTER TABLE dim_tool_name ALTER COLUMN id TYPE BIGINT",
    "ALTER TABLE dim_agent_type ALTER COLUMN id TYPE BIGINT",
    "ALTER TABLE dim_agent_model ALTER COLUMN id TYPE BIGINT",
    "ALTER TABLE dim_error_kind ALTER COLUMN id TYPE BIGINT",
    "ALTER TABLE dim_stop_reason ALTER COLUMN id TYPE BIGINT",
    "ALTER TABLE dim_effort ALTER COLUMN id TYPE BIGINT",
    "ALTER TABLE dim_rate_fingerprint ALTER COLUMN id TYPE BIGINT",
    "ALTER TABLE dim_pricing_version ALTER COLUMN id TYPE BIGINT",
    "ALTER TABLE usage_rollup ALTER COLUMN ses_id TYPE BIGINT,"
    " ALTER COLUMN proj_id TYPE BIGINT, ALTER COLUMN model_id TYPE BIGINT,"
    " ALTER COLUMN provider_id TYPE BIGINT",
    "ALTER TABLE tool_rollup ALTER COLUMN proj_id TYPE BIGINT,"
    " ALTER COLUMN model_id TYPE BIGINT,"
    " ALTER COLUMN tool_name_id TYPE BIGINT",
    "ALTER TABLE tool_error_rollup ALTER COLUMN proj_id TYPE BIGINT,"
    " ALTER COLUMN model_id TYPE BIGINT,"
    " ALTER COLUMN tool_name_id TYPE BIGINT,"
    " ALTER COLUMN error_kind_id TYPE BIGINT",
    "ALTER TABLE dispatch_rollup ALTER COLUMN proj_id TYPE BIGINT,"
    " ALTER COLUMN agent_type_id TYPE BIGINT,"
    " ALTER COLUMN agent_model_id TYPE BIGINT",
    "ALTER TABLE dispatch_brief_rollup ALTER COLUMN proj_id TYPE BIGINT,"
    " ALTER COLUMN agent_type_id TYPE BIGINT",
    "ALTER TABLE ctx_cost_rollup ALTER COLUMN proj_id TYPE BIGINT,"
    " ALTER COLUMN model_id TYPE BIGINT",
    "ALTER TABLE agent_rollup ALTER COLUMN proj_id TYPE BIGINT,"
    " ALTER COLUMN model_id TYPE BIGINT,"
    " ALTER COLUMN agent_type_id TYPE BIGINT",
    "ALTER TABLE latency_rollup ALTER COLUMN proj_id TYPE BIGINT,"
    " ALTER COLUMN model_id TYPE BIGINT",
)


def _stamp_earlier_shape(conn, project: str, file_key: str) -> None:
    """Rewind a canonical database to the branch's earlier stamped shape."""
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO projects (project_id, display_name, "
        "first_seen_at, last_seen_at) VALUES (%s, %s, %s, %s)",
        (project, project, now, now))
    conn.execute(
        "INSERT INTO files (file_key, project_id, session_id, is_main, "
        "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
        "parser_version) VALUES (%s, %s, 's-1', TRUE, 'e', 1, %s, %s, "
        "'108')", (file_key, project, now, now))
    conn.execute(
        "INSERT INTO dim_model (value) VALUES ('m') "
        "ON CONFLICT (value) DO UPDATE SET value = EXCLUDED.value")
    conn.commit()

    # Derived facts first: their FKs block the widenings below only in
    # the sense that the stamped state does not have them any more.
    conn.execute("DROP TABLE records_price")
    conn.execute("DROP TABLE records")
    conn.execute("DROP TABLE tool_uses")
    for stmt in _STAMPED_WIDEN:
        conn.execute(stmt)
    for ddl in _STAMPED_FACTS:
        conn.execute(ddl)
    conn.execute(
        "INSERT INTO records (file_id, line_num, model_id, ts, "
        "cli_version) SELECT file_id, 1, id, %s, '2.1.999' "
        "  FROM files, dim_model "
        " WHERE files.file_key = %s AND dim_model.value = 'm'",
        (now, file_key))
    conn.commit()


@pytest.mark.db
def test_a_database_stamped_at_the_earlier_widths_converges(monkeypatch):
    """BIGINT ids, TEXT cli_version, narrow-last order -> canonical.

    The signature guard drops the derived facts (rows rebuilt by the
    forced reparse) and narrows the identity/dimension tables in place;
    records and tool_uses come back in the widest-first order, and
    files/projects/dim ROWS survive the narrowing.
    """
    from test_storage_widths import (  # pylint: disable=import-outside-toplevel
        _assert_canonical, _order, _types,
    )
    name = scratch_db.create_database("dims_stamped")
    project, file_key = "stamped", "stamped/s-1/w.jsonl"
    with psycopg.connect(f"postgresql:///{name}") as conn:
        _stamp_earlier_shape(conn, project, file_key)
        # Prove the fixture rewound: the old order starts at file_id and
        # the surrogate is BIGINT — otherwise the test would pass by
        # never leaving the canonical state.
        assert _order(conn, "records")[0] == "file_id"
        assert _types(conn, "records")["file_id"] == "bigint"
        assert _types(conn, "records")["cli_version"] == "text"
        assert _types(conn, "tool_uses")["read_kind"] == "text"
        assert _types(conn, "files")["file_id"] == "bigint"

    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
    db.reset_viz_pool()
    try:
        db.apply_schema()
        with psycopg.connect(f"postgresql:///{name}") as conn:
            _assert_canonical(conn)
            # Identity and dimension rows migrate in place...
            assert (conn.execute(
                "SELECT COUNT(*) FROM files WHERE file_key = %s",
                (file_key,)).fetchone() or (0,))[0] == 1
            assert (conn.execute(
                "SELECT COUNT(*) FROM projects WHERE project_id = %s",
                (project,)).fetchone() or (0,))[0] == 1
            assert (conn.execute(
                "SELECT COUNT(*) FROM dim_model WHERE value = 'm'"
            ).fetchone() or (0,))[0] == 1
            # ..the derived facts rebuild empty (the PARSER_VERSION
            # bump's reparse refills them), never carrying the old row
            # or the TEXT cli_version across.
            assert (conn.execute(
                "SELECT COUNT(*) FROM records").fetchone() or (-1,))[0] == 0
            assert (conn.execute(
                "SELECT COUNT(*) FROM tool_uses").fetchone() or (-1,))[0] == 0
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)
