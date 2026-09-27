"""cost_buckets is a decomposition of cost_total, so it must be computed
per rate epoch. With dated rates, re-deriving one rate for a range that
straddles a cutover makes the buckets disagree with the authoritative
SUM(cost_usd) they claim to decompose.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from backend import pricing
from backend.api_cache import _cache_canon_source
from backend.api_common import (
    epoch_ts, fold_per_model, fold_per_model_provider, rate_epoch_sql,
)
from backend.db import sql_text
from tests import scratch_db

UTC = timezone.utc


def _row(model, epoch, fresh=0, cc=0, cr=0, output=0, eph5=0, eph1h=0,
         cost=0.0, long_context=False):
    # (model, provider, rate_epoch, long_context, turns, fresh,
    #  cache_create, cache_read, output, eph5, eph1h, cost_total)
    return (model, None, epoch, long_context, 1, fresh, cc, cr, output,
            eph5, eph1h, cost)


def test_buckets_sum_to_total_within_a_single_epoch():
    # The stored total is what the tables price the tokens at, at the
    # fold's own representative instant, so both sides move together.
    ts = epoch_ts(0)
    stored = pricing.compute_cost(
        "claude-opus-4-8", fresh=1_000_000, output=0, eph5=0, eph1h=0,  # sv-test-data: allow (derived: stored read from the same tables the fold prices at)
        unsplit_create=0, read=0, ts=ts,
    )
    rows = [_row("claude-opus-4-8", 0, fresh=1_000_000, cost=stored)]
    out = fold_per_model(rows, pair_bounds={})
    assert len(out) == 1
    m = out[0]
    # cost_total is the fold's own rounding of this stored cost to 4
    # decimals, so re-derive it rather than budget the rounding.
    assert m["cost_total"] == pytest.approx(round(stored, 4), abs=1e-6)
    assert sum(m["cost_buckets"].values()) == pytest.approx(m["cost_total"])


def test_buckets_sum_to_total_across_a_dated_rate_cutover(monkeypatch):
    # 1M input tokens in the promotional window and 1M after it, each
    # stored at that span's own fresh rate; the stored cost_total is
    # authoritative and the buckets must reconcile to it.
    model = "acme/cost-cutover-9"
    cutover = datetime(2030, 1, 1, tzinfo=UTC)
    before = {"fresh": 9.0, "create_5m": 11.25, "create_1h": 18.0,
              "read": 0.9, "output": 45.0}
    after = {"fresh": 0.06974999999999999, "create_5m": 0.1,
             "create_1h": 0.14, "read": 0.01, "output": 0.5}
    monkeypatch.setitem(pricing.MODEL_RATES, model, after)
    monkeypatch.setattr(pricing, "DATED_RATES", {model: [(cutover, before)]})
    monkeypatch.setattr(pricing, "RATE_EPOCHS", [cutover])
    rows = [
        _row(model, 0, fresh=1_000_000, cost=before["fresh"]),
        _row(model, 1, fresh=1_000_000, cost=after["fresh"]),
    ]
    total = w.before["fresh"] + w.after["fresh"]
    out = fold_per_model(
        rows, pair_bounds={(w.model, ""): [w.cutover]})
    assert len(out) == 1, "epochs must fold into one row per model"
    m = out[0]
    assert m["fresh"] == 2_000_000
    assert m["turns"] == 2
    # SV-TEST-DATA rounded-compare rule: cost_total sums the same stored
    # costs in the same order, so this rounding comparison stays exact.
    assert m["cost_total"] == pytest.approx(round(total, 4), abs=1e-6)
    # The five recomputed buckets round independently, so their sum may
    # differ from the unrounded stored total by up to 2.5e-4.
    assert abs(sum(m["cost_buckets"].values()) - total) <= 2.5e-4
    # SV-TEST-DATA rounded-compare rule: token-price math and stored-total
    # summation differ; allow one 4-place unit plus float comparison noise.
    assert m["cost_buckets"]["fresh"] == pytest.approx(
        round(total, 4), abs=1e-4 + 1e-12)


def test_null_timestamp_provider_fold_uses_provider_list_price(monkeypatch):
    """A null timestamp uses the provider row's default on both sides."""
    model, host = "claude-sonnet-99", "SyntheticHost"
    model_rates = {
        "fresh": 0.40, "create_5m": 0.50, "create_1h": 0.80,
        "read": 0.04, "output": 2.00,
    }
    provider_rates = {
        "fresh": 8.25, "create_5m": 10.00, "create_1h": 16.50,
        "read": 0.825, "output": 41.25,
    }
    synthetic_boundary = datetime(2032, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(pricing, "RATE_EPOCHS", [synthetic_boundary])
    first_epoch_ts = epoch_ts(0)
    provider_start = synthetic_boundary + timedelta(seconds=1)
    monkeypatch.setattr(pricing, "MODEL_RATES", {model: model_rates})
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {(model, host): provider_rates})
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {})
    monkeypatch.setattr(pricing, "PROVIDER_STARTS", {(model, host): provider_start})
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {})

    assert first_epoch_ts is not None
    assert provider_start > first_epoch_ts
    stored = pricing.compute_cost(
        model, fresh=1_000_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=None, provider=host,
    )
    assert stored == provider_rates["fresh"]
    row = (model, host, -1, False, 1, 1_000_000, 0, 0, 0, 0, 0, stored)

    folded = fold_per_model_provider(
        [row], pair_bounds={(model, host): []})[0]
    bucket_total = sum(folded["cost_buckets"].values())
    assert folded["cost_total"] == pytest.approx(stored)
    assert bucket_total == pytest.approx(stored)

    resolved = pricing.resolve(model, None, host)
    assert resolved.rates["fresh"] == provider_rates["fresh"]
    resolved_cost = 1_000_000 * resolved.rates["fresh"] / 1_000_000
    assert bucket_total == pytest.approx(resolved_cost)


@pytest.mark.db
def test_cache_range_includes_null_timestamp_in_list_epoch(monkeypatch):
    """The cache source admits null-ts records, which fold in epoch -1."""
    boundary = datetime(2031, 1, 1, tzinfo=UTC)
    since = datetime(2030, 12, 1, tzinfo=UTC)
    monkeypatch.setattr(pricing, "RATE_EPOCHS", [boundary])
    epoch_expr, epoch_params = rate_epoch_sql("ts")
    canon_src, canon_args = _cache_canon_source(None, None, since)
    canon_src = canon_src.replace(
        "FROM records", "FROM records LEFT JOIN rate_bounds ON FALSE", 1)

    with scratch_db.admin_connection() as conn:
        got = conn.execute(
            sql_text(
                "WITH rate_bounds AS (SELECT NULL::text AS pair_model, "
                "NULL::timestamptz[] AS boundaries WHERE FALSE), "
                "records(ts, is_canonical) AS (VALUES "
                "(NULL::timestamptz, TRUE), "
                "('2030-12-31T00:00:00+00:00'::timestamptz, TRUE)) "
                f"SELECT ({epoch_expr}) AS rate_epoch, COUNT(*) "
                f"{canon_src} GROUP BY rate_epoch ORDER BY rate_epoch"
            ),
            epoch_params + canon_args,
        ).fetchall()

    assert got == [(-1, 1), (0, 1)]


def test_epoch_index_selects_the_rate_in_force_for_that_window(synthetic_dated_rate):
    w = synthetic_dated_rate
    assert pricing.rate_for(w.model, epoch_ts(0))["fresh"] == w.before["fresh"]
    assert pricing.rate_for(w.model, epoch_ts(1))["fresh"] == w.after["fresh"]


@pytest.mark.db
def test_epoch_sql_places_null_at_list_epoch(monkeypatch):
    boundary = datetime(2031, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(pricing, "RATE_EPOCHS", [boundary])
    expr, params = rate_epoch_sql("ts")
    tick = timedelta(microseconds=1)
    probes = [None, boundary - tick, boundary + tick]
    with scratch_db.admin_connection() as conn:
        got = conn.execute(
            sql_text(
                "WITH rate_bounds AS (SELECT NULL::text AS pair_model, "
                "NULL::timestamptz[] AS boundaries WHERE FALSE) "
                f"SELECT {expr} FROM unnest(%s::timestamptz[]) AS v(ts) "
                "LEFT JOIN rate_bounds ON FALSE"),
            params + [probes],
        ).fetchall()
    assert [row[0] for row in got] == [-1, 0, 1]


def test_epoch_ts_negative_index_is_list_price_epoch(synthetic_dated_rate):
    assert epoch_ts(-1) is None
    assert epoch_ts(0) == synthetic_dated_rate.cutover - timedelta(microseconds=1)


def test_epoch_sql_expression_has_one_case_per_boundary(synthetic_dated_rate):
    expr, params = rate_epoch_sql("ts")
    # The only parameter is the global fallback array; pair boundaries are
    # supplied by the query's typed text-array inputs.
    assert expr.count("CASE") == 1
    assert "width_bucket(ts, rate_bounds.boundaries)" in expr
    assert "width_bucket(ts, %s::timestamptz[])" in expr
    assert params == [pricing.RATE_EPOCHS]


def test_epoch_sql_uses_empty_global_fallback_when_no_rates_are_dated(
        monkeypatch):
    # With no global boundaries, a missing pair uses width_bucket over an
    # empty typed array and lands in epoch 0.
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    monkeypatch.setattr(pricing, "RATE_EPOCHS", [])
    expr, params = rate_epoch_sql("ts")
    assert "width_bucket(ts, %s::timestamptz[])" in expr
    assert params == [[]]
    assert epoch_ts(0) is None, "no epochs => price at list, not a window"


def test_epoch_sql_binds_the_global_fallback_array():
    """The fallback array is derived from all file and provider windows."""
    expr, params = rate_epoch_sql("ts")
    expected = sorted(
        {end for w in pricing.DATED_RATES.values() for end, _ in w}
        | {end for w in pricing.PROVIDER_DATED_RATES.values() for end, _ in w}
        | set(pricing.PROVIDER_STARTS.values()))
    assert params == [expected]
    assert "width_bucket(ts, %s::timestamptz[])" in expr


@pytest.mark.db
def test_epoch_sql_places_every_timestamp_with_200_global_fallbacks(
        monkeypatch):
    """The global fallback array maps timestamp edges with width_bucket."""
    epochs = [datetime(2031, 1, 1, tzinfo=UTC) + timedelta(hours=7 * i)
              for i in range(200)]
    monkeypatch.setattr(pricing, "RATE_EPOCHS", epochs)
    expr, params = rate_epoch_sql("ts")
    assert expr.count("CASE") == 1 and params == [epochs]
    tick = timedelta(microseconds=1)
    probes = [(None, -1)]
    probes += [
        (e + d, i + (d >= timedelta(0)))
        for i, e in enumerate(epochs)
        for d in (-tick, timedelta(0), tick)
    ]
    probes += [(epoch_ts(i), i) for i in range(201)]
    with scratch_db.admin_connection() as conn:
        got = conn.execute(
            sql_text(
                "WITH rate_bounds AS (SELECT NULL::text AS pair_model, "
                "NULL::timestamptz[] AS boundaries WHERE FALSE) "
                f"SELECT {expr} FROM unnest(%s::timestamptz[]) "
                "WITH ORDINALITY AS v(ts, n) "
                "LEFT JOIN rate_bounds ON FALSE ORDER BY n"),
            params + [[ts for ts, _ in probes]]).fetchall()
    assert [row[0] for row in got] == [want for _, want in probes]


def test_an_undeclared_ttl_lands_in_the_1h_bucket(synthetic_dated_rate):
    """/api/cache decomposes the stored cost into per-component buckets.
    A write with no declared TTL is stored at the 1h rate (pricing.
    compute_cost), so the fold must put it in the 1h bucket — in the 5m
    bucket the parts would no longer sum to the stored total."""
    # The row's stored cost prices at the epoch's own representative
    # instant — the same one the fold re-derives at — so stored and
    # re-derived agree whatever the table's history holds. Driven on the
    # fixture's synthetic window (SV-TEST-DATA): the expected bucket is
    # the window's own create_1h rate, so no live row can move it.
    w = synthetic_dated_rate
    ts = epoch_ts(0)
    stored = pricing.compute_cost(
        w.model, fresh=0, output=0, eph5=0, eph1h=0,
        unsplit_create=1_000_000, read=0, ts=ts,
    )
    rows = [_row(w.model, 0, cc=1_000_000, cost=stored)]
    m = fold_per_model(
        rows, pair_bounds={(w.model, ""): [w.cutover]})[0]
    assert m["cost_buckets"]["create_1h"] == pytest.approx(
        round(w.before["create_1h"], 4), abs=1e-6)
    assert m["cost_buckets"]["create_5m"] == pytest.approx(0.0)
    assert sum(m["cost_buckets"].values()) == pytest.approx(m["cost_total"])


def test_long_context_buckets_reconcile_with_the_stored_total(synthetic_dated_rate):
    """A Codex record above the 272k threshold is STORED at
    2x input side / 1.5x output (pricing.compute_cost's long_context). A
    fold row that forgets the flag prices the same tokens at the flat
    rate and its buckets stop summing to the stored total — the exact drift
    SV-DATED-RATES bans. The flag is part of the aggregate row, alongside
    the rate epoch; the record's ts pins the epoch's rates on both sides
    so only the meter can move the numbers. Driven on the fixture's
    synthetic window: the meter is meter-agnostic, and the expected
    buckets read the window's own rates, so no live row can move them."""
    w = synthetic_dated_rate
    ts = epoch_ts(0)
    rates = w.before
    stored = pricing.compute_cost(
        w.model, fresh=300_000, output=2_000, eph5=0, eph1h=0,
        unsplit_create=0, read=0, long_context=True, ts=ts,
    )
    flat = pricing.compute_cost(
        w.model, fresh=300_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=ts,
    )
    assert stored > flat, "the meter must actually move the total here"

    m = fold_per_model([
        _row(w.model, 0, fresh=300_000, output=2_000,
             cost=stored, long_context=True),
    ], pair_bounds={(w.model, ""): [w.cutover]})[0]
    assert m["cost_total"] == pytest.approx(round(stored, 4), abs=1e-6)
    # Valued buckets + the total, each rounded to 4 decimals
    # independently: the safe-for-any-data bound on their disagreement
    # is 6 * 5e-5 = 3e-4.
    assert abs(sum(m["cost_buckets"].values()) - m["cost_total"]) <= 3e-4
    assert m["cost_buckets"]["fresh"] == pytest.approx(
        round(300_000 * rates["fresh"] * pricing.LONG_CONTEXT_INPUT_MULT / 1_000_000, 4),
        abs=1e-6)
    assert m["cost_buckets"]["output"] == pytest.approx(
        round(2_000 * rates["output"] * pricing.LONG_CONTEXT_OUTPUT_MULT / 1_000_000, 4),
        abs=1e-6)


def test_long_context_and_flat_rows_of_one_model_fold_into_one_entry(
        synthetic_dated_rate):
    """The flag splits the AGGREGATE row, never the model entry: both
    rows' tokens and turns land on the same model, priced each by its
    own meter. Driven on the fixture's synthetic window (SV-TEST-DATA):
    the meter is meter-agnostic, and both stored sides price the
    fixture's own rates, so no live row can move either number."""
    w = synthetic_dated_rate
    ts = epoch_ts(0)
    stored_lc = pricing.compute_cost(
        w.model, fresh=300_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, long_context=True, ts=ts,
    )
    stored_flat = pricing.compute_cost(
        w.model, fresh=100_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=ts,
    )
    out = fold_per_model([
        _row(w.model, 0, fresh=300_000, cost=stored_lc,
             long_context=True),
        _row(w.model, 0, fresh=100_000, cost=stored_flat),
    ], pair_bounds={(w.model, ""): [w.cutover]})
    assert len(out) == 1
    m = out[0]
    assert m["turns"] == 2
    assert m["fresh"] == 400_000
    assert m["cost_total"] == pytest.approx(
        round(stored_lc + stored_flat, 4), abs=1e-6)
    # Five rounded buckets against the rounded total: the safe-for-any-
    # data bound on their disagreement is 6 * 5e-5 = 3e-4.
    assert abs(sum(m["cost_buckets"].values()) - m["cost_total"]) <= 3e-4
    assert m["cost_buckets"]["fresh"] == pytest.approx(
        round(stored_lc + stored_flat, 4), abs=1e-6)


# api_common._fold rounds cost_total and every bucket to 4 decimal
# places. A rounded bucket sits within 0.5 * 10**-4 of its true value,
# and the five buckets — scaled to sum to the stored total before
# rounding — within 5 * 5e-5 = 2.5e-4 of it. The bound derives from the
# fold's own rounding, never from the rates the file happens to hold.
_FOLD_DP = 4
_PER_BUCKET_TOL = 0.5 * 10 ** -_FOLD_DP
_SUM_TOL = len(pricing.RATE_FIELDS) * _PER_BUCKET_TOL

# The made-up row the scheduled-fold tests price against (the
# tests/conftest.py fixtures' convention: rates deliberately unlike any
# real price, so a test asserting against them can never be mistaken for
# a pricing fact). Installed in memory through the real loader — the
# file is never written — so a refresh's moved prices and the perturbed
# leg's doubled rows reach none of the test's numbers (SV-TEST-DATA).
_SYNTHETIC_MODEL = "acme/acme-9"
_SYNTHETIC_HOST = "HostCo"
LIST_RATES = {"fresh": 2.60, "create_5m": 3.25, "create_1h": 5.20,
              "read": 0.26, "output": 13.00}
# Exactly half of the list rates — by construction, not by coincidence
# with a live entry — so the uniform-scaling param's split stays exact
# whatever the file holds. Halving is exact in binary floating point.
HALF = {field: value / 2 for field, value in LIST_RATES.items()}


def _install_scheduled_row(monkeypatch, schedule):
    """Give a made-up provider row a weekly schedule, through the real loader.

    Priced against the live Novita entry, the verdict moved with every
    refresh: the 2026-09-26T04:54:05Z Novita move turned the default
    1e-6 relative tolerance red on the perturbed leg (issue #218). A
    synthetic row makes the verdict independent of which rates the file
    holds; schedules live on provider rows, so the synthetic row is one.
    """
    doc = json.loads(pricing.PRICING_JSON.read_text(encoding="utf-8"))  # sv-test-data: allow (loads the live document only to inject the synthetic provider row; no live value reaches a verdict)
    doc["providers"][_SYNTHETIC_MODEL] = {_SYNTHETIC_HOST: [
        {"from": None, **LIST_RATES, "schedule": schedule},
    ]}
    for name, value in pricing.load_tables(doc).items():
        monkeypatch.setattr(pricing, name, value)


@pytest.mark.parametrize("schedule", [
    pytest.param([{"start": 1400, "end": 0, "rates": HALF}], id="uniform-scaling"),
    pytest.param([{"start": 1400, "end": 0, "rates": {**HALF, "output": 0.9}}],
                 id="uneven"),
])
def test_a_scheduled_rows_buckets_sum_to_its_stored_total(monkeypatch, schedule):
    """A fold row is summed over records priced in different windows, and
    re-derives at one representative time. A scheduled row's buckets take
    their split from those rates and are scaled to the row's stored total:
    the total is always exact, and the split is exact whenever every window
    scales all five rates alike — HALF is LIST_RATES / 2 by construction."""
    _install_scheduled_row(monkeypatch, schedule)
    model, host = _SYNTHETIC_MODEL, _SYNTHETIC_HOST
    peak = datetime(2031, 1, 6, 9, tzinfo=UTC)
    off_peak = datetime(2031, 1, 6, 20, tzinfo=UTC)
    tokens = {"fresh": 1_000_000, "output": 500_000, "read": 2_000_000}
    stored = sum(pricing.compute_cost(model, fresh=tokens["fresh"], output=tokens["output"],
                                      eph5=0, eph1h=0, unsplit_create=0,
                                      read=tokens["read"], ts=ts, provider=host)
                 for ts in (peak, off_peak))
    row = (model, host, 0, False, 2, 2 * tokens["fresh"], 0,
           2 * tokens["read"], 2 * tokens["output"], 0, 0, stored)
    got = fold_per_model(
        [row], pair_bounds={(model, host): []})[0]["cost_buckets"]
    assert sum(got.values()) == pytest.approx(stored, abs=_SUM_TOL)
    if schedule[0]["rates"] == HALF:
        want = {f: sum(pricing.rate_for(model, ts, host)[r] * tokens[t] / 1e6
                       for ts in (peak, off_peak))
                for f, r, t in (("fresh", "fresh", "fresh"), ("read", "read", "read"),
                                ("output", "output", "output"))}
        for field, value in want.items():
            assert got[field] == pytest.approx(value, abs=_PER_BUCKET_TOL)


def test_a_schedule_adds_no_rate_epoch(monkeypatch):
    """Time-of-day windows repeat every week; they are not epochs, and the
    epoch list is exactly what it was without them. The row is synthetic
    (_install_scheduled_row): it begins at no instant and carries no dated
    window, so it adds no boundary of its own either."""
    before = list(pricing.RATE_EPOCHS)
    _install_scheduled_row(monkeypatch, [{"days": ["saturday"], "rates": HALF}])
    assert pricing.RATE_EPOCHS == before
