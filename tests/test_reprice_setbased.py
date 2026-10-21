"""The reprice pass's restamp/reprice split and bounded writes.

Unchanged rows restamp silently; mixed batches preserve committed work
on shutdown, and instrumentation accounts for the pass's phases.
"""
from __future__ import annotations

import contextlib
import logging
import re

import pytest

# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture,
    _mini_r2_env_fixture,
)

from test_reprice import _FILE_KEY, _seed, _seeded_cost, _rows
from tests.reprice_fixture_builders import SeedRecord

from backend import (
    constants,
    db,
    ingest,
    ingest_reprice,
    ingest_rate_state,
    timing,
)


def test_reprice_restamp_only_run_reports_zero(fresh_db, caplog):
    """Every stale row's stored state already equals the computed state
    (a PRICING_VERSION move with no rate change for any stored pair):
    the pass restamps in set-based batches and reports ZERO — the ingest
    must not promote a full rebuild, invalidate the cache or broadcast
    for a run that changed no user-visible data."""
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1,
              SeedRecord(pricing_version="0", cost_usd=_seeded_cost()))
        _seed(c, _FILE_KEY, 2,
              SeedRecord(pricing_version="0", cost_usd=_seeded_cost()))
        c.commit()

    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        assert ingest_reprice.reprice_stale() == 0

    with db.viz_conn() as c:
        rows = _rows(c)
    assert [float(r[1]) for r in rows] == [_seeded_cost()] * 2
    assert [r[2] for r in rows] == [constants.PRICING_VERSION] * 2, (
        "every restamped row carries the current version")
    assert "2 restamped" in caplog.text, (
        "the pass logs the restamp count beside the changed count")


def test_reprice_endpoint_matches_the_assembly_point(fresh_db):
    """The endpoint invariant the differential rests on: after the pass,
    every non-guarded row equals what `record_updates` computes from its
    own stored columns at the current version, and a guard-skipped row
    (a newer stored version) keeps its stored state. This is exactly the
    old one-row-at-a-time pass's endpoint, so the split changes nothing —
    the result is byte-identical to the old pass's."""
    newer = str(int(constants.PRICING_VERSION) + 1)
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1,
              SeedRecord(pricing_version="0", cost_usd=_seeded_cost()))
        _seed(c, _FILE_KEY, 2, SeedRecord(pricing_version="0"))
        _seed(c, _FILE_KEY, 3, SeedRecord(pricing_version=newer))
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        raw = c.execute(
            "SELECT f.file_key, r.line_num, dm.value, r.fresh_tokens, "
            "r.cache_creation_tokens, r.cache_read_tokens, "
            "r.output_tokens, r.eph5_tokens, r.eph1h_tokens, r.ts, "
            "rp.long_context, dp.value, rp.cost_usd, pv.value, df.value, "
            "rp.request_fee_usd, r.web_search_requests "
            "FROM records r JOIN records_price rp ON rp.file_id = r.file_id AND rp.line_num = r.line_num JOIN files f ON f.file_id = r.file_id "
            "JOIN dim_model dm ON dm.id = r.model_id "
            "LEFT JOIN dim_provider dp ON dp.id = r.provider_id "
            "LEFT JOIN dim_pricing_version pv ON pv.id = rp.pricing_version_id "
            "LEFT JOIN dim_rate_fingerprint df "
            "  ON df.id = rp.rate_fingerprint_id "
            "WHERE f.file_key = %s ORDER BY r.line_num",
            (_FILE_KEY,)).fetchall()
    assert len(raw) == 3
    for tup in raw:
        row = ingest_rate_state.RateRow(*tup)
        expected = ingest_rate_state.record_updates(row)
        if row.pricing_version == newer:
            assert float(row.cost_usd) == 0.5, "the guard row keeps its sentinel"
            continue
        assert float(row.cost_usd) == expected["cost_usd"]
        assert row.long_context == expected["long_context"]
        assert row.pricing_version == constants.PRICING_VERSION


def test_reprice_split_survives_the_keyset_and_shutdown(fresh_db,
                                                        monkeypatch):
    """Batching and the shutdown contract survive the split: with
    REPRICE_BATCH=2 the keyset advances past restamped and repriced rows
    alike, and a mid-pass abort keeps committed batches (both kinds)."""
    monkeypatch.setattr(ingest_reprice, "REPRICE_BATCH", 2)
    with db.viz_conn() as c:
        for line_num in range(1, 8):
            correct = line_num in (1, 4)
            _seed(c, _FILE_KEY, line_num, SeedRecord(
                pricing_version="0",
                cost_usd=_seeded_cost() if correct else 0.5))
        c.commit()

    checks = iter([False, False, False, True])
    with pytest.raises(ingest.IngestAborted):
        ingest_reprice.reprice_stale(should_stop=lambda: next(checks))

    with db.viz_conn() as c:
        rows = _rows(c)
    versions = [r[2] for r in rows]
    assert versions[:6] == [constants.PRICING_VERSION] * 6, (
        "the first three batches were committed before the abort, so they "
        "persist (restamped and repriced rows alike)")
    assert versions[6] == "0", (
        "rows the aborted pass never reached stay stale")


def test_reprice_writes_set_based_not_per_row(fresh_db, monkeypatch):
    """Batch writes must use at most two set-based UPDATEs: restamp and reprice.
    Semantic tests alone cannot catch a return to per-row writes, so bound the
    statement count by batches rather than rows. One pair-qualified clean
    restamp before the loop raises the whole-pass bound to three UPDATEs."""
    calls: list[str] = []
    real_viz_conn = db.viz_conn

    class _SpyCursor:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, sql, params=None):
            calls.append(sql.split("(")[0].split()[0].lower())
            return self._cur.execute(sql, params)

        def executemany(self, sql, params_seq):
            calls.append("executemany")
            return self._cur.executemany(sql, params_seq)

        def __getattr__(self, name):
            return getattr(self._cur, name)

    class _SpyConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=None):
            calls.append(sql.split("(")[0].split()[0].lower())
            return self._conn.execute(sql, params)

        def executemany(self, sql, params_seq):
            calls.append("executemany")
            return self._conn.executemany(sql, params_seq)

        def cursor(self):
            return _SpyCursor(self._conn.cursor())

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextlib.contextmanager
    def spy_viz_conn():
        with real_viz_conn() as conn:
            yield _SpyConn(conn)

    monkeypatch.setattr(ingest_reprice.db, "viz_conn", spy_viz_conn)

    with db.viz_conn() as c:
        # five stale rows: two already correct (restamp), three moved
        for line_num in range(1, 6):
            correct = line_num in (1, 2)
            _seed(c, _FILE_KEY, line_num, SeedRecord(
                pricing_version="0",
                cost_usd=_seeded_cost() if correct else 0.5))
        c.commit()

    assert ingest_reprice.reprice_stale() == 3

    assert "executemany" not in calls, (
        "the per-row executemany stream must not return")
    update_calls = calls.count("update")
    assert update_calls <= 3, (
        f"{update_calls} UPDATE statements for the whole pass; the pass "
        "must write set-based (at most one pair-qualified clean restamp "
        "+ one restamp + one reprice per batch)")


def test_reprice_pass_emits_phase_timing_when_on(fresh_db, monkeypatch,
                                                 caplog):
    """CLAUDIT_TIMING gives the pass a phase split: one
    account=True TIMING line per run whose select/recompute/restamp/
    moved/commit marks must sum to the measured total — the gap is the
    unattributed residue, so the instrument accounts for the whole.
    The pair-qualified clean restamp has its own marks:
    pairs (the DISTINCT pair scan and the fingerprinting) and clean
    (its set-based restamp)."""
    monkeypatch.setattr(timing, "TIMING_ON", True)
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1,
              SeedRecord(pricing_version="0", cost_usd=_seeded_cost()))
        _seed(c, _FILE_KEY, 2, SeedRecord(pricing_version="0"))
        c.commit()

    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        assert ingest_reprice.reprice_stale() == 1

    lines = [record.getMessage() for record in caplog.records
             if record.getMessage().startswith("TIMING reprice")]
    assert len(lines) == 1, "exactly one TIMING line per pass"
    line = lines[0]
    for phase in ("select", "fetch", "recompute", "restamp", "moved",
                  "commit", "pairs", "clean"):
        assert re.search(rf"\b{phase}=\d+ms", line), f"missing {phase}"
    total_m = re.search(r"\btotal=(\d+)ms", line)
    sum_m = re.search(r"\bsum=(\d+)ms", line)
    assert total_m and sum_m
    total = float(total_m.group(1))
    summed = float(sum_m.group(1))
    assert "gap=" in line and "cpu=" in line
    assert summed <= total, "phases cannot sum to more than the pass"


def test_reprice_pass_emits_no_timing_when_off(fresh_db, caplog):
    """With CLAUDIT_TIMING unset the pass is instrumentation-free: no
    TIMING line reaches the log."""
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1, SeedRecord(pricing_version="0"))
        c.commit()

    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        assert ingest_reprice.reprice_stale() == 1

    assert not [record.getMessage() for record in caplog.records
                if record.getMessage().startswith("TIMING reprice")]
