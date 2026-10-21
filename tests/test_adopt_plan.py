"""Keep adopt_original_models on a hashable join of the split uuid identity.
IS NOT DISTINCT FROM over the three parts cannot hash or merge, forcing a
nested loop whose inner side scans records once per replayed copy. Equality
on rebuilt identity text permits a hash join; equal texts imply equal parts
because uuid_split and schema CHECKs enforce disjoint component encodings.
Seed a balanced winner/replay population, EXPLAIN the actual captured adoption
statement and reject Nested Loop, then verify every replay adopts its winner's model."""
from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any

import pytest

from test_ingest import _fresh_db_fixture as ingest_fresh_db_fixture  # pylint: disable=unused-import
from test_adoption_rate_state import _seed_many
from backend import db, ingest_reprice, ingest_rollup_state
from backend.ingest_abort import IngestAborted
from backend.ingest_scope import Scope
from tests import dims_seed

_PAIRS = 1500  # 3,000 records: a nested loop inner-scan runs millions of tuple checks


def _walk_plan(node: dict[str, Any]):
    yield node
    for child in node.get("Plans", []):
        yield from _walk_plan(child)


def _identity_join(plan: dict[str, Any]) -> dict[str, Any]:
    joins = []
    for node in _walk_plan(plan):
        condition = " ".join(node.get(key, "") for key in
                             ("Join Filter", "Hash Cond", "Merge Cond"))
        aliases = set(re.findall(r"\b(\w+)\.(?:uuid_col|fallback)\b", condition))
        if len(aliases) == 2:
            joins.append(node)
    assert len(joins) == 1, "identify the UUID join, excluding UPDATE and sidecar joins"
    return joins[0]


def _assert_identity_join(plan: dict[str, Any]) -> None:
    node = _identity_join(plan)
    assert node["Node Type"] in ("Hash Join", "Merge Join"), (
        "UUID join cross-scans originals for every replay (including "
        f"Materialize over an index-fed scan): {node['Node Type']}")


class _PlanCaptureConnection:
    """EXPLAIN the records-adopt statement before executing it."""

    def __init__(self, conn: Any, plans: list[dict[str, Any]]) -> None:
        self.conn = conn
        self.plans = plans
        self.guard_prices = False

    def execute(self, sql: Any, params: Any = None) -> Any:
        # Reissue after every commit; bound EXPLAIN and real batch execution.
        self.conn.execute("SET LOCAL statement_timeout = '30s'")
        statement = str(sql)
        identity = re.search(r"records r JOIN winners w", statement)
        price = self.guard_prices and "FROM records r JOIN records_price rp" in statement
        if price or ("adopt AS" in statement and (identity or "FROM model_adoption_candidates" in statement)):
            # Plain EXPLAIN: the statement's CTEs UPDATE, and EXPLAIN
            # ANALYZE would execute them (the adoption would happen
            # here and the real run would find nothing left to adopt).
            plan = self.conn.execute(
                "EXPLAIN (FORMAT JSON) " + statement, params
            ).fetchone()[0]
            self.plans.append(plan[0])
            # Reject the pathological plan before executing the real UPDATE.
            if identity:
                _assert_identity_join(plan[0]["Plan"])
            assert not price, "reuse the UPDATE's returned rate state instead of rereading records"
        return self.conn.execute(sql, params)

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()


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

    _assert_identity_join(plans[0]["Plan"])

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


def test_identity_join_above_default_batch_and_in_later_batch(fresh_db, monkeypatch):
    # 80,002 replays at the production limit: this reaches the ordered LIMIT
    # plan cliff that the small no-sidecar fixture cannot expose.
    _seed_many(2 * ingest_reprice.REPRICE_BATCH + 1)
    with db.viz_conn() as conn:
        conn.execute("ANALYZE records")
        conn.execute("ANALYZE records_price")
        conn.commit()
    plans = []
    original = db.viz_conn

    @contextmanager
    def explained():
        with original() as conn:
            capture = _PlanCaptureConnection(conn, plans)
            capture.guard_prices = True
            yield capture

    monkeypatch.setattr(db, "viz_conn", explained)

    def batch_plans():
        return [plan for plan in plans if any(
            n.get("Relation Name") == "model_adoption_candidates"
            for n in _walk_plan(plan["Plan"]))]

    with pytest.raises(IngestAborted):
        ingest_rollup_state.adopt_original_models(should_stop=lambda: len(batch_plans()) >= 2)
    assert len(plans) == 3, "one identity join and two batches; no duplicate rate reads"
    _assert_identity_join(plans[0]["Plan"])
    for plan in batch_plans():
        nodes = list(_walk_plan(plan["Plan"]))
        assert any(node.get("Relation Name") == "model_adoption_candidates"
                   and node["Node Type"] == "Index Scan" for node in nodes)
        assert not any("uuid_col" in node.get("Join Filter", "") for node in nodes), (
            "later batches read staged keys instead of repeating the identity join")
    with original() as conn:
        row = conn.execute(
            "SELECT count(*) FROM records r JOIN dim_model dm ON dm.id=r.model_id "
            "WHERE r.is_replay AND dm.value='gpt-5.6-sol'").fetchone()
        assert row is not None
        assert row[0] == 2 * ingest_reprice.REPRICE_BATCH
