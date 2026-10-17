"""records.pricing_version and the reprice pass.

A rate change must rePRICE stored records — recompute cost_usd from the
stored token columns — instead of re-parsing every R2 object.

- the reprice pass (backend/ingest_reprice.py, wired as a derived-state
  phase between suppression and the canonical pass): selects rows whose
  stored pricing_version differs from constants.PRICING_VERSION (NULL
  counts as stale) and recomputes their cost from STORED COLUMNS ONLY —
  no R2 access, no reparse. Its PROOF is parity: repriced rows must
  equal what a full reparse of the same bytes under the same rate
  tables yields — that proof lives in tests/test_reprice_parity.py.
"""
from __future__ import annotations

import logging
from datetime import timedelta, timezone

import pytest

# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture,
    _mini_r2_env_fixture,
)

from backend import constants, db, ingest, ingest_reprice, pricing
from tests import dims_seed
from tests.reprice_fixture_builders import (
    SEED_INPUTS as _SEED_INPUTS,
    SEED_MODEL as _SEED_MODEL,
    SEED_TOKENS as _SEED_TOKENS,
    SEED_TS as _SEED_TS,
    SeedRecord,
    seed_parents as _seed_parents,
    seed_record as _seed,
)

UTC = timezone.utc

# Rates deliberately unlike any real price, so an assertion against them
# can never be mistaken for a pricing fact (the same rule the conftest
# synthetic-rate fixtures follow).
_SCHED_RATES = {"fresh": 0.5, "create_5m": 0.625, "create_1h": 1.0,
                "read": 0.05, "output": 2.5}

# One seeded record's token tally: unsplit_create = 2000 - 250 - 500,
# so the pass's unsplit arithmetic is exercised by every seeded row.

# The file key every unit test seeds its rows under (fresh_db is
# per-test, so the name never collides across tests).
_FILE_KEY = "claude/reprice-test/sess-a/sess-a.jsonl"


def _pair(c, sql: str, params=None) -> tuple[int, int]:
    """The one row's two columns; the query must yield one (like
    test_ingest._scalar, for a two-column aggregate)."""
    row = c.execute(sql, params).fetchone()
    assert row is not None, f"expected a row: {sql[:80]}"
    return row[0], row[1]


def _seed_meter_row(c, line_num: int, *, model: str, fresh_tokens: int,
                    provider: str | None = None,
                    long_context: bool | None = None) -> None:
    """One stale record row whose tally the test chooses, inserted
    DIRECTLY: the fixed-tally _seed can carry no meter-sized tally and
    names no long_context. The sidecar INSERT names the long_context column, so
    a seeded flag is really in the row before the pass runs."""
    _seed_parents(c, _FILE_KEY)
    fid = dims_seed.file_id(c, _FILE_KEY)
    mid = dims_seed.dim_id(c, "dim_model", model)
    provid = (None if provider is None
              else dims_seed.dim_id(c, "dim_provider", provider))
    c.execute(
        "INSERT INTO records (file_id, line_num, ts, model_id, "
        "fresh_tokens, cache_creation_tokens, cache_read_tokens, "
        "output_tokens, eph5_tokens, eph1h_tokens, provider_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (fid, line_num, _SEED_TS, mid, fresh_tokens,
         0, 0, 0, 0, 0, provid),
    )
    c.execute(
        "INSERT INTO records_price (file_id, line_num, cost_usd, "
        "long_context, pricing_version_id) VALUES (%s, %s, %s, %s, %s)",
        (fid, line_num, 0.5, long_context, None))


def _seeded_cost() -> float:
    """What compute_cost prices the seeded tally at, under the tables
    as they stand right now."""
    return round(pricing.compute_cost(
        _SEED_MODEL, **_SEED_INPUTS, ts=_SEED_TS,
        adjustments=pricing.CostAdjustments(long_context=False)), 6)


# A synthetic search price, deliberately unlike a deployed listing.
_SEARCH_HOST = "SearchHost"
_SEARCH_RATES = {"fresh": 1.5, "create_5m": 1.875, "create_1h": 3.0,
                 "read": 0.15, "output": 7.5}
_SEARCH_RATE = 0.0137


def _search_tables(monkeypatch) -> None:
    """Install synthetic provider rates, including a per-search price."""
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {
        **pricing.PROVIDER_RATES,
        (_SEED_MODEL, _SEARCH_HOST):
            {**_SEARCH_RATES, "web_search": _SEARCH_RATE},
    })
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {})
    monkeypatch.setattr(pricing, "PROVIDER_STARTS", {})
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {})


def test_reprice_multiplies_stored_search_count_and_clears_old_fee(
        fresh_db, monkeypatch):
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1, SeedRecord(
            pricing_version=None, cost_usd=0.5, provider="SearchHost",
            request_fee_usd=0.99, web_search_requests=3))
        c.commit()
    _search_tables(monkeypatch)

    assert ingest_reprice.reprice_stale() == 1
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT cost_usd, request_fee_usd, web_search_requests "
            "FROM records r JOIN records_price rp ON rp.file_id = r.file_id AND rp.line_num = r.line_num JOIN files f ON f.file_id = r.file_id "
            "WHERE f.file_key = %s AND r.line_num = 1",
            (_FILE_KEY,)).fetchone()
    assert row is not None
    cost, old_fee, searches = row
    expected = round(pricing.compute_cost(
        _SEED_MODEL, **_SEED_INPUTS, ts=_SEED_TS,
        adjustments=pricing.CostAdjustments(web_search_requests=3),
        res=pricing.resolve(_SEED_MODEL, _SEED_TS, _SEARCH_HOST)), 6)
    assert float(cost) == expected
    assert old_fee is None
    assert searches == 3


def test_reprice_null_search_count_does_not_charge_search_or_keep_old_fee(
        fresh_db, monkeypatch):
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1, SeedRecord(
            pricing_version=None, cost_usd=0.5, provider=_SEARCH_HOST,
            request_fee_usd=0.99))
        c.commit()
    _search_tables(monkeypatch)
    assert ingest_reprice.reprice_stale() == 1
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT cost_usd, request_fee_usd FROM records r "
            "JOIN records_price rp ON rp.file_id = r.file_id AND rp.line_num = r.line_num JOIN files f ON f.file_id = r.file_id "
            "WHERE f.file_key = %s AND r.line_num = 1",
            (_FILE_KEY,)).fetchone()
    assert row is not None
    cost, old_fee = row
    expected = round(pricing.compute_cost(
        _SEED_MODEL, **_SEED_INPUTS, ts=_SEED_TS,
        res=pricing.resolve(_SEED_MODEL, _SEED_TS, _SEARCH_HOST)), 6)
    assert float(cost) == expected
    assert old_fee is None


def test_reprice_restamps_when_search_cost_already_matches(fresh_db,
                                                           monkeypatch):
    _search_tables(monkeypatch)
    search_cost = round(pricing.compute_cost(
        _SEED_MODEL, **_SEED_INPUTS, ts=_SEED_TS,
        adjustments=pricing.CostAdjustments(web_search_requests=3),
        res=pricing.resolve(_SEED_MODEL, _SEED_TS, _SEARCH_HOST)), 6)
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1, SeedRecord(
            pricing_version=None, cost_usd=search_cost, provider=_SEARCH_HOST,
            web_search_requests=3))
        c.commit()
    assert ingest_reprice.reprice_stale() == 0
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT pv.value, request_fee_usd FROM records r "
            "JOIN records_price rp ON rp.file_id = r.file_id AND rp.line_num = r.line_num JOIN files f ON f.file_id = r.file_id "
            "LEFT JOIN dim_pricing_version pv "
            "  ON pv.id = rp.pricing_version_id "
            "WHERE f.file_key = %s AND r.line_num = 1",
            (_FILE_KEY,)).fetchone()
    assert row is not None
    version, old_fee = row
    assert version == constants.PRICING_VERSION
    assert old_fee is None


def _rows(c) -> list:
    """The seeded file's record rows as (line_num, cost_usd,
    pricing_version), ordered by line_num."""
    return c.execute(
        "SELECT r.line_num, rp.cost_usd, pv.value "
        "FROM records r JOIN records_price rp ON rp.file_id = r.file_id AND rp.line_num = r.line_num JOIN files f ON f.file_id = r.file_id "
        "LEFT JOIN dim_pricing_version pv ON pv.id = rp.pricing_version_id "
        "WHERE f.file_key = %s ORDER BY r.line_num",
        (_FILE_KEY,),
    ).fetchall()


@pytest.mark.parametrize("pricing_version", [None, "abc"],
                         ids=["null", "non-integer"])
def test_reprice_selects_null_version(fresh_db, pricing_version):
    """NULL or non-integer pricing_version is stale: the row is repriced from its own
    stored columns to exactly what compute_cost prices the tally at,
    and re-stamped with the current version."""
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1, SeedRecord(pricing_version=pricing_version))
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        cost, version = _pair(
            c, "SELECT rp.cost_usd, pv.value "
               "FROM records r JOIN records_price rp ON rp.file_id = r.file_id AND rp.line_num = r.line_num JOIN files f ON f.file_id = r.file_id "
               "LEFT JOIN dim_pricing_version pv "
               "  ON pv.id = rp.pricing_version_id "
               "WHERE f.file_key = %s AND r.line_num = 1", (_FILE_KEY,))
    assert float(cost) == _seeded_cost()
    assert version == constants.PRICING_VERSION


def test_reprice_leaves_the_current_version_alone(fresh_db):
    """A row already stamped with the current PRICING_VERSION is not
    stale: neither repriced nor re-stamped, its stored cost sentinel
    intact."""
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1)
        _seed(c, _FILE_KEY, 2,
              SeedRecord(pricing_version=constants.PRICING_VERSION))
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        rows = _rows(c)
    assert float(rows[0][1]) == _seeded_cost()
    assert rows[0][2] == constants.PRICING_VERSION
    assert float(rows[1][1]) == 0.5, "the current row keeps its sentinel"
    assert rows[1][2] == constants.PRICING_VERSION


def test_reprice_skips_rows_priced_by_a_newer_version(fresh_db, caplog):
    """The rollback guard mirrors _stored_version_is_newer:
    a stored version that parses as an int and is GREATER than the
    current one was priced by a newer build, and an older binary must
    not clobber it. The row is skipped, the keyset advances past it,
    and the skip is logged."""
    # NEWER than the binary's own, derived from the committed constant so
    # the guard's semantics hold whatever the real value has reached
    #
    newer = str(int(constants.PRICING_VERSION) + 1)
    with db.viz_conn() as c:
        for line_num, version in ((1, None), (2, "0"), (3, newer)):
            _seed(c, _FILE_KEY, line_num,
                  SeedRecord(pricing_version=version))
        c.commit()

    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        assert ingest_reprice.reprice_stale() == 2

    with db.viz_conn() as c:
        rows = _rows(c)
    assert [row[2] for row in rows] == [
        constants.PRICING_VERSION, constants.PRICING_VERSION, newer]
    assert float(rows[2][1]) == 0.5, "the newer row keeps its sentinel"
    assert "skipped 1 record" in caplog.text


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
    # NEWER than the binary's own, derived — same reasoning
    # as the guard test above.
    newer = str(int(constants.PRICING_VERSION) + 1)
    with db.viz_conn() as c:
        for line_num in range(1, 8):
            _seed(c, _FILE_KEY, line_num, SeedRecord(
                pricing_version=newer if line_num == 4 else None))
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
    ingest stop contract at the reprice pass's own bounded
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
        _seed(c, _FILE_KEY, 1,
              SeedRecord(model=prov.model, ts=before_ts, provider=prov.host))
        _seed(c, _FILE_KEY, 2,
              SeedRecord(model=prov.model, ts=after_ts, provider=prov.host))
        c.commit()

    assert ingest_reprice.reprice_stale() == 2

    with db.viz_conn() as c:
        rows = _rows(c)
    assert [row[0] for row in rows] == [1, 2]
    for line_num, cost, _version in rows:
        ts = before_ts if line_num == 1 else after_ts
        assert float(cost) == round(pricing.compute_cost(
            prov.model, **_SEED_INPUTS, ts=ts,
            adjustments=pricing.CostAdjustments(long_context=False),
            res=pricing.resolve(prov.model, ts, prov.host)), 6), \
            f"line {line_num} repriced by host"


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
    assert pricing.rate_for(prov.model, ts, prov.host) == _SCHED_RATES, (
        "the schedule must be the price in force at ts, or this test "
        "proves nothing")
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1,
              SeedRecord(model=prov.model, ts=ts, provider=prov.host))
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        cost, _version = _pair(
            c, "SELECT rp.cost_usd, pv.value "
               "FROM records r JOIN records_price rp ON rp.file_id = r.file_id AND rp.line_num = r.line_num JOIN files f ON f.file_id = r.file_id "
               "LEFT JOIN dim_pricing_version pv "
               "  ON pv.id = rp.pricing_version_id "
               "WHERE f.file_key = %s AND r.line_num = 1", (_FILE_KEY,))
    assert float(cost) == round(pricing.compute_cost(
        prov.model, **_SEED_INPUTS, ts=ts,
        adjustments=pricing.CostAdjustments(long_context=False),
        res=pricing.resolve(prov.model, ts, prov.host)), 6)


def test_reprice_rederives_long_context_for_the_meters_models(fresh_db):
    """for the meter's models the long-context flag is a pure
    function of stored columns — fresh + cache_creation + cache_read
    against the threshold — so the pass re-derives it beside the cost. A
    stale gpt-5.6-sol row whose tally sits above the 272k threshold flips
    from its stored FALSE — what the old subscription-exempt rule wrote —
    to TRUE with its metered cost; a claude row seeded alongside — a
    model whose card carries no meter — keeps its stored NULL and prices
    flat above the same threshold."""
    metered = round(pricing.compute_cost(
        "gpt-5.6-sol", fresh=280_000, output=0, eph5=0, eph1h=0,  # sv-test-data: allow (derived: expected priced from the same loaded tables as the reprice pass)
        unsplit_create=0, read=0, ts=_SEED_TS,
        adjustments=pricing.CostAdjustments(long_context=True)), 6)
    flat = round(pricing.compute_cost(
        _SEED_MODEL, fresh=280_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=_SEED_TS,
        adjustments=pricing.CostAdjustments(long_context=False)), 6)
    with db.viz_conn() as c:
        _seed_meter_row(c, 1, model="gpt-5.6-sol", fresh_tokens=280_000,
                        long_context=False)
        _seed_meter_row(c, 2, model=_SEED_MODEL, fresh_tokens=280_000)
        c.commit()

    assert ingest_reprice.reprice_stale() == 2

    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT r.line_num, rp.long_context, rp.cost_usd, pv.value "
            "FROM records r JOIN records_price rp ON rp.file_id = r.file_id AND rp.line_num = r.line_num JOIN files f ON f.file_id = r.file_id "
            "LEFT JOIN dim_pricing_version pv "
            "  ON pv.id = rp.pricing_version_id "
            "WHERE f.file_key = %s ORDER BY r.line_num",
            (_FILE_KEY,)).fetchall()
    codex, claude = rows
    assert codex[0] == 1 and claude[0] == 2
    assert codex[1] is True
    assert float(codex[2]) == metered
    assert codex[3] == constants.PRICING_VERSION
    assert claude[1] is None, "a no-meter model's stored flag is untouched"
    assert float(claude[2]) == flat


def test_reprice_keeps_a_nonmember_codex_rows_stored_flag(fresh_db, monkeypatch):
    """An id the meter does not list — the `unknown` label a model-less
    rollout stores, or any future id — keeps the flag parse wrote: the
    threshold-only parse-time decision stands, and the pass prices the
    stored flag instead of re-deriving. Membership is patched synthetic
    so the test stays independent of the committed data (SV-TEST-DATA)."""
    monkeypatch.setattr(pricing, "LONG_CONTEXT_MODELS", frozenset())
    flat = round(pricing.compute_cost(
        "unknown", fresh=280_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=_SEED_TS,
        adjustments=pricing.CostAdjustments(long_context=True)), 6)
    with db.viz_conn() as c:
        _seed_meter_row(c, 1, model="unknown", fresh_tokens=280_000,
                        long_context=True)
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        row = c.execute(
            "SELECT long_context, cost_usd FROM records r "
            "JOIN records_price rp ON rp.file_id = r.file_id AND rp.line_num = r.line_num JOIN files f ON f.file_id = r.file_id "
            "WHERE f.file_key = %s AND r.line_num = 1",
            (_FILE_KEY,)).fetchone()
    assert row is not None
    assert row[0] is True
    assert float(row[1]) == flat


def test_reprice_claims_a_pre_fold_null_row_of_a_member(fresh_db):
    """At DB level: the refresh learns a band
    and bumps PRICING_VERSION only; every pre-fold row of the newly
    learned member carries the parse-stored NULL flag (non-member
    marker) priced flat. The reprice re-derives member rows whatever the
    stored flag — so the row lands exactly what a reparse would store:
    the band decision and the metered cost."""
    metered = round(pricing.compute_cost(
        "gpt-6-sol", fresh=300_000, output=0, eph5=0, eph1h=0,  # sv-test-data: allow (derived: expected priced from the same loaded tables as the reprice pass)
        unsplit_create=0, read=0, ts=_SEED_TS,
        adjustments=pricing.CostAdjustments(long_context=True)), 6)
    with db.viz_conn() as c:
        _seed_meter_row(c, 1, model="gpt-6-sol", fresh_tokens=300_000)
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        row = c.execute(
            "SELECT rp.long_context, rp.cost_usd, pv.value "
            "FROM records r JOIN records_price rp ON rp.file_id = r.file_id AND rp.line_num = r.line_num JOIN files f ON f.file_id = r.file_id "
            "LEFT JOIN dim_pricing_version pv "
            "  ON pv.id = rp.pricing_version_id "
            "WHERE f.file_key = %s AND r.line_num = 1",
            (_FILE_KEY,)).fetchone()
    assert row is not None, "the seeded meter row must exist"
    flag, cost, version = row
    assert flag is True, (
        "the pre-fold NULL row of a learned member reprices to the band "
        "decision — reprice equals what a reparse stores")
    assert float(cost) == metered
    assert version == constants.PRICING_VERSION


def test_reprice_unbills_a_lapsed_members_stored_true(fresh_db):
    """ at DB level: a non-member row whose stored TRUE is the
    lapsed member era's — 250k tokens, under the global threshold, no
    parse path stores TRUE there — reprices to FALSE and the flat cost,
    what a reparse stores for every format."""
    flat = round(pricing.compute_cost(
        "claude-opus-4-7", fresh=250_000, output=0, eph5=0, eph1h=0,  # sv-test-data: allow (derived: expected priced from the same loaded tables as the reprice pass)
        unsplit_create=0, read=0, ts=_SEED_TS,
        adjustments=pricing.CostAdjustments(long_context=False)), 6)
    with db.viz_conn() as c:
        _seed_meter_row(c, 1, model="gpt-6-sol", fresh_tokens=250_000,
                        long_context=True)
        new_model = dims_seed.dim_id(c, "dim_model", "claude-opus-4-7")
        c.execute(
            "UPDATE records r SET model_id = %s "
            "  FROM files f "
            " WHERE r.file_id = f.file_id AND f.file_key = %s "
            "   AND r.line_num = 1", (new_model, _FILE_KEY))
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        row = c.execute(
            "SELECT rp.long_context, rp.cost_usd, pv.value "
            "FROM records r JOIN records_price rp ON rp.file_id = r.file_id AND rp.line_num = r.line_num JOIN files f ON f.file_id = r.file_id "
            "LEFT JOIN dim_pricing_version pv "
            "  ON pv.id = rp.pricing_version_id "
            "WHERE f.file_key = %s AND r.line_num = 1",
            (_FILE_KEY,)).fetchone()
    assert row is not None, "the seeded lapsed row must exist"
    flag, cost, version = row
    assert flag is False, (
        "the lapsed member's stored TRUE unbills: the reprice re-derives "
        "the Codex threshold test for a non-member's stored TRUE")
    assert float(cost) == flat
    assert version == constants.PRICING_VERSION


def test_reprice_derives_a_provider_rows_flag_too(fresh_db):
    """The meter decision ignores the provider because the parse's does:
    a provider-tagged member row re-derives whatever its stored flag —
    a stored FALSE above the threshold moves to TRUE and the metered
    cost, what a reparse of the same record stores."""
    with db.viz_conn() as c:
        _seed_meter_row(c, 1, model="gpt-5.6-sol", fresh_tokens=280_000,
                        provider="OpenRouter", long_context=False)
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        row = c.execute(
            "SELECT rp.long_context, rp.cost_usd, pv.value "
            "FROM records r JOIN records_price rp ON rp.file_id = r.file_id AND rp.line_num = r.line_num JOIN files f ON f.file_id = r.file_id "
            "LEFT JOIN dim_pricing_version pv "
            "  ON pv.id = rp.pricing_version_id "
            "WHERE (f.file_key, r.line_num) = (%s, 1)",
            (_FILE_KEY,)).fetchone()
    assert row is not None, "the seeded provider row must exist"
    flag, cost, version = row
    assert flag is True
    assert float(cost) == round(pricing.compute_cost(
        "gpt-5.6-sol", fresh=280_000, output=0, eph5=0, eph1h=0,  # sv-test-data: allow (derived: expected priced from the same loaded tables as the reprice pass)
        unsplit_create=0, read=0, ts=_SEED_TS,
        adjustments=pricing.CostAdjustments(long_context=True),
        res=pricing.resolve("gpt-5.6-sol", _SEED_TS, "OpenRouter")), 6)  # sv-test-data: allow (derived: expected priced from the same loaded tables as the reprice pass)
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
              "rebuild_agent_rollup", "rebuild_web_metrics_rollup")
    for name in phases:
        monkeypatch.setattr(ingest, name, stub(name))
    ingest._rebuild_derived_state()  # pylint: disable=protected-access

    assert set(order) == set(phases), "every phase ran exactly once"
    assert order[:4] == ["purge_suppressed", "reprice_stale",
                         "rekey_folded_projects", "recompute_canonical"]
