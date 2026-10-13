"""Keep `adopt_original_models`' records join on a hashable plan.

The records adoption joins two subsets of `records` on the split uuid
identity. Since issue #959 task 8 that join was spelled
`IS NOT DISTINCT FROM` over the three parts — a form PostgreSQL can
neither hash nor merge — so the planner's ONLY shape was a nested loop
whose inner side seq-scans the whole `records` table once per replayed
copy. At corpus scale (14,011 replay rows against 1.79M records) that is
quadratic and ran for hours; an equality join on the rebuilt identity
text (the same fragment every other uuid read uses) plans as a hash
join: equal texts iff equal parts, enforced by uuid_split's docstring
and schema_dims' CHECK disjointness.

The test seeds a 50/50 winner/replay population, EXPLAINs the actual
statement `adopt_original_models` executes (captured the way
`test_ingest_incremental_plan` captures the rollup INSERTs) and fails on
a Nested Loop; it then checks the adoption itself still moved every
replayed copy onto its winner's model.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any

import pytest

from test_ingest import _fresh_db_fixture as ingest_fresh_db_fixture  # pylint: disable=unused-import
from backend import db, ingest_rollup_state
from backend.ingest_scope import Scope
from tests import dims_seed

_PAIRS = 1500  # 3,000 records: a nested loop inner-scan runs millions of tuple checks


def _walk_plan(node: dict[str, Any]):
    yield node
    for child in node.get("Plans", []):
        yield from _walk_plan(child)


def _seq_scans_records(node: dict[str, Any]) -> bool:
    """Does this plan node (through pass-through wrappers) seq-scan records?

    Counting through Materialize/Gather matters: at test scale the
    defect plan's inner is a Materialize-wrapped Seq Scan on records.
    Memoize is deliberately NOT transparent — nothing under it is a
    records scan (it wraps the dim_model index probe)."""
    if node.get("Node Type") == "Seq Scan":
        return node.get("Relation Name") == "records"
    if node.get("Node Type") in ("Materialize", "Gather",
                                 "Gather Merge", "LockRows"):
        child = node.get("Plans", [])
        return bool(child) and _seq_scans_records(child[0])
    return False


class _PlanCaptureConnection:
    """EXPLAIN the records-adopt statement before executing it."""

    def __init__(self, conn: Any, plans: list[dict[str, Any]]) -> None:
        self.conn = conn
        self.plans = plans

    def execute(self, sql: Any, params: Any = None) -> Any:
        statement = str(sql)
        if ("adopt AS" in statement
                and re.search(r"records r JOIN winners w", statement)):
            # Plain EXPLAIN: the statement's CTEs UPDATE, and EXPLAIN
            # ANALYZE would execute them (the adoption would happen
            # here and the real run would find nothing left to adopt).
            plan = self.conn.execute(
                "EXPLAIN (FORMAT JSON) " + statement, params
            ).fetchone()[0]
            self.plans.append(plan[0])
        return self.conn.execute(sql, params)

    def commit(self) -> None:
        self.conn.commit()


def _seed() -> tuple[int, int]:
    """Winner/replay copies sharing every identity; return the model ids."""
    with db.viz_conn() as conn:
        conn.execute(
            "INSERT INTO projects VALUES ('q', 'q', now(), now()) "
            "ON CONFLICT (project_id) DO NOTHING"
        )
        conn.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, "
            "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
            "parser_version) VALUES "
            "('win', 'q', 's', TRUE, 'e', 1, now(), now(), 'v'), "
            "('rep', 'q', 's', FALSE, 'e', 1, now(), now(), 'v')"
        )
        winner_model = dims_seed.dim_id(conn, "dim_model", "winner-model")
        replay_model = dims_seed.dim_id(conn, "dim_model", "replay-model")
        conn.execute(
            "INSERT INTO records (file_id, line_num, fallback, ts, "
            "model_id, is_canonical, is_replay) "
            "SELECT f.file_id, i, 'ident-' || i, "
            "       '2026-01-01T00:00Z'::timestamptz + i * interval '1 minute', "
            "       %s, TRUE, FALSE "
            "  FROM generate_series(1, %s) i "
            "  JOIN files f ON f.file_key = 'win'",
            (winner_model, _PAIRS),
        )
        conn.execute(
            "INSERT INTO records (file_id, line_num, fallback, ts, "
            "model_id, is_canonical, is_replay) "
            "SELECT f.file_id, i, 'ident-' || i, "
            "       '2026-01-01T00:00Z'::timestamptz + i * interval '1 minute', "
            "       %s, FALSE, TRUE "
            "  FROM generate_series(1, %s) i "
            "  JOIN files f ON f.file_key = 'rep'",
            (replay_model, _PAIRS),
        )
        conn.execute("ANALYZE records")
        conn.commit()
    return winner_model, replay_model


def test_records_adopts_on_a_hashable_join_not_a_nested_loop(
        fresh_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """No Nested Loop in the actual plan; every replay adopts its winner."""
    winner_model, replay_model = _seed()
    plans: list[dict] = []
    original = db.viz_conn

    @contextmanager
    def explained():
        with original() as conn:
            yield _PlanCaptureConnection(conn, plans)

    monkeypatch.setattr(db, "viz_conn", explained)
    assert ingest_rollup_state.adopt_original_models(Scope(True, "t", 1e9)) == _PAIRS
    monkeypatch.setattr(db, "viz_conn", original)

    assert plans, "the records-adopt statement was never executed"
    nodes = list(_walk_plan(plans[0]["Plan"]))
    print("join nodes:",
          sorted({n["Node Type"] for n in nodes if "Join" in n["Node Type"]
                  or n["Node Type"] == "Nested Loop"}))
    for node in nodes:
        print(node["Node Type"], node.get("Relation Name"),
              node.get("Join Type"), "rows", node.get("Plan Rows"))

    # The defect shape: a Nested Loop whose INNER side (Plans[1])
    # seq-scans records — one full-table scan per outer row — counting
    # through the pass-through wrappers the planner may interpose
    # (Materialize, Gather): see _seq_scans_records. A Nested Loop whose
    # inner probes the tiny dim tables (memoized index scan) is fine
    # and expected; that is the winners CTE's dim_model join.
    defect = [
        n for n in nodes
        if n["Node Type"] == "Nested Loop"
        and len(n.get("Plans", [])) > 1
        and _seq_scans_records(n["Plans"][1])
    ]
    assert not defect, (
        "records adoption planned as a Nested Loop with an inner ("
        "possibly Materialize-wrapped) Seq Scan on records: one "
        "full-table scan per replayed copy — quadratic at corpus scale "
        "(measured: 14,011 outer rows against 1.79M inner, hours). The "
        "join must stay hashable (equality on the rebuilt identity text)."
    )
    # Second guard: the adopt join itself must be a hash- or
    # merge-joinable node. "Nested Loop" is not a Join node type, so on
    # the defect plan this fires with a message that names the real
    # property instead of "the plan joins nothing".
    assert any(n["Node Type"] in ("Hash Join", "Merge Join")
               for n in nodes), (
        "no Hash Join or Merge Join in the records-adopt plan — the "
        "join condition is not hash-mergeable (IS NOT DISTINCT FROM "
        "over the uuid parts has no hash/merge path), which forces the "
        "quadratic nested loop"
    )

    with db.viz_conn() as conn:
        row = conn.execute(
            "SELECT count(*) FILTER (WHERE model_id = %s), "
            "       count(*) FILTER (WHERE model_id <> %s) "
            "  FROM records WHERE is_replay",
            (winner_model, winner_model),
        ).fetchone()
        assert row is not None
        adopted, still = row
        row = conn.execute(
            "SELECT models FROM files WHERE file_key = 'rep'"
        ).fetchone()
        assert row is not None
        models = row[0]
    assert adopted == _PAIRS, f"{adopted} copies adopted, {_PAIRS} expected"
    assert still == 0, (
        f"{still} replayed copies kept a model other than the winner's "
        f"(winner {winner_model}, fork {replay_model})")
    assert "winner-model" in models
