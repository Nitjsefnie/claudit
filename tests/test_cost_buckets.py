"""cost_buckets is a decomposition of cost_total, so it must be computed
per rate epoch. With dated rates, re-deriving one rate for a range that
straddles a cutover makes the buckets disagree with the authoritative
SUM(cost_usd) they claim to decompose.
"""
from datetime import datetime, timezone

import pytest

from backend import pricing
from backend.api_common import epoch_ts, fold_per_model, rate_epoch_sql

UTC = timezone.utc


def _row(model, epoch, fresh=0, cc=0, cr=0, output=0, eph5=0, eph1h=0,
         cost=0.0, long_context=False):
    # (model, provider, rate_epoch, long_context, turns, fresh,
    #  cache_create, cache_read, output, eph5, eph1h, cost_total)
    return (model, None, epoch, long_context, 1, fresh, cc, cr, output,
            eph5, eph1h, cost)


def test_buckets_sum_to_total_within_a_single_epoch():
    rows = [_row("claude-opus-4-8", 0, fresh=1_000_000, cost=5.00)]
    out = fold_per_model(rows)
    assert len(out) == 1
    m = out[0]
    assert m["cost_total"] == pytest.approx(5.00)
    assert sum(m["cost_buckets"].values()) == pytest.approx(m["cost_total"])


def test_buckets_sum_to_total_across_a_dated_rate_cutover(synthetic_dated_rate):
    # 1M input tokens in the promotional window ($9.00) and 1M after it
    # ($2.00). Stored cost_total is authoritative at $11.00 total.
    w = synthetic_dated_rate
    rows = [
        _row(w.model, 0, fresh=1_000_000, cost=9.00),
        _row(w.model, 1, fresh=1_000_000, cost=2.00),
    ]
    out = fold_per_model(rows)
    assert len(out) == 1, "epochs must fold into one row per model"
    m = out[0]
    assert m["fresh"] == 2_000_000
    assert m["turns"] == 2
    assert m["cost_total"] == pytest.approx(11.00)
    assert sum(m["cost_buckets"].values()) == pytest.approx(11.00)
    assert m["cost_buckets"]["fresh"] == pytest.approx(11.00)


def test_epoch_index_selects_the_rate_in_force_for_that_window(synthetic_dated_rate):
    w = synthetic_dated_rate
    assert pricing.rate_for(w.model, epoch_ts(0))["fresh"] == w.before["fresh"]
    assert pricing.rate_for(w.model, epoch_ts(1))["fresh"] == w.after["fresh"]


def test_epoch_sql_expression_has_one_case_per_boundary(synthetic_dated_rate):
    expr, params = rate_epoch_sql("ts")
    # Boundaries are BOUND, never interpolated into the SQL string.
    assert params == [synthetic_dated_rate.cutover]
    assert expr.count("CASE") == 1


def test_epoch_sql_collapses_to_a_constant_when_no_rates_are_dated(monkeypatch):
    # Patched empty table (the live table now carries the GLM-5.3-Flash
    # promotion): every row must land in epoch 0, with no parameters bound
    # and no CASE emitted.
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    monkeypatch.setattr(pricing, "RATE_EPOCHS", [])
    expr, params = rate_epoch_sql("ts")
    assert (expr, params) == ("0", [])
    assert epoch_ts(0) is None, "no epochs => price at list, not a window"


def test_epoch_sql_binds_every_live_rate_boundary():
    # The live windows: the GLM-5.3-Flash promotion cutover plus the two
    # GPT-5.6 repricings ported from codexmeter. Each boundary is bound as
    # a parameter, one CASE per boundary.
    expr, params = rate_epoch_sql("ts")
    assert params == [
        datetime(2026, 7, 30, 18, 12, tzinfo=UTC),   # GPT-5.6 JUL30_CUT
        datetime(2026, 8, 21, 19, 40, tzinfo=UTC),   # GPT-5.6 AUG21_CUT
        datetime(2026, 9, 9, 16, 0, tzinfo=UTC),     # GLM promo cutover
    ]
    assert expr.count("CASE") == 3


def test_an_undeclared_ttl_lands_in_the_1h_bucket():
    """/api/cache decomposes the stored cost into per-component buckets.
    A write with no declared TTL is stored at the 1h rate (pricing.
    compute_cost), so the fold must put it in the 1h bucket — in the 5m
    bucket the parts would no longer sum to the stored total."""
    stored = pricing.compute_cost(
        "claude-sonnet-4-5", fresh=0, output=0, eph5=0, eph1h=0,
        unsplit_create=1_000_000, read=0,
    )
    rows = [_row("claude-sonnet-4-5", 0, cc=1_000_000, cost=stored)]
    m = fold_per_model(rows)[0]
    assert m["cost_buckets"]["create_1h"] == pytest.approx(6.00)
    assert m["cost_buckets"]["create_5m"] == pytest.approx(0.0)
    assert sum(m["cost_buckets"].values()) == pytest.approx(m["cost_total"])


def test_long_context_buckets_reconcile_with_the_stored_total():
    """A pay-as-you-go Codex record above the 272k threshold is STORED at
    2x input side / 1.5x output (pricing.compute_cost's long_context). A
    fold row that forgets the flag prices the same tokens at the flat
    rate and its buckets stop summing to cost_total — the exact drift
    SV-DATED-RATES bans. The flag is part of the aggregate row, alongside
    the rate epoch; the record's ts pins the epoch's rates on both sides
    so only the meter can move the numbers."""
    ts = epoch_ts(0)
    rates = pricing.rate_for("gpt-5-6-sol", ts)
    stored = pricing.compute_cost(
        "gpt-5-6-sol", fresh=300_000, output=2_000, eph5=0, eph1h=0,
        unsplit_create=0, read=0, long_context=True, ts=ts,
    )
    flat = pricing.compute_cost(
        "gpt-5-6-sol", fresh=300_000, output=2_000, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=ts,
    )
    assert stored > flat, "the meter must actually move the total here"

    m = fold_per_model([
        _row("gpt-5-6-sol", 0, fresh=300_000, output=2_000,
             cost=stored, long_context=True),
    ])[0]
    assert m["cost_total"] == pytest.approx(stored)
    assert sum(m["cost_buckets"].values()) == pytest.approx(m["cost_total"])
    assert m["cost_buckets"]["fresh"] == pytest.approx(
        300_000 * rates["fresh"] * pricing.LONG_CONTEXT_INPUT_MULT / 1_000_000
    )
    assert m["cost_buckets"]["output"] == pytest.approx(
        2_000 * rates["output"] * pricing.LONG_CONTEXT_OUTPUT_MULT / 1_000_000
    )


def test_long_context_and_flat_rows_of_one_model_fold_into_one_entry():
    """The flag splits the AGGREGATE row, never the model entry: both
    rows' tokens and turns land on the same model, priced each by its
    own meter."""
    ts = epoch_ts(0)
    stored_lc = pricing.compute_cost(
        "gpt-5-6-sol", fresh=300_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, long_context=True, ts=ts,
    )
    stored_flat = pricing.compute_cost(
        "gpt-5-6-sol", fresh=100_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=ts,
    )
    out = fold_per_model([
        _row("gpt-5-6-sol", 0, fresh=300_000, cost=stored_lc,
             long_context=True),
        _row("gpt-5-6-sol", 0, fresh=100_000, cost=stored_flat),
    ])
    assert len(out) == 1
    m = out[0]
    assert m["turns"] == 2
    assert m["fresh"] == 400_000
    assert m["cost_total"] == pytest.approx(stored_lc + stored_flat)
    assert sum(m["cost_buckets"].values()) == pytest.approx(m["cost_total"])
    assert m["cost_buckets"]["fresh"] == pytest.approx(stored_lc + stored_flat)
