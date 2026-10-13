"""Startup policy converges real index catalogs under the schema stamp."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
import pytest

from backend import db, flags, schema_indexes as compiler
from tests import scratch_db

ANALYSIS = {
    "tool_uses_read_targets_idx": ("gin", None),
    "tool_uses_reread_idx": ("btree", "is_reread"),
}
RETAINED = {
    "records_pkey", "records_ts_idx",
    "records_canonical_ts_idx", "records_latency_idx", "tool_uses_pkey",
    "tool_uses_ts_idx", "tool_uses_tool_use_id_idx",
}
OBSOLETE = {"records_request_idx", "tool_uses_tool_idx"}


@contextmanager
def _owned_schema(monkeypatch, label: str, schema=scratch_db.SCHEMA) -> Iterator[list[str]]:
    name = scratch_db.create_database(label, schema=schema)
    statements: list[str] = []
    original_viz_conn = db.viz_conn

    class ObservedCursor(psycopg.Cursor):
        def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
            statements.append(str(query))
            return super().execute(query, params, **kwargs)

    @contextmanager
    def observed_connection() -> Iterator[psycopg.Connection]:
        with original_viz_conn() as conn:
            original_factory = conn.cursor_factory
            conn.cursor_factory = ObservedCursor
            try:
                yield conn
            finally:
                conn.cursor_factory = original_factory

    db.reset_viz_pool()
    try:
        with monkeypatch.context() as patch:
            patch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
            patch.setattr(db, "viz_conn", observed_connection)
            patch.setattr(flags, "CONFIG", flags.resolve_config({}))
            yield statements
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)


def _catalog() -> dict[str, tuple]:
    with db.viz_conn() as conn:
        return {name: (valid, method, predicate, definition)
                for name, valid, method, predicate, definition in conn.execute(
                    "SELECT idx.relname, i.indisvalid, am.amname, "
                    "pg_get_expr(i.indpred, i.indrelid), pg_get_indexdef(i.indexrelid) "
                    "FROM pg_index i JOIN pg_class idx ON idx.oid = i.indexrelid "
                    "JOIN pg_namespace ns ON ns.oid = idx.relnamespace "
                    "JOIN pg_am am ON am.oid = idx.relam WHERE ns.nspname = 'public'"
                ).fetchall()}


def _assert_catalog(present: set[str], absent: set[str]) -> None:
    catalog = _catalog()
    for name in RETAINED | present:
        assert name in catalog and catalog[name][0], (name, catalog)
    assert not absent.intersection(catalog), catalog
    for name in present.intersection(ANALYSIS):
        method, predicate = ANALYSIS[name]
        assert catalog[name][1:3] == (method, predicate)
        column = {"tool_uses_read_targets_idx": "read_targets",
                  "tool_uses_reread_idx": "ts"}[name]
        table = "records" if name.startswith("records_") else "tool_uses"
        assert f"ON public.{table} USING {method} ({column})" in catalog[name][3]


def _seed_rows() -> None:
    with db.viz_conn() as conn:
        conn.execute("INSERT INTO projects (project_id, display_name, "
                     "first_seen_at, last_seen_at) "
                     "VALUES ('p', 'Project', now(), now())")
        conn.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, "
            "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, parser_version) "
            "VALUES ('f', 'p', 's', TRUE, 'e', 10, now(), now(), 'legacy-writer')")
        conn.execute(
            "INSERT INTO dim_model (value) VALUES ('synthetic') "
            "ON CONFLICT (value) DO NOTHING")
        conn.execute(
            "INSERT INTO dim_tool_name (value) VALUES ('Read') "
            "ON CONFLICT (value) DO NOTHING")
        conn.execute(
            "INSERT INTO records (file_id, line_num, ts, model_id, turn_flags) "
            "SELECT file_id, 1, now(), "
            "(SELECT id FROM dim_model WHERE value = 'synthetic'), 1 "
            "FROM files WHERE file_key = 'f'")
        conn.execute(
            "INSERT INTO tool_uses (file_id, line_num, idx, ts, tool_name_id, "
            "read_targets, is_reread) "
            "SELECT file_id, 1, 0, now(), "
            "(SELECT id FROM dim_tool_name WHERE value = 'Read'), "
            "ARRAY['sample.py'], TRUE FROM files WHERE file_key = 'f'")


def _counts() -> tuple[int, ...]:
    with db.viz_conn() as conn:
        row = conn.execute("SELECT (SELECT count(*) FROM files), "
                           "(SELECT count(*) FROM records), "
                           "(SELECT count(*) FROM tool_uses)").fetchone()
    assert row is not None
    return row


def _stamp() -> str:
    with db.viz_conn() as conn:
        row = conn.execute("SELECT version FROM schema_stamp").fetchone()
    assert row is not None
    return row[0]


def _submitted_ddl(statements: list[str]) -> str:
    scripts = [stmt for stmt in statements if stmt.startswith("-- claudit schema")]
    assert len(scripts) == 1
    return scripts[0]


def _assert_no_create(ddl: str, names: set[str]) -> None:
    for name in names:
        assert not re.search(rf"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+{name}\b", ddl)
        assert f"DROP INDEX IF EXISTS public.{name};" in ddl


@pytest.mark.db
def test_unused_indexes_dropped_on_startup(monkeypatch):
    with _owned_schema(monkeypatch, "unused_indexes") as statements:
        _seed_rows()
        with db.viz_conn() as conn:
            conn.execute("CREATE INDEX IF NOT EXISTS records_request_idx "
                         "ON records (request_id) WHERE request_id <> ''")
            conn.execute("CREATE INDEX IF NOT EXISTS tool_uses_tool_idx "
                         "ON tool_uses (tool_name_id)")
        _assert_catalog(set(ANALYSIS) | OBSOLETE, set())
        statements.clear()
        db.apply_schema()
        _assert_catalog(set(ANALYSIS), OBSOLETE)
        assert _counts() == (1, 1, 1)
        _assert_no_create(_submitted_ddl(statements), OBSOLETE)


@pytest.mark.db
@pytest.mark.parametrize("key, names", [
    # turn_context carries no index on this branch: records.turn_flags
    # is an INT4 mask (#959 R5) and GIN has no integer operator class.
    ("context_intake", {"tool_uses_read_targets_idx", "tool_uses_reread_idx"}),
])
def test_analysis_indexes_follow_collection_both_directions(monkeypatch, key, names):
    with _owned_schema(monkeypatch, "index_transitions") as statements:
        _seed_rows()
        db.apply_schema()
        _assert_catalog(set(ANALYSIS), set())
        on_stamp = _stamp()
        env = next(spec.env for spec in flags.ALL_COLLECTIONS_ON.specs if spec.key == key)
        monkeypatch.setattr(flags, "CONFIG", flags.resolve_config({env: "0"}))
        statements.clear()
        db.apply_schema()
        _assert_catalog(set(ANALYSIS) - names, names | OBSOLETE)
        assert _stamp() != on_stamp
        assert _counts() == (1, 1, 1)
        _assert_no_create(_submitted_ddl(statements), names | OBSOLETE)
        monkeypatch.setattr(flags, "CONFIG", flags.resolve_config({}))
        db.apply_schema()
        _assert_catalog(set(ANALYSIS), OBSOLETE)
        assert _stamp() == on_stamp
        assert _counts() == (1, 1, 1)


@pytest.mark.db
def test_fresh_off_schema_never_creates_analysis_indexes(monkeypatch):
    with _owned_schema(monkeypatch, "fresh_off", schema=None) as statements:
        monkeypatch.setattr(flags, "CONFIG", flags.resolve_config({
            "CLAUDIT_COLLECT_TURN_CONTEXT": "0", "CLAUDIT_COLLECT_CONTEXT_INTAKE": "0"}))
        db.apply_schema()
        _assert_catalog(set(), set(ANALYSIS) | OBSOLETE)
        _assert_no_create(_submitted_ddl(statements), set(ANALYSIS) | OBSOLETE)
        _seed_rows()
        assert _counts() == (1, 1, 1)
    with _owned_schema(monkeypatch, "fresh_on", schema=None) as statements:
        db.apply_schema()
        _assert_catalog(set(ANALYSIS), OBSOLETE)
        _seed_rows()
        assert _counts() == (1, 1, 1)
        _assert_no_create(_submitted_ddl(statements), OBSOLETE)


@pytest.mark.db
def test_effective_stamp_changes_only_for_index_policy(monkeypatch):
    static = db.read_schema()
    on = compiler.compile_schema(static, flags.ALL_COLLECTIONS_ON)
    assert on == static + "\n" + compiler.analysis_index_sql(flags.ALL_COLLECTIONS_ON)

    def digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

        # turn_context carries no index on this branch (#959 R5 made
        # turn_flags an INT4 mask), so only context_intake moves it.
    for env in ("CLAUDIT_COLLECT_CONTEXT_INTAKE",):
        off = compiler.compile_schema(static, flags.resolve_config({env: "0"}).collection)
        assert digest(off) != digest(on)
    for spec in flags.ALL_COLLECTIONS_ON.specs:
        if spec.key not in {"context_intake"}:
            unrelated = flags.resolve_config({spec.env: "0"})
            assert compiler.compile_schema(static, unrelated.collection) == on
    for env in (spec.env for spec in flags.FEATURE_SPECS):
        monkeypatch.setattr(flags, "CONFIG", flags.resolve_config({env: "0"}))
        assert compiler.startup_schema(static) == on
    assert digest(compiler.compile_schema(static + "\nSELECT 7;", flags.ALL_COLLECTIONS_ON)) != digest(on)
    with _owned_schema(monkeypatch, "stamp_policy") as statements:
        db.apply_schema()
        assert _submitted_ddl(statements) == on
        assert _stamp() == digest(on)
        statements.clear()
        db.apply_schema()
        assert statements and not any("pg_advisory" in stmt or "ALTER" in stmt
                                      or stmt.startswith("-- claudit schema") for stmt in statements)
        _assert_catalog(set(ANALYSIS), OBSOLETE)


@pytest.mark.db
def test_index_policy_failure_rolls_back_ddl_and_stamp(monkeypatch):
    with _owned_schema(monkeypatch, "policy_failure") as statements:
        _seed_rows()
        db.apply_schema()
        _assert_catalog(set(ANALYSIS), set())
        on_stamp = _stamp()
        monkeypatch.setattr(flags, "CONFIG", flags.resolve_config({
            "CLAUDIT_COLLECT_TURN_CONTEXT": "0", "CLAUDIT_COLLECT_CONTEXT_INTAKE": "0"}))
        original = db.read_schema()
        # A runtime error after a DROP exercises rollback of executed DDL;
        # a syntax error would reject the whole script before any DROP ran.
        with monkeypatch.context() as patch:
            patch.setattr(db, "read_schema", lambda: original +
                          "\nDROP INDEX IF EXISTS public.tool_uses_read_targets_idx;\nSELECT 1/0;")
            with pytest.raises(psycopg.errors.DivisionByZero):
                db.apply_schema()
        _assert_catalog(set(ANALYSIS), set())
        assert _stamp() == on_stamp
        assert _counts() == (1, 1, 1)
        with psycopg.connect(os.environ["CLAUDIT_DATABASE_URL_VIZ"]) as conn:
            row = conn.execute("SELECT pg_try_advisory_lock(%s)",
                               (db._SCHEMA_LOCK_KEY,)).fetchone()  # pylint: disable=protected-access
            assert row == (True,)
            conn.execute("SELECT pg_advisory_unlock(%s)",
                         (db._SCHEMA_LOCK_KEY,))  # pylint: disable=protected-access
        statements.clear()
        db.apply_schema()
        _assert_catalog(set(), set(ANALYSIS) | OBSOLETE)
        assert _stamp() != on_stamp
        assert _counts() == (1, 1, 1)
        _assert_no_create(_submitted_ddl(statements), set(ANALYSIS) | OBSOLETE)


@pytest.mark.db
def test_fixture_schema_uses_all_on_even_when_startup_collection_is_off(monkeypatch):
    off = flags.resolve_config({
        "CLAUDIT_COLLECT_TURN_CONTEXT": "0", "CLAUDIT_COLLECT_CONTEXT_INTAKE": "0"})
    monkeypatch.setattr(flags, "CONFIG", off)
    with _owned_schema(monkeypatch, "fixture_all_on") as statements:
        # Fixture DDL runs before _owned_schema replaces startup CONFIG.
        _assert_catalog(set(ANALYSIS), OBSOLETE)
        monkeypatch.setattr(flags, "CONFIG", off)
        statements.clear()
        db.apply_schema()
        _assert_catalog(set(), set(ANALYSIS) | OBSOLETE)
        _assert_no_create(_submitted_ddl(statements), set(ANALYSIS) | OBSOLETE)


@pytest.mark.db
def test_startup_policy_uses_post_dotenv_config(tmp_path):
    code = """
import json
import sys
from backend import db
assert 'backend.flags' not in sys.modules
db.load_dotenv(sys.argv[1])
from backend import flags
statements = []
original = db.sql_text
def observe(text):
    statements.append(text)
    return original(text)
db.sql_text = observe
try:
    db.apply_schema()
    print(json.dumps(statements))
finally:
    db.reset_viz_pool()
"""
    for state in ("0", "1"):
        name = scratch_db.create_database("dotenv_" + state, schema=None)
        try:
            dotenv = tmp_path / (state + ".env")
            dotenv.write_text(
                f"CLAUDIT_DATABASE_URL_VIZ=postgresql:///{name}\n"
                f"CLAUDIT_COLLECT_TURN_CONTEXT={state}\n"
                f"CLAUDIT_COLLECT_CONTEXT_INTAKE={state}\n", encoding="utf-8")
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("CLAUDIT_") and key not in {"DATABASE_URL_VIZ"}}
            proc = subprocess.run([sys.executable, "-c", code, str(dotenv)],
                                  cwd=Path(__file__).resolve().parents[1], env=env,
                                  capture_output=True, text=True, check=False, timeout=30)
            assert proc.returncode == 0, proc.stdout + proc.stderr
            submitted = json.loads(proc.stdout)
            ddl = _submitted_ddl(submitted)
            with psycopg.connect(dbname=name) as conn:
                indexes = {row[0] for row in conn.execute(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'").fetchall()}
            assert RETAINED <= indexes
            if state == "0":
                _assert_no_create(ddl, set(ANALYSIS))
                assert not set(ANALYSIS).intersection(indexes)
            else:
                assert set(ANALYSIS) <= indexes
                for index in ANALYSIS:
                    assert f"CREATE INDEX IF NOT EXISTS {index}" in ddl
        finally:
            scratch_db.drop_database(name)
