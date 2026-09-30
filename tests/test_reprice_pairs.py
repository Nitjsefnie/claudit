"""The pair-qualified reprice staleness (issue #351): the pass's own
tests.

The reprice pass classifies the stale (model, provider) pairs and
SQL-restamps every stale row whose stored rate_fingerprint still
equals its pair's current fingerprint — the fingerprint covers every
rate input resolve() consults plus the pricing modules' source, so
those rows' recomputation is the identity and the keyset loop never
sees them. The tests here bind that structural guarantee (a clean
bump reads ZERO rows into Python), the guard's reach into the SQL
restamp, the odd version spellings the SQL set excludes, and the
persist/migration shapes. The differential against the frozen
pre-#351 pass lives in tests/test_reprice_differential.py; the
assembly-point and write-shape pins stay in test_reprice_setbased.py.
Split from test_reprice.py, which would otherwise cross the
SV-CI-RATCHETS test ceiling.
"""
from __future__ import annotations

import contextlib
import logging
import re
import time
from datetime import datetime, timezone

# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture,
    _mini_r2_env_fixture,
)

from test_reprice import (
    _FILE_KEY,
    _SEED_INPUTS,
    _SEED_MODEL,
    _SEED_TS,
    _pair,
    _seed,
    _seed_parents,
    _seeded_cost,
)

from backend import (
    constants,
    db,
    ingest,
    ingest_reprice,
    pricing,
    rate_fingerprint,
    timing,
)

UTC = timezone.utc

# Rates deliberately unlike any real price, so an assertion against
# them can never be mistaken for a pricing fact (the conftest
# synthetic-rate fixtures' rule).
_MOVED_RATES = {"fresh": 8.5, "create_5m": 10.625, "create_1h": 17.0,
                "read": 0.85, "output": 42.5}

# The second pair of the moved-pair test, bound like _SEED_MODEL: the
# pinned-version guard flags a string constant naming a live rate row
# inside a pricing call, but a module constant passed as a variable is
# the sanctioned shape (SV-TEST-DATA).
_B_PAIR_MODEL = "claude-sonnet-4-5"


def _seed_block(c, model: str, first_line: int, count: int, *,
                fp: str | None,
                cost: float, provider: str | None = None,
                pricing_version: str | None = "0",
                ts=_SEED_TS) -> None:
    """`count` stale rows of one (model, provider) pair under the seeded
    file, all with the fixed tally, one cost and one fingerprint — the
    block shapes the pair-qualified tests need (issue #351)."""
    _seed_parents(c, _FILE_KEY)
    c.execute(
        "INSERT INTO records (file_key, line_num, ts, model, fresh_tokens, "
        "cache_creation_tokens, cache_read_tokens, output_tokens, "
        "eph5_tokens, eph1h_tokens, cost_usd, provider, pricing_version, "
        "rate_fingerprint) "
        "SELECT %s, i, %s, %s, 1000, 2000, 3000, 100, 250, 500, %s, %s, %s, %s "
        "FROM generate_series(%s::int, %s::int) AS i",
        (_FILE_KEY, ts, model, cost, provider, pricing_version, fp,
         first_line, first_line + count - 1),
    )


def _timing_line(caplog) -> str:
    """The pass's most recent TIMING line from the captured log."""
    lines = [record.getMessage() for record in caplog.records
             if record.getMessage().startswith("TIMING reprice")]
    assert lines, "the pass must emit its TIMING line"
    return lines[-1]


def _sql_recorder(monkeypatch) -> list[str]:
    """Route the pass's connections through a wrapper that appends every
    executed statement's SQL text to the returned list."""
    sqls: list[str] = []
    real_viz_conn = db.viz_conn

    @contextlib.contextmanager
    def spy_viz_conn():
        with real_viz_conn() as conn:
            class _SpyConn:
                def execute(self, sql, params=None):
                    sqls.append(str(sql))
                    return conn.execute(sql, params)

                def __getattr__(self, name):
                    return getattr(conn, name)

            yield _SpyConn()

    monkeypatch.setattr(ingest_reprice.db, "viz_conn", spy_viz_conn)
    return sqls


def test_ingest_stamps_rate_fingerprint(fresh_db, mini_r2_env):
    """Persist stamps each record's rate fingerprint beside its
    pricing_version — the fingerprint of the pair its stored cost was
    computed under, so a later reprice can recognise rows whose pair's
    rate data has not moved."""
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None

    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT model, provider, rate_fingerprint, pricing_version "
            "FROM records").fetchall()
    assert rows, "the mirror must produce records, or this proves nothing"
    for model, provider, fingerprint, version in rows:
        assert fingerprint == rate_fingerprint.pair_fingerprint(model,
                                                                provider), (
            f"{model!r} via {provider!r} must carry its pair's fingerprint")
        assert version == constants.PRICING_VERSION


def test_rate_fingerprint_column_is_nullable_migration(fresh_db, mini_r2_env):
    """The rate_fingerprint migration mirrors pricing_version's
    (SV-SCHEMA-AUTOAPPLY): additive and nullable, existing rows keep
    their place reading NULL — the conservative stale shape the reprice
    pass recomputes once before stamping."""
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None

    with db.viz_conn() as c:
        c.execute(
            "ALTER TABLE records DROP COLUMN IF EXISTS rate_fingerprint")
        c.commit()

    with db.viz_conn() as c:
        before = c.execute("SELECT COUNT(*) FROM records").fetchone()
    assert before is not None and before[0] > 0, (
        "the mirror must produce records, or this proves nothing")

    db.apply_schema()

    with db.viz_conn() as c:
        row = c.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'records' "
            "AND column_name = 'rate_fingerprint'").fetchone()
        assert row is not None and row[0] == "YES", (
            "schema.sql must add rate_fingerprint and admit NULL")
        after, nulls = _pair(
            c,
            "SELECT COUNT(*), COUNT(*) FILTER ("
            "WHERE rate_fingerprint IS NULL) FROM records")
    assert after == before[0], "the migration must not disturb existing rows"
    assert nulls == after, (
        "pre-migration rows must read NULL, not be backfilled")


def test_clean_bump_restamps_set_based_without_reading_rows(
        fresh_db, monkeypatch, caplog):
    """THE structural pin (issue #351): 30k stale rows across three
    pairs, every cost correct and every fingerprint stamped — a
    PRICING_VERSION bump that moved no pair's rate data must restamp
    the whole block in ONE set-based UPDATE and read ZERO rows into
    Python. The old shape's Python recompute costs about 20us per row,
    so the CPU budget below is unmeetable while any of the 30k rows
    reaches the keyset loop (that RED run is the evidence)."""
    monkeypatch.setattr(timing, "TIMING_ON", True)
    pairs = [("claude-opus-4-7", None), ("gpt-5.6-sol", None),
             ("claude-sonnet-4-5", None)]
    with db.viz_conn() as c:
        _seed_parents(c, _FILE_KEY)
        line = 1
        for model, provider in pairs:
            _seed_block(
                c, model, line, 10_000,
                fp=rate_fingerprint.pair_fingerprint(model, provider),
                cost=round(pricing.compute_cost(
                    model, **_SEED_INPUTS, ts=_SEED_TS, long_context=False,
                    provider=provider), 6),
                provider=provider)
            line += 10_000
        c.commit()

    sqls = _sql_recorder(monkeypatch)

    cpu0 = time.process_time()
    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        assert ingest_reprice.reprice_stale() == 0
    cpu = time.process_time() - cpu0
    assert cpu < 0.5, (
        f"the pass spent {cpu:.2f}s of CPU on a clean bump over 30k "
        "rows; the clean restamp must keep them out of Python")

    timing_line = _timing_line(caplog)
    rows_m = re.search(r"\brows=(\d+)", timing_line)
    assert rows_m and rows_m.group(1) == "0", (
        f"the keyset loop must read no rows, TIMING said {timing_line}")
    assert any("AS d(m, p, f)" in sql for sql in sqls), (
        "the SQL clean restamp must fire for the stamped pairs")
    assert not any("AS d(k, n" in sql for sql in sqls), (
        "no per-batch keyset write may run when every row went clean")

    with db.viz_conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM records "
            "WHERE pricing_version IS DISTINCT FROM %s",
            (constants.PRICING_VERSION,)).fetchone()
    assert row is not None and row[0] == 0, (
        "every row of a clean bump ends at the current version")


def test_clean_restamp_respects_the_rollback_guard(fresh_db):
    """Guard rows (a newer stored version, fingerprint stamped) must
    survive the SQL clean restamp untouched — the guard is in the
    UPDATE's WHERE, not only in the Python path."""
    newer = str(int(constants.PRICING_VERSION) + 1)
    fp = rate_fingerprint.pair_fingerprint(_SEED_MODEL, None)
    cost = _seeded_cost()
    with db.viz_conn() as c:
        _seed_block(c, _SEED_MODEL, 1, 5, fp=fp, cost=cost)
        # The guards carry the sentinel cost, so "untouched" is observable.
        _seed_block(c, _SEED_MODEL, 11, 5, fp=fp, cost=0.5,
                    pricing_version=newer)
        c.commit()

    assert ingest_reprice.reprice_stale() == 0

    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT line_num, cost_usd, pricing_version FROM records "
            "WHERE file_key = %s ORDER BY line_num", (_FILE_KEY,)).fetchall()
    assert len(rows) == 10
    for line_num, row_cost, version in rows:
        if line_num <= 5:
            assert version == constants.PRICING_VERSION, (
                "clean rows restamp to the current version")
        else:
            assert version == newer, "the guard row keeps its newer version"
            assert float(row_cost) == 0.5, (
                "the guard row keeps its sentinel through the clean restamp")


def test_stale_fingerprint_takes_the_python_path(fresh_db, monkeypatch,
                                                 caplog):
    """A row whose stored fingerprint is not its pair's current one
    cannot be proven clean, however right its cost looks: it reaches
    the keyset path, is recomputed, and comes out stamped with the
    CURRENT fingerprint and version."""
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1, pricing_version="0",
              cost_usd=_seeded_cost(), rate_fingerprint="stale-string")
        c.commit()

    monkeypatch.setattr(timing, "TIMING_ON", True)
    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        assert ingest_reprice.reprice_stale() == 0

    with db.viz_conn() as c:
        fingerprint, version = _pair(
            c, "SELECT rate_fingerprint, pricing_version FROM records "
               "WHERE file_key = %s AND line_num = 1", (_FILE_KEY,))
    assert fingerprint == rate_fingerprint.pair_fingerprint(
        _SEED_MODEL, None), "the row now carries the current fingerprint"
    assert version == constants.PRICING_VERSION
    assert re.search(r"\brows=1\b", _timing_line(caplog)), (
        "the row must have reached Python, not the SQL restamp")


def test_moved_pair_recomputes_while_the_clean_pair_restamps(
        fresh_db, monkeypatch, caplog):
    """A rate window appended for pair A only moves A's fingerprint:
    A's rows recompute (and the pass counts them changed), while pair
    B's rows — fingerprint unchanged — SQL-restamp without reaching
    Python."""
    monkeypatch.setattr(timing, "TIMING_ON", True)
    fp_a = rate_fingerprint.pair_fingerprint(_SEED_MODEL, None)
    fp_b = rate_fingerprint.pair_fingerprint(_B_PAIR_MODEL, None)
    cost_a = _seeded_cost()
    cost_b = round(pricing.compute_cost(
        _B_PAIR_MODEL, **_SEED_INPUTS, ts=_SEED_TS,
        long_context=False, provider=None), 6)
    with db.viz_conn() as c:
        _seed_block(c, _SEED_MODEL, 1, 5, fp=fp_a, cost=cost_a)
        _seed_block(c, _B_PAIR_MODEL, 11, 5, fp=fp_b, cost=cost_b)
        c.commit()

    # The mutation pair A's rows were priced under no longer describes:
    # the moved pair's WHOLE rows are replaced in BOTH tables with one
    # dated window that covers the seeded ts (end 2099-01-01, the
    # cache-gate test's spelling), dict-additive so no other pair's
    # resolution reads this patch. A's move is then self-contained —
    # it dominates whatever live or perturbed rows the tables carry —
    # and pair B stays untouched under any table content.
    monkeypatch.setattr(pricing, "DATED_RATES", {
        **pricing.DATED_RATES,
        _SEED_MODEL: [(datetime(2099, 1, 1, tzinfo=UTC), _MOVED_RATES)]})
    monkeypatch.setattr(pricing, "MODEL_RATES", {
        **pricing.MODEL_RATES,
        _SEED_MODEL: dict(_MOVED_RATES)})
    rate_fingerprint.clear_fingerprint_cache()

    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        assert ingest_reprice.reprice_stale() == 5

    assert re.search(r"\brows=5\b", _timing_line(caplog)), (
        "only pair A's rows may reach Python")

    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT line_num, cost_usd, pricing_version, rate_fingerprint "
            "FROM records WHERE file_key = %s ORDER BY line_num",
            (_FILE_KEY,)).fetchall()
    for line_num, row_cost, version, fingerprint in rows:
        if line_num <= 5:
            assert float(row_cost) == round(pricing.compute_cost(
                _SEED_MODEL, **_SEED_INPUTS, ts=_SEED_TS,
                long_context=False, provider=None), 6), (
                "pair A repriced under the window")
        else:
            assert float(row_cost) == cost_b, "pair B's cost is untouched"
        assert version == constants.PRICING_VERSION
        assert fingerprint == rate_fingerprint.pair_fingerprint(
            _SEED_MODEL if line_num <= 5 else _B_PAIR_MODEL, None)


def test_null_fingerprint_recomputes_once_then_goes_clean(
        fresh_db, monkeypatch, caplog):
    """Migration shape (issue #351): pre-feature rows read NULL and
    take the conservative recompute path once — the first run stamps
    their fingerprint, and the second run is the clean path."""
    cost = _seeded_cost()
    with db.viz_conn() as c:
        _seed_block(c, _SEED_MODEL, 1, 3, fp=None, cost=cost)
        c.commit()

    monkeypatch.setattr(timing, "TIMING_ON", True)
    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        assert ingest_reprice.reprice_stale() == 0
        assert re.search(r"\brows=3\b", _timing_line(caplog))

        assert ingest_reprice.reprice_stale() == 0
        assert re.search(r"\brows=0\b", _timing_line(caplog)), (
            "the second run must be the clean path")

    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT rate_fingerprint, pricing_version FROM records "
            "WHERE file_key = %s", (_FILE_KEY,)).fetchall()
    assert rows and all(
        fingerprint == rate_fingerprint.pair_fingerprint(_SEED_MODEL, None)
        and version == constants.PRICING_VERSION
        for fingerprint, version in rows)


def _odd_spellings() -> list[tuple[str | None, bool]]:
    """One row per odd stored-version spelling, as (version,
    restampable): the SQL restamp set is exactly {plain-digit versions
    <= V}; every other spelling reaches the keyset path, whose guard
    decides it exactly as before. All derived from the committed
    constant (never a literal, issue #198). Two traps: a version
    Python's int() parses GREATER than the binary's behind a sign the
    SQL cast refuses, and a plain-digit string too long for the SQL
    cast's int4 — the regex bound keeps it OUT of the set-based
    statement (an unbounded digit run would abort the whole UPDATE
    with integer-out-of-range where the guard must merely skip it)."""
    current = constants.PRICING_VERSION
    newer = str(int(current) + 1)
    return [
        (f"+{int(current) - 1}", True),   # SQL cast refuses the sign
        (f"{int(current) // 10}_{int(current) % 10}", True),  # int() gap
        ("abc", True),
        (None, True),
        (f" {current} ", True),           # whitespace-padded current
        (f"{int(current):04d}", True),    # plain digits <= V: the SQL set
        (f"+{newer}", False),             # int()-GREATER behind a sign
        ("9" * 14, False),                # plain digits, int4-overflowing
    ]


def _seed_spellings(c) -> list[tuple[str | None, bool]]:
    """Seed the odd spellings as one row each; every guarded row
    carries the 0.5 sentinel so its survival is observable."""
    spellings = _odd_spellings()
    fp = rate_fingerprint.pair_fingerprint(_SEED_MODEL, None)
    cost = _seeded_cost()
    for offset, (version, restampable) in enumerate(spellings):
        _seed(c, _FILE_KEY, offset + 1, pricing_version=version,
              cost_usd=0.5 if not restampable else cost,
              rate_fingerprint=fp)
    return spellings


def test_clean_restamp_and_python_guard_agree_on_odd_versions(
        fresh_db, monkeypatch, caplog):
    """The SQL restamp set is exactly {plain-digit versions <= V}: a
    subset of the keyset path's, because Python's int() parses
    spellings the SQL cast refuses (+5, 1_0, whitespace) and every
    stale row outside the SQL set still reaches the keyset path, whose
    _stored_pricing_version_is_newer decides it exactly as before. The
    trap spelling — plain-decimal-refusing but int()-GREATER — must
    survive untouched."""
    with db.viz_conn() as c:
        spellings = _seed_spellings(c)
        c.commit()

    monkeypatch.setattr(timing, "TIMING_ON", True)
    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        assert ingest_reprice.reprice_stale() == 0

    timing_line = _timing_line(caplog)
    # clean_rows=N is the kwarg rowcount; the phase mark spells
    # clean=Nms and must never read as the rowcount.
    clean_m = re.search(r"\bclean_rows=(\d+)", timing_line)
    assert clean_m and clean_m.group(1) == "1", (
        f"only the one plain-digit <= V row SQL-restamps: {timing_line}")

    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT line_num, cost_usd, pricing_version FROM records "
            "WHERE file_key = %s ORDER BY line_num",
            (_FILE_KEY,)).fetchall()
    assert [row[0] for row in rows] == list(range(1, len(spellings) + 1))
    for (version, restampable), (_line_num, row_cost, stored) in zip(
            spellings, rows):
        if restampable:
            assert stored == constants.PRICING_VERSION, (
                f"{version!r} is not newer by Python's guard, so the "
                "keyset path restamps it to the current version")
        else:
            assert stored == version, (
                f"{version!r} parses GREATER than the current version, so "
                "both the SQL restamp and the keyset guard must leave it")
            assert float(row_cost) == 0.5, "its sentinel survives"


# --------------------------------------------------------------------------
# Issue #377: a change to the reprice pass's OWN long-context rule,
# shipped with a PRICING_VERSION bump, must reprice: the fingerprint
# covers the pass's derivation, so the rule edit moves the pair's
# fingerprint and the clean restamp cannot claim the rows.
# --------------------------------------------------------------------------

_METER_377 = "gpt-5.6-sol"


def test_long_context_rule_change_shipped_with_a_bump_reprices(
        fresh_db, monkeypatch):
    """The #249 route end to end: the rule change (threshold collapsed
    to 1) rides its PRICING_VERSION bump, and the rule edit moves the
    pair's fingerprint — simulated at the seam the pass reads it
    through — so every stale row of the pair reaches Python, is
    recomputed under the new rule, and comes out repriced (flag moved),
    not clean-restamped."""
    cost = round(pricing.compute_cost(
        _METER_377, **_SEED_INPUTS, ts=_SEED_TS,
        long_context=False, provider=None), 6)
    # flag FALSE explicitly: the meter re-derivation keeps a NULL flag
    # whatever the rule does (issue #249), so a NULL-seeded row would
    # prove nothing.
    with db.viz_conn() as c:
        _seed_parents(c, _FILE_KEY)
        c.execute(
            "INSERT INTO records (file_key, line_num, ts, model, "
            "fresh_tokens, cache_creation_tokens, cache_read_tokens, "
            "output_tokens, eph5_tokens, eph1h_tokens, cost_usd, provider, "
            "long_context, pricing_version, rate_fingerprint) "
            "SELECT %s, i, %s, %s, 1000, 2000, 3000, 100, 250, 500, %s, "
            "NULL, FALSE, '0', %s FROM generate_series(1, 3) AS i",
            (_FILE_KEY, _SEED_TS, _METER_377, cost,
             rate_fingerprint.pair_fingerprint(_METER_377, None)))
        c.commit()

    real_fp = rate_fingerprint.pair_fingerprint

    def _moved(model, provider=None):
        if model == _METER_377 and provider is None:
            return "0" * 64  # the digest an edit to the rule would produce
        return real_fp(model, provider)

    monkeypatch.setattr(rate_fingerprint, "pair_fingerprint", _moved)
    monkeypatch.setattr(pricing, "LONG_CONTEXT_THRESHOLD", 1)
    monkeypatch.setattr(
        constants, "PRICING_VERSION", str(int(constants.PRICING_VERSION) + 1))

    assert ingest_reprice.reprice_stale() == 3
    with db.viz_conn() as c:
        flags = c.execute(
            "SELECT bool_and(long_context) FROM records").fetchone()
    assert flags is not None and flags[0], (
        "every row must come out under the changed rule (flag moved "
        "FALSE -> TRUE)")
