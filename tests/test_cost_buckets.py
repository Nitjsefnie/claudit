"""cost_buckets is a decomposition of cost_total, so it must be computed
per rate epoch. With dated rates, re-deriving one rate for a range that
straddles a cutover makes the buckets disagree with the authoritative
SUM(cost_usd) they claim to decompose.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from backend import pricing
from backend.api_common import epoch_ts, fold_per_model, rate_epoch_sql
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
    rows = [_row("claude-opus-4-8", 0, fresh=1_000_000, cost=5.00)]
    out = fold_per_model(rows)
    assert len(out) == 1
    m = out[0]
    assert m["cost_total"] == pytest.approx(5.00)
    assert sum(m["cost_buckets"].values()) == pytest.approx(m["cost_total"])


def test_buckets_sum_to_total_across_a_dated_rate_cutover(synthetic_dated_rate):
    # 1M input tokens in the promotional window and 1M after it, each
    # stored at that span's own fresh rate; the stored cost_total is
    # authoritative and the buckets must reconcile to it.
    w = synthetic_dated_rate
    rows = [
        _row(w.model, 0, fresh=1_000_000, cost=w.before["fresh"]),
        _row(w.model, 1, fresh=1_000_000, cost=w.after["fresh"]),
    ]
    total = w.before["fresh"] + w.after["fresh"]
    out = fold_per_model(rows)
    assert len(out) == 1, "epochs must fold into one row per model"
    m = out[0]
    assert m["fresh"] == 2_000_000
    assert m["turns"] == 2
    assert m["cost_total"] == pytest.approx(total)
    assert sum(m["cost_buckets"].values()) == pytest.approx(total)
    assert m["cost_buckets"]["fresh"] == pytest.approx(total)


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
    """Each boundary the file carries is bound as a parameter, one CASE per
    boundary. Derived from the file, provider windows and row starts
    included, because the scheduled refresh appends boundaries: a literal
    list here would fail the first commit it makes."""
    expr, params = rate_epoch_sql("ts")
    assert params == sorted(
        {end for w in pricing.DATED_RATES.values() for end, _ in w}
        | {end for w in pricing.PROVIDER_DATED_RATES.values() for end, _ in w}
        | set(pricing.PROVIDER_STARTS.values()))
    assert expr.count("CASE") == len(params)
    # The model rows' own boundaries, which no refresh touches, are there.
    assert {
        datetime(2026, 7, 30, 18, 12, tzinfo=UTC),   # GPT-5.6 JUL30_CUT
        datetime(2026, 8, 21, 19, 40, tzinfo=UTC),   # GPT-5.6 AUG21_CUT
        datetime(2026, 9, 9, 16, 0, tzinfo=UTC),     # GLM promo cutover
    } <= set(params)


@pytest.mark.db
def test_epoch_sql_places_every_timestamp_with_200_epochs(monkeypatch):
    """Every appended rate entry adds an epoch (SV-RATE-REFRESH), so the
    expression grows with the table. Correctness, not speed: with 200
    boundaries, Postgres puts a timestamp one microsecond before, at and
    after each boundary in the epoch this module says it is in, and the
    lookup timestamp epoch_ts(i) of every epoch lands in epoch i."""
    epochs = [datetime(2031, 1, 1, tzinfo=UTC) + timedelta(hours=7 * i)
              for i in range(200)]
    monkeypatch.setattr(pricing, "RATE_EPOCHS", epochs)
    expr, params = rate_epoch_sql("ts")
    assert expr.count("CASE") == 200 and params == epochs
    tick = timedelta(microseconds=1)
    probes = [(e + d, i + (d >= timedelta(0)))
              for i, e in enumerate(epochs) for d in (-tick, timedelta(0), tick)]
    probes += [(epoch_ts(i), i) for i in range(201)]
    with scratch_db.admin_connection() as conn:
        got = conn.execute(
            sql_text(f"SELECT {expr} FROM unnest(%s::timestamptz[]) WITH ORDINALITY"
                     " AS v(ts, n) ORDER BY n"),
            [*params, [ts for ts, _ in probes]]).fetchall()
    assert [row[0] for row in got] == [want for _, want in probes]


def test_an_undeclared_ttl_lands_in_the_1h_bucket():
    """/api/cache decomposes the stored cost into per-component buckets.
    A write with no declared TTL is stored at the 1h rate (pricing.
    compute_cost), so the fold must put it in the 1h bucket — in the 5m
    bucket the parts would no longer sum to the stored total."""
    # The row's stored cost prices at the epoch's own representative
    # instant — the same one the fold re-derives at — so stored and
    # re-derived agree whatever the table's history holds.
    ts = epoch_ts(0)
    stored = pricing.compute_cost(
        "claude-sonnet-4-5", fresh=0, output=0, eph5=0, eph1h=0,
        unsplit_create=1_000_000, read=0, ts=ts,
    )
    rows = [_row("claude-sonnet-4-5", 0, cc=1_000_000, cost=stored)]
    m = fold_per_model(rows)[0]
    expected = pricing.rate_for("claude-sonnet-4-5", ts)["create_1h"]
    assert m["cost_buckets"]["create_1h"] == pytest.approx(expected)
    assert m["cost_buckets"]["create_5m"] == pytest.approx(0.0)
    assert sum(m["cost_buckets"].values()) == pytest.approx(m["cost_total"])


def test_long_context_buckets_reconcile_with_the_stored_total():
    """A Codex record above the 272k threshold is STORED at
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


def _schedule_novita(monkeypatch, schedule):
    """Give GLM's Novita row a weekly schedule, through the real loader."""
    doc = json.loads(pricing.PRICING_JSON.read_text(encoding="utf-8"))
    doc["providers"]["z-ai/glm-5-3-flash"]["Novita"][-1]["schedule"] = schedule
    for name, value in pricing.load_tables(doc).items():
        monkeypatch.setattr(pricing, name, value)


HALF = {"fresh": 0.066, "create_5m": 0.066, "create_1h": 0.066,
        "read": 0.0132, "output": 0.22}


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
    scales all five rates alike."""
    _schedule_novita(monkeypatch, schedule)
    model, host = "z-ai/glm-5.3-flash", "Novita"
    peak = datetime(2031, 1, 6, 9, tzinfo=UTC)
    off_peak = datetime(2031, 1, 6, 20, tzinfo=UTC)
    tokens = {"fresh": 1_000_000, "output": 500_000, "read": 2_000_000}
    stored = sum(pricing.compute_cost(model, fresh=tokens["fresh"], output=tokens["output"],
                                      eph5=0, eph1h=0, unsplit_create=0,
                                      read=tokens["read"], ts=ts, provider=host)
                 for ts in (peak, off_peak))
    row = (model, host, len(pricing.RATE_EPOCHS), False, 2, 2 * tokens["fresh"], 0,
           2 * tokens["read"], 2 * tokens["output"], 0, 0, stored)
    got = fold_per_model([row])[0]["cost_buckets"]
    assert sum(got.values()) == pytest.approx(stored)
    if schedule[0]["rates"] == HALF:
        want = {f: sum(pricing.rate_for(model, ts, host)[r] * tokens[t] / 1e6
                       for ts in (peak, off_peak))
                for f, r, t in (("fresh", "fresh", "fresh"), ("read", "read", "read"),
                                ("output", "output", "output"))}
        for field, value in want.items():
            assert got[field] == pytest.approx(value)


def test_a_schedule_adds_no_rate_epoch(monkeypatch):
    """Time-of-day windows repeat every week; they are not epochs, and the
    epoch list is exactly what it was without them."""
    before = list(pricing.RATE_EPOCHS)
    _schedule_novita(monkeypatch, [{"days": ["saturday"], "rates": HALF}])
    assert pricing.RATE_EPOCHS == before
