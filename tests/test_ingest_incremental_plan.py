"""Keep small scoped hourly rebuilds on timestamp index paths."""
from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

from test_ingest import _fresh_db_fixture as ingest_fresh_db_fixture  # pylint: disable=unused-import
from test_ingest_incremental_dst import _use_timezone
from test_ingest_incremental_review import _seed_rollups
from backend import db, ingest, ingest_scope


def _walk_plan(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("Plans", []):
        yield from _walk_plan(child)


class _PlanCaptureConnection:
    """Run EXPLAIN for the actual scoped INSERT SELECT before inserting."""

    def __init__(self, conn: Any, plans: dict[str, dict[str, Any]]) -> None:
        self.conn = conn
        self.plans = plans

    def execute(self, sql: Any, params: Any = None) -> Any:
        statement = str(sql).strip()
        match = re.search(r"INSERT INTO (usage_rollup|tool_rollup)\b", statement)
        if match:
            table = match.group(1)
            query_start = statement.find("WITH scoped_source AS MATERIALIZED")
            if query_start < 0:
                query_start = statement.index("SELECT ", match.end())
            select = statement[query_start:]
            plan = self.conn.execute(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + select,
                params,
            ).fetchone()[0]
            self.plans[table] = plan[0]
        return self.conn.execute(sql, params)

    def commit(self) -> None:
        self.conn.commit()


def test_actual_scoped_insert_plans_use_timestamp_indexes(
        fresh_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single dirty hour must avoid scanning the complete source tables."""
    _use_timezone(monkeypatch, "UTC")
    _seed_rollups()
    with db.viz_conn() as conn:
        conn.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, "
            "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
            "parser_version) SELECT 'bulk-' || i, 'q', 'bulk-' || i, TRUE, "
            "'e', 1, now(), now(), 'v' FROM generate_series(1, 200) i"
        )
        conn.execute(
            "INSERT INTO records (file_key, line_num, uuid, ts, model) "
            "SELECT 'bulk-' || i, n, i || '-' || n, "
            "'2026-01-01T00:00Z'::timestamptz + n * interval '1 hour', "
            "'model' FROM generate_series(1, 200) i "
            "CROSS JOIN generate_series(1, 200) n"
        )
        conn.execute(
            "INSERT INTO tool_uses (file_key, line_num, idx, tool_name, ts, "
            "model) SELECT file_key, line_num, 0, 'Read', ts, model "
            "FROM records WHERE file_key LIKE 'bulk-%'"
        )
        for table in ("records", "tool_uses", "files"):
            conn.execute(f"ANALYZE {table}")
        conn.commit()

    scope = ingest_scope.Scope(False, "plan-test", 2000)
    scope.add_hour("q", datetime(2026, 1, 2, tzinfo=timezone.utc))
    plans: dict[str, dict] = {}
    original = db.viz_conn

    @contextmanager
    def explained():
        with original() as conn:
            yield _PlanCaptureConnection(conn, plans)

    monkeypatch.setattr(db, "viz_conn", explained)
    assert ingest.rebuild_rollup(scope) == 200
    assert ingest.rebuild_tool_rollup(scope) == 1

    failures = []
    for table, source in (("usage_rollup", "records"),
                          ("tool_rollup", "tool_uses")):
        plan = plans[table]
        nodes = list(_walk_plan(plan["Plan"]))
        print(table, f"{plan['Execution Time']:.3f}ms")
        for node in nodes:
            if "Scan" in node["Node Type"]:
                print(node["Node Type"], node.get("Relation Name"),
                      node.get("Index Name"), node.get("Index Cond"),
                      "rows", node["Actual Rows"], "loops", node["Actual Loops"])
        has_timestamp_index = any(
            "ts" in node.get("Index Cond", "") for node in nodes)
        source_seq_scan = any(
            node["Node Type"] == "Seq Scan"
            and node.get("Relation Name") == source
            for node in nodes)
        if not has_timestamp_index or source_seq_scan:
            failures.append(
                f"{table}: ts_index={has_timestamp_index}, "
                f"source_seq_scan={source_seq_scan}")
    assert not failures, "; ".join(failures)
