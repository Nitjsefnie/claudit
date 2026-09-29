"""records.pricing_version and the reprice pass (issue #193).

A rate change must rePRICE stored records — recompute cost_usd from the
stored token columns — instead of re-parsing every R2 object. Two halves
are tested here:

- persist-time stamping (backend/ingest_persist.py): every INSERT names
  the PRICING_VERSION its row was priced under, and a reparse naturally
  re-stamps it. NULL counts as stale — priced by an older build. The
  column is additive and nullable (SV-SCHEMA-AUTOAPPLY), so the next
  startup's apply_schema() adds it without disturbing anything.

- the reprice pass (backend/ingest_reprice.py, wired as a derived-state
  phase between suppression and the canonical pass): selects rows whose
  stored pricing_version differs from constants.PRICING_VERSION (NULL
  counts as stale) and recomputes their cost from STORED COLUMNS ONLY —
  no R2 access, no reparse. Its PROOF is parity: repriced rows must
  equal what a full reparse of the same bytes under the same rate
  tables yields — that proof lives in tests/test_reprice_parity.py.
"""
from __future__ import annotations

import contextlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import pytest

# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture,
    _mini_r2_env_fixture,
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

# Rates deliberately unlike any real price, so an assertion against them
# can never be mistaken for a pricing fact (the same rule the conftest
# synthetic-rate fixtures follow).
_SCHED_RATES = {"fresh": 0.5, "create_5m": 0.625, "create_1h": 1.0,
                "read": 0.05, "output": 2.5}
_MOVED_RATES = {"fresh": 8.5, "create_5m": 10.625, "create_1h": 17.0,
                "read": 0.85, "output": 42.5}

# One seeded record's token tally: unsplit_create = 2000 - 250 - 500,
# so the pass's unsplit arithmetic is exercised by every seeded row.
_SEED_MODEL = "claude-opus-4-7"
_SEED_TS = datetime(2026, 5, 7, 10, 0, tzinfo=UTC)
_SEED_TOKENS = (1_000, 2_000, 3_000, 100, 250, 500)
_SEED_INPUTS = {"fresh": 1_000, "output": 100, "eph5": 250, "eph1h": 500,
                "unsplit_create": 1_250, "read": 3_000}

# The file key every unit test seeds its rows under (fresh_db is
# per-test, so the name never collides across tests).
_FILE_KEY = "claude/reprice-test/sess-a/sess-a.jsonl"


def _pair(c, sql: str, params=None) -> tuple[int, int]:
    """The one row's two columns; the query must yield one (like
    test_ingest._scalar, for a two-column aggregate)."""
    row = c.execute(sql, params).fetchone()
    assert row is not None, f"expected a row: {sql[:80]}"
    return row[0], row[1]


def test_ingest_stamps_pricing_version(fresh_db, mini_r2_env):
    """run_ingest over the mini mirror stamps every record with the
    current PRICING_VERSION — the value the reprice pass compares
    against."""
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None

    with db.viz_conn() as c:
        total, stamped = _pair(
            c,
            "SELECT COUNT(*), COUNT(*) FILTER ("
            "WHERE pricing_version = %s) FROM records",
            (constants.PRICING_VERSION,),
        )
    assert total > 0, "the mirror must produce records, or this proves nothing"
    assert stamped == total, (
        f"{total - stamped} of {total} records were not stamped "
        f"with pricing_version {constants.PRICING_VERSION!r}")


def test_pricing_version_column_is_nullable_migration(fresh_db, mini_r2_env):
    """A DB already holding pre-migration-shaped records rows gains the
    column at the next schema pass: additive, NULL-allowing, and the
    existing rows keep their place — reading NULL, the shape the reprice
    pass treats as stale."""
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None

    # Put the DB into the pre-migration shape: the column the new schema
    # would create is absent while data is already in `records`.
    with db.viz_conn() as c:
        c.execute(
            "ALTER TABLE records DROP COLUMN IF EXISTS pricing_version")
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
            "AND column_name = 'pricing_version'").fetchone()
        assert row is not None and row[0] == "YES", (
            "schema.sql must add pricing_version and admit NULL")
        after, nulls = _pair(
            c,
            "SELECT COUNT(*), COUNT(*) FILTER ("
            "WHERE pricing_version IS NULL) FROM records")
    assert after == before[0], "the migration must not disturb existing rows"
    assert nulls == after, (
        "pre-migration rows must read NULL, not be backfilled")


def test_ingest_stamps_rate_fingerprint(fresh_db, mini_r2_env):
    """Issue #351: persist stamps each record's rate fingerprint beside
    its pricing_version — the fingerprint of the pair its stored cost
    was computed under, so a later reprice can recognise rows whose
    pair's rate data has not moved."""
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


def _seed_parents(c, file_key: str) -> None:
    """The project+file rows every seeded record needs (idempotent)."""
    c.execute(
        "INSERT INTO projects (project_id, display_name, first_seen_at, "
        "last_seen_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (project_id) DO NOTHING",
        ("reprice-test", "reprice-test", _SEED_TS, _SEED_TS),
    )
    c.execute(
        "INSERT INTO files (file_key, project_id, session_id, is_main, "
        "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
        "parser_version) VALUES (%s, %s, %s, TRUE, %s, %s, %s, %s, %s) "
        "ON CONFLICT (file_key) DO NOTHING",
        (file_key, "reprice-test", "sess-seed", "seed-etag", 12,
         _SEED_TS, _SEED_TS, constants.PARSER_VERSION),
    )


def _seed(c, file_key: str, line_num: int, *, pricing_version=None,
          cost_usd: float = 0.5, model: str = _SEED_MODEL, ts=_SEED_TS,
          provider: str | None = None,
          rate_fingerprint: str | None = None) -> None:  # pylint: disable=redefined-outer-name
    """One record row under seeded project+file parents, with a fixed
    token tally (_SEED_TOKENS) a test prices through compute_cost.

    cost_usd is a sentinel far from any computed value, so "untouched"
    is observable. rate_fingerprint seeds the pair fingerprint the
    reprice pass compares against (issue #351); None inserts NULL, the
    pre-feature shape. The parameter is the stored column's own name,
    hence the shadow of the module import of the same name.
    """
    _seed_parents(c, file_key)
    fresh, create, read, output, eph5, eph1h = _SEED_TOKENS
    c.execute(
        "INSERT INTO records (file_key, line_num, ts, model, fresh_tokens, "
        "cache_creation_tokens, cache_read_tokens, output_tokens, "
        "eph5_tokens, eph1h_tokens, cost_usd, provider, pricing_version, "
        "rate_fingerprint) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (file_key, line_num, ts, model, fresh, create, read,
         output, eph5, eph1h, cost_usd, provider, pricing_version,
         rate_fingerprint),
    )


def _seed_meter_row(c, line_num: int, *, model: str, fresh_tokens: int,
                    provider: str | None = None,
                    long_context: bool | None = None) -> None:
    """One stale record row whose tally the test chooses, inserted
    DIRECTLY: the fixed-tally _seed can carry no meter-sized tally and
    names no long_context. The INSERT names the long_context column, so
    a seeded flag is really in the row before the pass runs."""
    _seed_parents(c, _FILE_KEY)
    c.execute(
        "INSERT INTO records (file_key, line_num, ts, model, fresh_tokens, "
        "cache_creation_tokens, cache_read_tokens, output_tokens, "
        "eph5_tokens, eph1h_tokens, cost_usd, provider, long_context, "
        "pricing_version) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (_FILE_KEY, line_num, _SEED_TS, model, fresh_tokens,
         0, 0, 0, 0, 0, 0.5, provider, long_context, None),
    )


def _seeded_cost() -> float:
    """What compute_cost prices the seeded tally at, under the tables
    as they stand right now."""
    return round(pricing.compute_cost(
        _SEED_MODEL, **_SEED_INPUTS, ts=_SEED_TS,
        long_context=False, provider=None), 6)


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
    """The pass's one TIMING line from the captured log."""
    lines = [record.getMessage() for record in caplog.records
             if record.getMessage().startswith("TIMING reprice")]
    assert lines, "the pass must emit its TIMING line"
    return lines[-1]


def _rows(c) -> list:
    """The seeded file's record rows as (line_num, cost_usd,
    pricing_version), ordered by line_num."""
    return c.execute(
        "SELECT line_num, cost_usd, pricing_version FROM records "
        "WHERE file_key = %s ORDER BY line_num",
        (_FILE_KEY,),
    ).fetchall()


def test_reprice_selects_null_version(fresh_db):
    """NULL pricing_version is stale: the row is repriced from its own
    stored columns to exactly what compute_cost prices the tally at,
    and re-stamped with the current version."""
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1)
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        cost, version = _pair(
            c, "SELECT cost_usd, pricing_version FROM records "
               "WHERE file_key = %s AND line_num = 1", (_FILE_KEY,))
    assert float(cost) == _seeded_cost()
    assert version == constants.PRICING_VERSION


def test_reprice_leaves_the_current_version_alone(fresh_db):
    """A row already stamped with the current PRICING_VERSION is not
    stale: neither repriced nor re-stamped, its stored cost sentinel
    intact."""
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1)
        _seed(c, _FILE_KEY, 2, pricing_version=constants.PRICING_VERSION)
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        rows = _rows(c)
    assert float(rows[0][1]) == _seeded_cost()
    assert rows[0][2] == constants.PRICING_VERSION
    assert float(rows[1][1]) == 0.5, "the current row keeps its sentinel"
    assert rows[1][2] == constants.PRICING_VERSION


def test_reprice_skips_rows_priced_by_a_newer_version(fresh_db, caplog):
    """The rollback guard mirrors _stored_version_is_newer (issue #118):
    a stored version that parses as an int and is GREATER than the
    current one was priced by a newer build, and an older binary must
    not clobber it. The row is skipped, the keyset advances past it,
    and the skip is logged."""
    # NEWER than the binary's own, derived from the committed constant so
    # the guard's semantics hold whatever the real value has reached
    # (issue #198).
    newer = str(int(constants.PRICING_VERSION) + 1)
    with db.viz_conn() as c:
        for line_num, version in ((1, None), (2, "0"), (3, newer)):
            _seed(c, _FILE_KEY, line_num, pricing_version=version)
        c.commit()

    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        assert ingest_reprice.reprice_stale() == 2

    with db.viz_conn() as c:
        rows = _rows(c)
    assert [row[2] for row in rows] == [
        constants.PRICING_VERSION, constants.PRICING_VERSION, newer]
    assert float(rows[2][1]) == 0.5, "the newer row keeps its sentinel"
    assert "skipped 1 record" in caplog.text


def test_reprice_reprices_a_non_integer_version(fresh_db):
    """A stored pricing_version that does not parse as an int cannot be
    shown NEWER, so the guard's ordinary-staleness branch applies: the
    row is repriced from its stored columns and stamped with the current
    version, exactly like a NULL."""
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1, pricing_version="abc")
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        cost, version = _pair(
            c, "SELECT cost_usd, pricing_version FROM records "
               "WHERE file_key = %s AND line_num = 1", (_FILE_KEY,))
    assert float(cost) == _seeded_cost()
    assert version == constants.PRICING_VERSION


def test_reprice_is_idempotent(fresh_db):
    """Rows the first run stamped no longer differ from the current
    version, so the second run selects nothing."""
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1)
        _seed(c, _FILE_KEY, 2)
        c.commit()

    assert ingest_reprice.reprice_stale() == 2
    assert ingest_reprice.reprice_stale() == 0


def test_reprice_batches_across_the_keyset(fresh_db, monkeypatch):
    """Seven rows against a REPRICE_BATCH of 2: the keyset cursor must
    carry past updated AND guard-skipped rows — a batch that could not
    advance past a skip would spin forever. The skip sits at the batch
    boundary (line 4, the last row of the second batch of two) on
    purpose."""
    monkeypatch.setattr(ingest_reprice, "REPRICE_BATCH", 2)
    # NEWER than the binary's own, derived (issue #198) — same reasoning
    # as the guard test above.
    newer = str(int(constants.PRICING_VERSION) + 1)
    with db.viz_conn() as c:
        for line_num in range(1, 8):
            _seed(c, _FILE_KEY, line_num,
                  pricing_version=newer if line_num == 4 else None)
        c.commit()

    assert ingest_reprice.reprice_stale() == 6

    with db.viz_conn() as c:
        rows = _rows(c)
    assert [row[2] for row in rows] == [
        constants.PRICING_VERSION, constants.PRICING_VERSION,
        constants.PRICING_VERSION, newer, constants.PRICING_VERSION,
        constants.PRICING_VERSION, constants.PRICING_VERSION]
    assert float(rows[3][1]) == 0.5, "the skipped row keeps its sentinel"


def test_reprice_aborts_between_batches_when_shutdown_requested(
        fresh_db, monkeypatch):
    """A shutdown request between batches unwinds the pass via
    IngestAborted: every batch already committed persists, the batch the
    abort interrupts never opens, and the next run converges — the
    ingest stop contract (issue #103) at the reprice pass's own bounded
    steps. should_stop returning True (or raising) is the signal."""
    monkeypatch.setattr(ingest_reprice, "REPRICE_BATCH", 2)
    with db.viz_conn() as c:
        for line_num in range(1, 8):
            _seed(c, _FILE_KEY, line_num)
        c.commit()

    checks = iter([False, True])
    with pytest.raises(ingest.IngestAborted):
        ingest_reprice.reprice_stale(should_stop=lambda: next(checks))

    with db.viz_conn() as c:
        rows = _rows(c)
    versions = [row[2] for row in rows]
    assert versions[:2] == [constants.PRICING_VERSION] * 2, (
        "the first batch was committed before the abort, so it persists")
    assert versions[2:] == [None] * 5, (
        "rows the aborted pass never reached stay stale (NULL version)")


def test_reprice_prices_a_provider_row(fresh_db,
                                       synthetic_provider_dated_rate):
    """A record naming its serving host prices from PROVIDER_RATES keyed
    (model, provider), the row's dated windows included: seeded rows on
    both sides of the synthetic row's cutover must reprice to exactly
    what compute_cost — the parse path's own pricing code — yields for
    the same stored columns. No fixture-backed record names a provider,
    so this shape is seeded and compared by construction (SV-RATE-DATA)."""
    prov = synthetic_provider_dated_rate
    before_ts = prov.start + timedelta(days=5)   # inside the dated window
    after_ts = prov.cutover + timedelta(days=5)  # at the row's list price
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1, model=prov.model, ts=before_ts,
              provider=prov.host)
        _seed(c, _FILE_KEY, 2, model=prov.model, ts=after_ts,
              provider=prov.host)
        c.commit()

    assert ingest_reprice.reprice_stale() == 2

    with db.viz_conn() as c:
        rows = _rows(c)
    assert [row[0] for row in rows] == [1, 2]
    for line_num, cost, _version in rows:
        ts = before_ts if line_num == 1 else after_ts
        assert float(cost) == round(pricing.compute_cost(
            prov.model, **_SEED_INPUTS, ts=ts, long_context=False,
            provider=prov.host), 6), f"line {line_num} repriced by host"


def test_reprice_prices_a_weekly_schedule(fresh_db,
                                          synthetic_provider_dated_rate,
                                          monkeypatch):
    """A schedule on a provider row REPLACES the row's rates inside its
    windows. The synthetic row carries one dated window, so a record
    after the cutover reads schedule entry 1; seeded with an
    always-applies window, the repriced row must cost exactly what
    compute_cost prices through the same schedule."""
    prov = synthetic_provider_dated_rate
    ts = prov.cutover + timedelta(days=5)
    # Entry index 1: exactly one dated window (the cutover) ends at or
    # before ts. (None, None, None, rates) = every day, the whole day.
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {
        (prov.model, prov.host): {1: [(None, None, None, _SCHED_RATES)]}})
    assert pricing.rate_for(prov.model, ts, prov.host) is _SCHED_RATES, (
        "the schedule must be the price in force at ts, or this test "
        "proves nothing")
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1, model=prov.model, ts=ts, provider=prov.host)
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        cost, _version = _pair(
            c, "SELECT cost_usd, pricing_version FROM records "
               "WHERE file_key = %s AND line_num = 1", (_FILE_KEY,))
    assert float(cost) == round(pricing.compute_cost(
        prov.model, **_SEED_INPUTS, ts=ts, long_context=False,
        provider=prov.host), 6)


def test_reprice_rederives_long_context_for_the_meters_models(fresh_db):
    """Issue #194: for the meter's models the long-context flag is a pure
    function of stored columns — fresh + cache_creation + cache_read
    against the threshold — so the pass re-derives it beside the cost. A
    stale gpt-5.6-sol row whose tally sits above the 272k threshold flips
    from its stored FALSE — what the old subscription-exempt rule wrote —
    to TRUE with its metered cost; a claude row seeded alongside — a
    model whose card carries no meter — keeps its stored NULL and prices
    flat above the same threshold."""
    metered = round(pricing.compute_cost(
        "gpt-5.6-sol", fresh=280_000, output=0, eph5=0, eph1h=0,  # sv-test-data: allow (derived: expected priced from the same loaded tables as the reprice pass)
        unsplit_create=0, read=0, ts=_SEED_TS, long_context=True), 6)
    flat = round(pricing.compute_cost(
        _SEED_MODEL, fresh=280_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=_SEED_TS, long_context=False), 6)
    with db.viz_conn() as c:
        _seed_meter_row(c, 1, model="gpt-5.6-sol", fresh_tokens=280_000,
                        long_context=False)
        _seed_meter_row(c, 2, model=_SEED_MODEL, fresh_tokens=280_000)
        c.commit()

    assert ingest_reprice.reprice_stale() == 2

    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT line_num, long_context, cost_usd, pricing_version "
            "FROM records WHERE file_key = %s ORDER BY line_num",
            (_FILE_KEY,)).fetchall()
    codex, claude = rows
    assert codex[0] == 1 and claude[0] == 2
    assert codex[1] is True
    assert float(codex[2]) == metered
    assert codex[3] == constants.PRICING_VERSION
    assert claude[1] is None, "a no-meter model's stored flag is untouched"
    assert float(claude[2]) == flat


def test_reprice_keeps_a_claude_rows_stored_null_flag(fresh_db):
    """Issue #249: a CLAUDE-format record from a bare meter model stores
    long_context NULL — the flag is lane-only
    (parse_common._append_usage_record), so NULL is the parse-stored value
    and the pass must keep it, pricing the tally flat. A bare model id
    with no provider above the threshold is exactly the shape
    re-derivation must not claim: a reparse of the same record stores
    NULL and a flat cost."""
    flat = round(pricing.compute_cost(
        "gpt-6-sol", fresh=300_000, output=0, eph5=0, eph1h=0,  # sv-test-data: allow (derived: expected priced from the same loaded tables as the reprice pass)
        unsplit_create=0, read=0, ts=_SEED_TS, long_context=False), 6)
    with db.viz_conn() as c:
        _seed_meter_row(c, 1, model="gpt-6-sol", fresh_tokens=300_000)
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        row = c.execute(
            "SELECT long_context, cost_usd, pricing_version FROM records "
            "WHERE file_key = %s AND line_num = 1", (_FILE_KEY,)).fetchone()
    assert row is not None, "the seeded meter row must exist"
    flag, cost, version = row
    assert flag is None, (
        "a Claude-format row's NULL flag is the parse-stored value, and a "
        "reprice must not claim it for the meter")
    assert float(cost) == flat
    assert version == constants.PRICING_VERSION


def test_reprice_keeps_a_provider_rows_stored_flag(fresh_db):
    """A row naming a provider host prices by that host's card, not the
    meter model's, so its stored long_context is left exactly as parse
    stored it: FALSE stays FALSE even though the model is one of the
    meter's and the tally sits above the threshold."""
    with db.viz_conn() as c:
        _seed_meter_row(c, 1, model="gpt-5.6-sol", fresh_tokens=280_000,
                        provider="OpenRouter", long_context=False)
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        row = c.execute(
            "SELECT long_context, cost_usd, pricing_version FROM records "
            "WHERE file_key = %s AND line_num = 1", (_FILE_KEY,)).fetchone()
    assert row is not None, "the seeded provider row must exist"
    flag, cost, version = row
    assert flag is False
    assert float(cost) == round(pricing.compute_cost(
        "gpt-5.6-sol", fresh=280_000, output=0, eph5=0, eph1h=0,  # sv-test-data: allow (derived: expected priced from the same loaded tables as the reprice pass)
        unsplit_create=0, read=0, ts=_SEED_TS, long_context=False,
        provider="OpenRouter"), 6)
    assert version == constants.PRICING_VERSION


def test_reprice_phase_runs_between_suppression_and_canonical(monkeypatch):
    """The phases tuple resolves its names through ingest's globals at
    call time, so stubbing every phase on `ingest` records the order the
    rebuild runs them in. Reprice is a records mutation, so it runs after
    suppression and before the alias fold, canonical pass and every
    rollup."""
    order = []

    def stub(name):
        # The reprice phase is invoked through a partial that forwards
        # should_stop, so every stub accepts (and ignores) arguments.
        return lambda *args, **kwargs: order.append(name)

    phases = ("purge_suppressed", "reprice_stale", "rekey_folded_projects",
              "recompute_canonical",
              "resolve_teammate_agent_types", "rebuild_rollup",
              "rebuild_tool_rollup", "rebuild_tool_error_rollup",
              "rebuild_dispatch_rollup", "rebuild_dispatch_brief_rollup",
              "rebuild_latency_rollup", "rebuild_ctx_cost_rollup",
              "rebuild_agent_rollup")
    for name in phases:
        monkeypatch.setattr(ingest, name, stub(name))
    ingest._rebuild_derived_state()  # pylint: disable=protected-access

    assert set(order) == set(phases), "every phase ran exactly once"
    assert order[:4] == ["purge_suppressed", "reprice_stale",
                         "rekey_folded_projects", "recompute_canonical"]


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

    line_text = _timing_line(caplog)
    rows_m = re.search(r"\brows=(\d+)", line_text)
    assert rows_m and rows_m.group(1) == "0", (
        f"the keyset loop must read no rows, TIMING said {line_text}")
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
    fp_b = rate_fingerprint.pair_fingerprint("claude-sonnet-4-5", None)
    cost_a = _seeded_cost()
    cost_b = round(pricing.compute_cost(
        "claude-sonnet-4-5", **_SEED_INPUTS, ts=_SEED_TS,
        long_context=False, provider=None), 6)
    with db.viz_conn() as c:
        _seed_block(c, _SEED_MODEL, 1, 5, fp=fp_a, cost=cost_a)
        _seed_block(c, "claude-sonnet-4-5", 11, 5, fp=fp_b, cost=cost_b)
        c.commit()

    # The mutation pair A's rows were priced under no longer describe:
    # a window now covers their ts, so both cost and fingerprint move.
    monkeypatch.setattr(pricing, "DATED_RATES", {
        _SEED_MODEL: [(datetime(2027, 1, 1, tzinfo=UTC), _MOVED_RATES)]})
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
            _SEED_MODEL if line_num <= 5 else "claude-sonnet-4-5", None)


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
    constant (never a literal, issue #198); the last entry is the trap
    — a version Python's int() parses GREATER than the binary's behind
    a sign the SQL cast refuses."""
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
    ]


def _seed_spellings(c) -> list[tuple[str | None, bool]]:
    """Seed the odd spellings as one row each; the trap row carries the
    0.5 sentinel so its survival is observable."""
    spellings = _odd_spellings()
    fp = rate_fingerprint.pair_fingerprint(_SEED_MODEL, None)
    cost = _seeded_cost()
    for offset, (version, _restampable) in enumerate(spellings):
        _seed(c, _FILE_KEY, offset + 1, pricing_version=version,
              cost_usd=0.5 if version == spellings[-1][0] else cost,
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
    # The KWARG clean=N (rowcount), not the clean=Nms phase mark.
    clean_m = re.search(r"\bclean=(\d+)(?!ms)", timing_line)
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
