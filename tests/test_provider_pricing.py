"""Rates keyed by (model, serving provider): pricing.PROVIDER_RATES.

OpenRouter records carry the host that served them as message.provider,
and one model costs different amounts on different hosts. Every other
lane carries no provider, and a record without one must price exactly as
it did before the provider table existed — the z.ai subscription's GLM
usage in particular must not be repriced by an OpenRouter host's rate.
"""
from datetime import datetime, timezone

import pytest

from backend import pricing
from backend.api_common import fold_per_model, fold_per_model_provider

UTC = timezone.utc

V41 = "deepseek/deepseek-v4.1-flash"


def _cost(model, provider=None, ts=None, *, fresh=0, output=0, eph5=0,
          eph1h=0, unsplit_create=0, read=0):
    return pricing.compute_cost(
        model, fresh=fresh, output=output, eph5=eph5, eph1h=eph1h,
        unsplit_create=unsplit_create, read=read, ts=ts, provider=provider)


# --- provider rows price the record -----------------------------------------


def test_a_record_with_a_provider_is_priced_from_the_provider_table():
    # OpenRouter's Novita endpoint for deepseek-v4.1-flash, 2026-09-24:
    # $0.285 in / $1.14 out / $0.0057 cache read per 1M.
    r = pricing.resolve(V41, provider="Novita")
    assert r.kind == "exact"
    assert r.rates == {"fresh": 0.285, "create_5m": 0.285, "create_1h": 0.285,
                       "read": 0.0057, "output": 1.14}
    assert _cost(V41, "Novita", fresh=1_000_000, output=1_000_000,
                 read=1_000_000) == pytest.approx(0.285 + 1.14 + 0.0057)


def test_two_providers_of_one_model_price_differently():
    morph = pricing.rate_for(V41, provider="Morph")
    novita = pricing.rate_for(V41, provider="Novita")
    assert (morph["fresh"], morph["output"]) == (0.075, 0.3)
    assert morph != novita


def test_a_cache_write_prices_at_the_input_rate_when_the_host_lists_none():
    # Every snapshot row lists cache_write 0, so both create buckets
    # carry the input rate rather than a free write.
    for (_model, _provider), rates in pricing.PROVIDER_RATES.items():
        assert rates["create_5m"] == rates["fresh"]
        assert rates["create_1h"] == rates["fresh"]
    assert _cost(V41, "Novita", eph5=1_000_000, eph1h=1_000_000,
                 unsplit_create=1_000_000) == pytest.approx(3 * 0.285)


def test_the_provider_table_is_keyed_on_the_normalised_model_id():
    for model, _provider in pricing.PROVIDER_RATES:
        assert model == model.lower() and "." not in model
    assert pricing.rate_for("DeepSeek/DeepSeek-V4.1-Flash", provider="Novita") == \
        pricing.rate_for(V41, provider="Novita")


def test_the_seeded_models_and_their_provider_counts():
    per_model: dict[str, set] = {}
    for model, provider in pricing.PROVIDER_RATES:
        per_model.setdefault(model, set()).add(provider)
    assert {m: len(p) for m, p in per_model.items()} == {
        "z-ai/glm-5-3-flash": 31,
        "deepseek/deepseek-v4-1-flash": 26,
        "stealth/space-bunny-alpha": 1,
        "deepseek/deepseek-v4-flash-0731": 29,
        "deepseek/deepseek-v4-flash": 16,
    }


def test_a_provider_listing_two_prices_bills_the_higher_endpoint():
    # BaseTen serves deepseek-v4.1-flash from two fp8 endpoints that differ
    # only in cache-read price, and the transcript names only the host.
    # Billing the cheaper one would under-count whenever the other served.
    assert pricing.rate_for(V41, provider="BaseTen")["read"] == 0.03


def test_modal_glm_carries_its_one_remaining_endpoint():
    # Modal's fp8 glm-5.3-flash endpoint (0.45/1.50) was withdrawn; only the
    # nvfp4 endpoint at list price remains.
    assert pricing.rate_for("z-ai/glm-5.3-flash", provider="Modal") == {
        "fresh": 0.15, "create_5m": 0.15, "create_1h": 0.15,
        "read": 0.03, "output": 0.5}


# --- NULL provider: exactly today's pricing ---------------------------------


def test_the_same_model_with_no_provider_prices_exactly_as_before():
    # Pinned figures from before the provider table: deepseek-v4.1-flash
    # has no MODEL_RATES row, so it falls to DEFAULT (Opus 4.7 list,
    # 5/25) and is flagged estimated.
    r = pricing.resolve(V41)
    assert r.kind == "default"
    assert r.rates is pricing.DEFAULT_RATES
    assert _cost(V41, fresh=1_000_000, output=1_000_000) == 30.00
    assert _cost(V41, None, fresh=1_000_000, output=1_000_000) == 30.00


def test_the_zai_subscription_glm_is_not_repriced():
    # The z.ai official lane records glm-5.3-flash with no provider. Its
    # list price and its launch promotion are unchanged.
    tokens = {"fresh": 1_000_000, "output": 1_000_000, "read": 1_000_000}
    assert _cost("glm-5.3-flash", **tokens) == pytest.approx(0.15 + 0.50 + 0.03)
    promo = datetime(2026, 9, 1, tzinfo=UTC)
    assert _cost("glm-5.3-flash", ts=promo, **tokens) == \
        pytest.approx(0.075 + 0.25 + 0.015)
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
    slug = pricing.resolve("deepseek/deepseek-v4-flash-0731", provider="Cohere")
    perma = pricing.resolve("deepseek/deepseek-v4-flash-20260731",
                            provider="Cohere")
    assert slug.rates == perma.rates == {
        "fresh": 0.14, "create_5m": 0.14, "create_1h": 0.14,
        "read": 0.07, "output": 0.28}
    assert slug.key == perma.key == "deepseek/deepseek-v4-flash-0731"


def test_the_permaslug_never_takes_the_undated_models_rate():
    # Novita hosts both: the undated v4-flash at 0.14/0.28 and the 0731
    # snapshot at 0.4092/1.2276. A dated suffix read as "same model" would
    # bill the permaslug at the undated row.
    assert pricing.rate_for("deepseek/deepseek-v4-flash",
                            provider="Novita")["fresh"] == 0.14
    assert pricing.rate_for("deepseek/deepseek-v4-flash-20260731",
                            provider="Novita")["fresh"] == 0.4092


# --- SV-DATED-RATES holds for provider rows ----------------------------------


@pytest.fixture(name="synthetic_provider_window")
def _synthetic_provider_window_fixture(monkeypatch):
    """A made-up dated window on one (model, provider) row. The rates are
    unlike any real price so no assertion reads as a pricing fact."""
    cutover = datetime(2026, 9, 20, tzinfo=UTC)
    key = ("deepseek/deepseek-v4-1-flash", "Novita")
    before = {"fresh": 7.0, "create_5m": 7.0, "create_1h": 7.0,
              "read": 0.7, "output": 70.0}
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {key: [(cutover, before)]})
    monkeypatch.setattr(pricing, "RATE_EPOCHS", [cutover])
    return cutover, before, pricing.PROVIDER_RATES[key]


def test_a_provider_window_applies_before_its_cutover(synthetic_provider_window):
    cutover, before, after = synthetic_provider_window
    assert pricing.rate_for(V41, datetime(2026, 9, 19, tzinfo=UTC), "Novita") == before
    assert pricing.rate_for(V41, cutover, "Novita") == after
    assert pricing.rate_for(V41, None, "Novita") == after, "no ts => list"
    assert pricing.rate_for(V41, datetime(2026, 9, 19, tzinfo=UTC), "Morph") == \
        pricing.rate_for(V41, None, "Morph")


def test_live_rate_epochs_include_provider_windows():
    assert pricing.RATE_EPOCHS == sorted(
        {end for w in pricing.DATED_RATES.values() for end, _ in w}
        | {end for w in pricing.PROVIDER_DATED_RATES.values() for end, _ in w}
    )


def _row(model, provider, epoch, fresh=0, output=0, cost=0.0):
    # (model, provider, rate_epoch, long_context, turns, fresh,
    #  cache_create, cache_read, output, eph5, eph1h, cost_total)
    return (model, provider, epoch, False, 1, fresh, 0, 0, output, 0, 0, cost)


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
        assert m["cost_total"] == pytest.approx(in_window + past)
        assert sum(m["cost_buckets"].values()) == pytest.approx(m["cost_total"])


# --- the split fold -------------------------------------------------------------


def test_fold_prices_each_row_by_its_provider_and_keeps_the_model_total():
    novita = _cost(V41, "Novita", fresh=1_000_000, output=1_000_000)
    morph = _cost(V41, "Morph", fresh=1_000_000, output=1_000_000)
    direct = _cost(V41, None, fresh=1_000_000, output=1_000_000)
    rows = [_row(V41, "Novita", 0, 1_000_000, 1_000_000, novita),
            _row(V41, "Morph", 0, 1_000_000, 1_000_000, morph),
            _row(V41, None, 0, 1_000_000, 1_000_000, direct)]

    out = fold_per_model(rows)
    assert len(out) == 1
    per_model = out[0]
    assert per_model["model"] == V41
    assert per_model["turns"] == 3
    assert per_model["cost_total"] == pytest.approx(novita + morph + direct)
    assert sum(per_model["cost_buckets"].values()) == \
        pytest.approx(per_model["cost_total"])
    # The NULL-provider row is still a DEFAULT-rate estimate.
    assert per_model["estimated_rate"] is True

    split = {e["provider"]: e for e in fold_per_model_provider(rows)}
    assert set(split) == {"Novita", "Morph", None}
    for provider, want in (("Novita", novita), ("Morph", morph), (None, direct)):
        e = split[provider]
        assert e["model"] == V41
        assert e["cost_total"] == pytest.approx(want)
        assert sum(e["cost_buckets"].values()) == pytest.approx(want)
    assert split["Novita"]["estimated_rate"] is False
    assert split[None]["estimated_rate"] is True


def test_fold_of_null_provider_rows_is_unchanged():
    # Rows with no provider fold exactly as the model-only fold did. The
    # last epoch is past the GLM promotion, so list price applies.
    stored = _cost("glm-5.3-flash", fresh=2_000_000, output=500_000)
    last = len(pricing.RATE_EPOCHS)
    out = fold_per_model([_row("glm-5.3-flash", None, last, 2_000_000, 500_000, stored)])
    assert len(out) == 1
    m = out[0]
    assert m["cost_buckets"]["fresh"] == pytest.approx(0.30)
    assert m["cost_buckets"]["output"] == pytest.approx(0.25)
    assert m["estimated_rate"] is False
