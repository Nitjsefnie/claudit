"""The reprice pass's restamp/reprice split (issue #339).

The pass recomputes every stale row's rate-derived state from stored
columns (unchanged assembly point `_record_updates`) and writes only
rows whose stored cost or long-context flag differ — the rest are
re-stamped with the current PRICING_VERSION in one set-based UPDATE per
batch. The return count is rows whose rate-derived data CHANGED, so the
ingest's promote_full and cache gate fire only when user-visible data
moved (ingest.py: a reprice phase returning 0 promotes nothing and
invalidates nothing).
"""
from __future__ import annotations

import logging

import pytest

# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture,
    _mini_r2_env_fixture,
)

from test_reprice import _FILE_KEY, _seed, _seeded_cost, _rows

from backend import constants, db, ingest, ingest_reprice


def test_reprice_reports_only_rows_whose_data_changed(fresh_db):
    """A stale row whose stored cost already equals the computed one is
    restamped, not repriced: the pass returns only the row whose data
    actually moved, and the restamped row carries the same cost with the
    current version."""
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1, pricing_version="0",
              cost_usd=_seeded_cost())
        _seed(c, _FILE_KEY, 2, pricing_version="0")
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        rows = _rows(c)
    assert float(rows[0][1]) == _seeded_cost(), (
        "the restamped row keeps its cost exactly")
    assert rows[0][2] == constants.PRICING_VERSION, (
        "the restamped row is re-stamped with the current version")
    assert float(rows[1][1]) == _seeded_cost(), "the stale row is repriced"
    assert rows[1][2] == constants.PRICING_VERSION


def test_reprice_restamp_only_run_reports_zero(fresh_db, caplog):
    """Every stale row's stored state already equals the computed state
    (a PRICING_VERSION move with no rate change for any stored pair):
    the pass restamps in set-based batches and reports ZERO — the ingest
    must not promote a full rebuild, invalidate the cache or broadcast
    for a run that changed no user-visible data."""
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1, pricing_version="0",
              cost_usd=_seeded_cost())
        _seed(c, _FILE_KEY, 2, pricing_version="0",
              cost_usd=_seeded_cost())
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
    every non-guarded row equals what `_record_updates` computes from its
    own stored columns at the current version, and a guard-skipped row
    (a newer stored version) keeps its stored state. This is exactly the
    old one-row-at-a-time pass's endpoint, so the split changes nothing —
    the result is byte-identical to the old pass's."""
    newer = str(int(constants.PRICING_VERSION) + 1)
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1, pricing_version="0",
              cost_usd=_seeded_cost())
        _seed(c, _FILE_KEY, 2, pricing_version="0")
        _seed(c, _FILE_KEY, 3, pricing_version=newer)
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        raw = c.execute(
            "SELECT file_key, line_num, model, fresh_tokens, "
            "cache_creation_tokens, cache_read_tokens, output_tokens, "
            "eph5_tokens, eph1h_tokens, ts, long_context, provider, "
            "cost_usd, pricing_version FROM records "
            "WHERE file_key = %s ORDER BY line_num", (_FILE_KEY,)).fetchall()
    assert len(raw) == 3
    for tup in raw:
        row = ingest_reprice._StaleRow(*tup)  # pylint: disable=protected-access
        expected = ingest_reprice._record_updates(row)  # pylint: disable=protected-access
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
            _seed(c, _FILE_KEY, line_num, pricing_version="0",
                  cost_usd=_seeded_cost() if correct else 0.5)
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
    """The mechanism half of issue #339: each batch's rows travel in at
    most two set-based UPDATE statements (restamp + reprice), and no
    per-row executemany stream returns. A regression to one statement
    per row — the shape this issue removed — would keep every semantic
    test green, so the write shape itself is pinned here: statement
    count stays O(batches), never O(rows)."""
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

    import contextlib

    @contextlib.contextmanager
    def spy_viz_conn():
        with real_viz_conn() as conn:
            yield _SpyConn(conn)

    monkeypatch.setattr(ingest_reprice.db, "viz_conn", spy_viz_conn)

    with db.viz_conn() as c:
        # five stale rows: two already correct (restamp), three moved
        for line_num in range(1, 6):
            correct = line_num in (1, 2)
            _seed(c, _FILE_KEY, line_num, pricing_version="0",
                  cost_usd=_seeded_cost() if correct else 0.5)
        c.commit()

    assert ingest_reprice.reprice_stale() == 3

    assert "executemany" not in calls, (
        "the per-row executemany stream must not return")
    update_calls = calls.count("update")
    assert update_calls <= 2, (
        f"{update_calls} UPDATE statements for one batch; the pass must "
        "write set-based (at most one restamp + one reprice)")
