"""Rates keyed by (model, serving provider): pricing.PROVIDER_RATES.

OpenRouter records carry the host that served them as message.provider,
and one model costs different amounts on different hosts. Every other
lane carries no provider, and a record without one must price exactly as
it did before the provider table existed — the z.ai subscription's GLM
usage in particular must not be repriced by an OpenRouter host's rate.
"""
from datetime import datetime, timedelta, timezone

import pytest

from backend import pricing
from backend.api_common import fold_per_model, fold_per_model_provider

UTC = timezone.utc

V41 = "deepseek/deepseek-v4.1-flash"
# The instant the provider table was seeded. The scheduled refresh appends
# entries effective from later instants only, so the prices these tests
# assert hold at this instant whatever it has committed since; a list
# price (no timestamp) is whatever the newest entry says today.
SEEDED = datetime(2026, 9, 24, 22, 3, 13, tzinfo=UTC)


def _cost(model, provider=None, ts=None, *, fresh=0, output=0, eph5=0,
          eph1h=0, unsplit_create=0, read=0):
    return pricing.compute_cost(
        model, fresh=fresh, output=output, eph5=eph5, eph1h=eph1h,
        unsplit_create=unsplit_create, read=read, ts=ts, provider=provider)


# --- provider rows price the record -----------------------------------------


def test_a_record_with_a_provider_is_priced_from_the_provider_table():
    # OpenRouter's Novita endpoint for deepseek-v4.1-flash, 2026-09-24:
    # $0.285 in / $1.14 out / $0.0057 cache read per 1M.
    r = pricing.resolve(V41, SEEDED, provider="Novita")  # sv-test-data: allow (closed-window pin at SEEDED)
    assert r.kind == "exact"
    assert r.rates == {"fresh": 0.285, "create_5m": 0.285, "create_1h": 0.285,
                       "read": 0.0057, "output": 1.14}
    assert _cost(V41, "Novita", SEEDED, fresh=1_000_000, output=1_000_000,
                 read=1_000_000) == pytest.approx(0.285 + 1.14 + 0.0057)


def test_two_providers_of_one_model_price_differently():
    morph = pricing.rate_for(V41, SEEDED, provider="Morph")  # sv-test-data: allow (closed-window pin at SEEDED)
    novita = pricing.rate_for(V41, SEEDED, provider="Novita")  # sv-test-data: allow (closed-window pin at SEEDED)
    assert (morph["fresh"], morph["output"]) == (0.075, 0.3)
    assert morph != novita


def test_a_cache_write_prices_at_the_input_rate_when_the_host_lists_none():
    # Every seeded row lists cache_write 0, so both create buckets carry
    # the input rate rather than a free write.
    for model, provider in pricing.PROVIDER_RATES:
        if (model, provider) not in pricing.PROVIDER_STARTS:
            rates = pricing.rate_for(model, SEEDED, provider)
            assert rates["create_5m"] == rates["fresh"]
            assert rates["create_1h"] == rates["fresh"]
    assert _cost(V41, "Novita", SEEDED, eph5=1_000_000, eph1h=1_000_000,
                 unsplit_create=1_000_000) == pytest.approx(3 * 0.285)


def test_the_provider_table_is_keyed_on_the_normalised_model_id():
    for model, _provider in pricing.PROVIDER_RATES:
        assert model == model.lower() and "." not in model
    assert (pricing.rate_for("DeepSeek/DeepSeek-V4.1-Flash", provider="Novita") ==  # sv-test-data: allow (derived: normalisation identity between the same loaded rows)
            pricing.rate_for(V41, provider="Novita"))  # sv-test-data: allow (derived: normalisation identity between the same loaded rows)


def test_baseten_bills_the_global_endpoint_the_keys_can_reach():
    # BaseTen lists deepseek-v4.1-flash at two cache-read prices: 0.03 US
    # in-region, 0.007 global. The account's keys allow only the global
    # data region, so the global price applies.
    assert pricing.rate_for(V41, SEEDED, provider="BaseTen")["read"] == 0.007  # sv-test-data: allow (closed-window pin at SEEDED)


def test_modal_glm_carries_its_one_remaining_endpoint():
    # Modal's fp8 glm-5.3-flash endpoint (0.45/1.50) was withdrawn; only the
    # nvfp4 endpoint at list price remains.
    assert pricing.rate_for("z-ai/glm-5.3-flash", SEEDED, provider="Modal") == {  # sv-test-data: allow (closed-window pin at SEEDED)
        "fresh": 0.15, "create_5m": 0.15, "create_1h": 0.15,
        "read": 0.03, "output": 0.5}


# --- NULL provider: exactly today's pricing ---------------------------------


def test_the_same_model_with_no_provider_prices_exactly_as_before():
    # Deepseek-v4.1-flash has no MODEL_RATES row, so it falls to the
    # DEFAULT row and is flagged estimated. The default row's values are
    # whatever the table says today; the pin is the lane's behaviour.
    r = pricing.resolve(V41)
    assert r.kind == "default"
    assert r.rates is pricing.DEFAULT_RATES
    default = pricing.DEFAULT_RATES
    want = default["fresh"] + default["output"]
    # approx: the same rates on both sides; the M-token scaling can cost
    # each term a last-bit rounding, never a real difference.
    assert _cost(V41, fresh=1_000_000, output=1_000_000) == pytest.approx(want)
    assert _cost(V41, None, fresh=1_000_000, output=1_000_000) == \
        pytest.approx(want)


def test_the_zai_subscription_glm_is_not_repriced():
    # The z.ai official lane records glm-5.3-flash with no provider: its
    # own row's list price at the record's time — never an OpenRouter
    # host's rate. The promotion's boundary comes from the row's loaded
    # window, so the assertion moves with the file.
    listed = pricing.MODEL_RATES["glm-5-3-flash"]
    windows = pricing.DATED_RATES["glm-5-3-flash"]
    cutover, promo = windows[0]
    tokens = {"fresh": 1_000_000, "output": 1_000_000, "read": 1_000_000}
    assert _cost("glm-5.3-flash", **tokens) == pytest.approx(
        listed["fresh"] + listed["output"] + listed["read"])
    assert _cost("glm-5.3-flash", ts=cutover - timedelta(seconds=1),
                 **tokens) == pytest.approx(
        promo["fresh"] + promo["output"] + promo["read"])
    # An OpenRouter host's GLM row never reaches the bare model id.
    assert _cost("glm-5.3-flash", "Novita", **tokens) == \
        _cost("glm-5.3-flash", **tokens)


def test_an_unknown_provider_falls_back_to_the_model_rate():
    for model in ("glm-5.3-flash", V41, "claude-opus-4-8"):
        want = pricing.resolve(model)
        got = pricing.resolve(model, provider="NoSuchHost")
        assert (got.rates, got.kind) == (want.rates, want.kind)
    # Spelling is the transcript's, matched exactly.
    assert pricing.resolve(V41, provider="novita").kind == "default"


def test_stealth_stays_free_with_or_without_a_provider():
    for provider in (None, "Stealth", "NoSuchHost"):
        r = pricing.resolve("stealth/space-bunny-alpha", provider=provider)
        assert r.kind == "exact"
        assert all(v == 0 for v in r.rates.values())
    assert _cost("stealth/space-bunny-alpha", "Stealth",
                 fresh=10**9, output=10**9) == 0


# --- the dated permaslug ------------------------------------------------------


def test_the_dated_permaslug_resolves_to_the_same_row_as_the_slug():
    slug = pricing.resolve("deepseek/deepseek-v4-flash-0731", SEEDED, provider="Cohere")  # sv-test-data: allow (closed-window pin at SEEDED)
    perma = pricing.resolve("deepseek/deepseek-v4-flash-20260731", SEEDED,
                            provider="Cohere")  # sv-test-data: allow (closed-window pin at SEEDED)
    assert slug.rates == perma.rates == {
        "fresh": 0.14, "create_5m": 0.14, "create_1h": 0.14,
        "read": 0.07, "output": 0.28}
    assert slug.key == perma.key == "deepseek/deepseek-v4-flash-0731"


def test_the_permaslug_never_takes_the_undated_models_rate():
    # Novita hosts both: the undated v4-flash at 0.14/0.28 and the 0731
    # snapshot at 0.4092/1.2276. A dated suffix read as "same model" would
    # bill the permaslug at the undated row.
    assert pricing.rate_for("deepseek/deepseek-v4-flash", SEEDED,
                            provider="Novita")["fresh"] == 0.14  # sv-test-data: allow (closed-window pin at SEEDED)
    assert pricing.rate_for("deepseek/deepseek-v4-flash-20260731", SEEDED,
                            provider="Novita")["fresh"] == 0.4092  # sv-test-data: allow (closed-window pin at SEEDED)


# --- SV-DATED-RATES holds for provider rows ----------------------------------


@pytest.fixture(name="synthetic_provider_window")
def _synthetic_provider_window_fixture(monkeypatch):
    """A made-up dated window on one (model, provider) row. The rates are
    unlike any real price so no assertion reads as a pricing fact.

    PROVIDER_SCHEDULES is emptied too: an appended entry could carry a
    schedule on the touched rows, and a live window answering the schedule
    lookup would price `past` at the window's rates instead of the list
    price the assertion names (issue #191)."""
    cutover = datetime(2026, 9, 20, tzinfo=UTC)
    key = ("deepseek/deepseek-v4-1-flash", "Novita")
    before = {"fresh": 7.0, "create_5m": 7.0, "create_1h": 7.0,
              "read": 0.7, "output": 70.0}
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {key: [(cutover, before)]})
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {})
    monkeypatch.setattr(pricing, "RATE_EPOCHS", [cutover])
    return cutover, before, pricing.PROVIDER_RATES[key]


def test_a_provider_window_applies_before_its_cutover(synthetic_provider_window):
    cutover, before, after = synthetic_provider_window
    assert pricing.rate_for(V41, datetime(2026, 9, 19, tzinfo=UTC), "Novita") == before  # sv-test-data: allow (derived: expected is the fixture's own synthetic window rates)
    assert pricing.rate_for(V41, cutover, "Novita") == after  # sv-test-data: allow (derived: identity against the list rates the fixture returns)
    assert pricing.rate_for(V41, None, "Novita") == after, "no ts => list"  # sv-test-data: allow (derived: identity against the list rates the fixture returns)
    assert (pricing.rate_for(V41, datetime(2026, 9, 19, tzinfo=UTC), "Morph") ==  # sv-test-data: allow (derived: same-tables identity; Morph carries no window)
            pricing.rate_for(V41, None, "Morph"))  # sv-test-data: allow (derived: same-tables identity; Morph carries no window)


def test_live_rate_epochs_include_provider_windows():
    assert pricing.RATE_EPOCHS == sorted(
        {end for w in pricing.DATED_RATES.values() for end, _ in w}
        | {end for w in pricing.PROVIDER_DATED_RATES.values() for end, _ in w}
        | set(pricing.PROVIDER_STARTS.values())
    )


def _row(model, provider, epoch, fresh=0, output=0, cost=0.0, read=0):
    # (model, provider, rate_epoch, long_context, turns, fresh,
    #  cache_create, cache_read, output, eph5, eph1h, cost_total)
    return (model, provider, epoch, False, 1, fresh, 0, read, output, 0, 0, cost)


def test_fold_reconciles_across_a_provider_cutover(synthetic_provider_window):
    cutover, before, after = synthetic_provider_window
    in_window = _cost(V41, "Novita", ts=datetime(2026, 9, 19, tzinfo=UTC),
                      fresh=1_000_000)
    past = _cost(V41, "Novita", ts=cutover, fresh=1_000_000)
    assert (in_window, past) == (pytest.approx(before["fresh"]),
                                 pytest.approx(after["fresh"]))
    rows = [_row(V41, "Novita", 0, fresh=1_000_000, cost=in_window),
            _row(V41, "Novita", 1, fresh=1_000_000, cost=past)]
    for m in fold_per_model(rows) + fold_per_model_provider(rows):
        # cost_total and the buckets each round to 4 decimals; the sum
        # carries 5e-5 of rounding per rounded quantity it touches.
        assert m["cost_total"] == pytest.approx(in_window + past, abs=5e-5)
        assert sum(m["cost_buckets"].values()) == pytest.approx(
            m["cost_total"], abs=1e-4)


# --- the split fold -------------------------------------------------------------
# The fold tests price SYNTHETIC provider rows (issue #191): the rates are
# unlike any real price and the keys sit in no other rate table, so a
# legitimate refresh appending entries or first-seeing a host cannot move an
# assertion. Rows carry epoch 0, before every live boundary; a synthetic row
# has no dated window, so the representative time reads the same list rates
# the stored costs priced.

SYNTH_MODEL = "acme/flux-9"
HOST_A = "HostA"
HOST_B = "HostB"


@pytest.fixture(name="synthetic_provider_rows")
def _synthetic_provider_rows_fixture(monkeypatch):
    """Two made-up provider rows on one synthetic model, at rates unlike
    any real price. The keys are absent from PROVIDER_DATED_RATES,
    PROVIDER_SCHEDULES and PROVIDER_STARTS, which is what leaves them
    inert: every lookup for them misses and the list price answers."""
    monkeypatch.setitem(pricing.PROVIDER_RATES, (SYNTH_MODEL, HOST_A),
                        {"fresh": 3.3, "create_5m": 3.3, "create_1h": 3.3,
                         "read": 0.33, "output": 33.3})
    monkeypatch.setitem(pricing.PROVIDER_RATES, (SYNTH_MODEL, HOST_B),
                        {"fresh": 4.4, "create_5m": 4.4, "create_1h": 4.4,
                         "read": 0.44, "output": 44.4})
    return HOST_A, HOST_B


def test_fold_prices_each_row_by_its_provider_and_keeps_the_model_total(
        synthetic_provider_rows):
    via_a = _cost(SYNTH_MODEL, HOST_A, fresh=1_000_000, output=1_000_000)
    via_b = _cost(SYNTH_MODEL, HOST_B, fresh=1_000_000, output=1_000_000)
    assert via_a != via_b, "the two hosts must price differently"
    direct = _cost(SYNTH_MODEL, None, fresh=1_000_000, output=1_000_000)
    rows = [_row(SYNTH_MODEL, HOST_A, 0, 1_000_000, 1_000_000, via_a),
            _row(SYNTH_MODEL, HOST_B, 0, 1_000_000, 1_000_000, via_b),
            _row(SYNTH_MODEL, None, 0, 1_000_000, 1_000_000, direct)]

    out = fold_per_model(rows)
    assert len(out) == 1
    per_model = out[0]
    assert per_model["model"] == SYNTH_MODEL
    assert per_model["turns"] == 3
    assert per_model["cost_total"] == pytest.approx(
        via_a + via_b + direct, abs=5e-5)
    assert sum(per_model["cost_buckets"].values()) == \
        pytest.approx(per_model["cost_total"], abs=2e-4)
    # The NULL-provider row is still a DEFAULT-rate estimate.
    assert per_model["estimated_rate"] is True

    split = {e["provider"]: e for e in fold_per_model_provider(rows)}
    assert set(split) == {HOST_A, HOST_B, None}
    for provider, want in ((HOST_A, via_a), (HOST_B, via_b), (None, direct)):
        e = split[provider]
        assert e["model"] == SYNTH_MODEL
        assert e["cost_total"] == pytest.approx(want, abs=5e-5)
        assert sum(e["cost_buckets"].values()) == pytest.approx(want, abs=2e-4)
    assert split[HOST_A]["estimated_rate"] is False
    assert split[None]["estimated_rate"] is True


def test_fold_reconciles_across_several_provider_entries(monkeypatch):
    """A provider row whose history holds SEVERAL dated entries: each fold
    row prices at the epoch its pricing time falls in, and the buckets
    reconcile to the stored totals. A legitimate refresh appends entries
    exactly like these; the reconciliation must not care."""
    key = (SYNTH_MODEL, "HostCo")
    t1 = datetime(2026, 8, 1, tzinfo=UTC)
    t2 = datetime(2026, 9, 1, tzinfo=UTC)
    spans = ({"fresh": 7.0, "create_5m": 7.0, "create_1h": 7.0,
              "read": 0.7, "output": 70.0},
             {"fresh": 5.0, "create_5m": 5.0, "create_1h": 5.0,
              "read": 0.5, "output": 50.0},
             {"fresh": 3.0, "create_5m": 3.0, "create_1h": 3.0,
              "read": 0.3, "output": 30.0})
    monkeypatch.setitem(pricing.PROVIDER_RATES, key, spans[-1])
    monkeypatch.setitem(pricing.PROVIDER_DATED_RATES, key,
                        [(t1, spans[0]), (t2, spans[1])])
    monkeypatch.setattr(pricing, "RATE_EPOCHS", [t1, t2])

    # One record per span, priced at the instant the parser would price it:
    # before t1 (the first window), at t1 (the second), at t2 (list).
    def span_of(ts):
        return len([t for t in (t1, t2) if ts >= t])

    pricing_ts = (t1 - timedelta(microseconds=1), t1, t2)
    stored = [_cost(SYNTH_MODEL, "HostCo", ts, fresh=1_000_000)
              for ts in pricing_ts]
    assert stored == [pytest.approx(r["fresh"]) for r in spans], \
        "each record must price at its own span's rates"
    rows = [_row(SYNTH_MODEL, "HostCo", span_of(ts), 1_000_000, cost=cost)
            for ts, cost in zip(pricing_ts, stored, strict=True)]

    for m in fold_per_model(rows) + fold_per_model_provider(rows):
        assert m["cost_total"] == pytest.approx(sum(stored))
        assert sum(m["cost_buckets"].values()) == pytest.approx(m["cost_total"])
        assert m["cost_buckets"]["fresh"] == pytest.approx(sum(stored))


def test_a_non_uniform_schedule_still_sums_to_the_stored_total(monkeypatch):
    """A provider entry whose weekly window scales the five rates by
    DIFFERENT factors: the fold re-derives at one representative time, so
    its split of a row whose records were priced at different times of day
    is approximate (the documented, reported case) — but the buckets are
    scaled to the stored total, so the sum stays exact."""
    key = (SYNTH_MODEL, "HostCo")
    default = {"fresh": 6.0, "create_5m": 6.0, "create_1h": 6.0,
               "read": 0.6, "output": 60.0}
    peak = {"fresh": 3.0, "create_5m": 3.0, "create_1h": 3.0,
            "read": 0.006, "output": 30.0}
    monkeypatch.setitem(pricing.PROVIDER_RATES, key, default)
    schedule = pricing._schedule(  # pylint: disable=protected-access
        [{"start": 900, "end": 1700, "rates": peak}], f"{SYNTH_MODEL} via HostCo")
    monkeypatch.setitem(pricing.PROVIDER_SCHEDULES, key, {0: schedule})

    noon = datetime(2026, 9, 21, 12, tzinfo=UTC)    # inside 09:00-17:00
    evening = datetime(2026, 9, 21, 20, tzinfo=UTC)  # outside it
    stored = (_cost(SYNTH_MODEL, "HostCo", noon, fresh=1_000_000,
                    read=1_000_000, output=1_000_000)
              + _cost(SYNTH_MODEL, "HostCo", evening, fresh=1_000_000,
                      read=1_000_000, output=1_000_000))
    assert stored == pytest.approx(
        (3.0 + 0.006 + 30.0) + (6.0 + 0.6 + 60.0)), \
        "the records must price by the window and the default respectively"
    rows = [_row(SYNTH_MODEL, "HostCo", 0, fresh=2_000_000, output=2_000_000,
                 read=2_000_000, cost=stored)]

    for m in fold_per_model(rows) + fold_per_model_provider(rows):
        assert m["cost_total"] == pytest.approx(stored)
        # The buckets carry the 4-decimal rounding _fold applies: five of
        # them can drift 5e-5 each from the scaled sum, hence the abs.
        assert sum(m["cost_buckets"].values()) == pytest.approx(
            m["cost_total"], abs=3e-4)


def test_fold_of_null_provider_rows_is_unchanged(monkeypatch):
    # Rows with no provider fold exactly as the model-only fold did. Pinned
    # on a synthetic models row: the stored cost and the fold's
    # re-derivation read the same made-up rates, so no live rate — list or
    # refreshed — can move either side (issue #191).
    model = "acme/grommet-7"
    rates = {"fresh": 0.21, "create_5m": 0.21, "create_1h": 0.21,
             "read": 0.021, "output": 0.71}
    monkeypatch.setitem(pricing.MODEL_RATES, model, rates)
    stored = _cost(model, fresh=2_000_000, output=500_000)
    last = len(pricing.RATE_EPOCHS)
    out = fold_per_model([_row(model, None, last, 2_000_000, 500_000, stored)])
    assert len(out) == 1
    m = out[0]
    assert m["cost_buckets"]["fresh"] == pytest.approx(2 * rates["fresh"])
    assert m["cost_buckets"]["output"] == pytest.approx(0.5 * rates["output"])
    assert m["estimated_rate"] is False
