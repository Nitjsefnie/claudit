"""Rates keyed by (model, serving provider): pricing.PROVIDER_RATES.

OpenRouter records carry the host that served them as message.provider,
and one model costs different amounts on different hosts. Every other
lane carries no provider and must price by the model alone.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend import pricing
from backend.pricing_document import expand_pricing_doc
from backend.pricing_load import _instant
from backend.pricing_rows import encode_pricing_doc, flatten_provider_history
from backend.api_cache import _session_total
from backend.api_common import fold_per_model, fold_per_model_provider
from tests.refresh_fixture_builders import seed_doc

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]

V41 = "deepseek/deepseek-v4.1-flash"
V41_KEY = next(key for key, entry in json.loads(
    pricing.PRICING_JSON.read_text(encoding="utf-8"))["openrouter"]["models"].items()  # sv-test-data: allow (catalog configuration only; tracked key derived from id)
               if entry["id"] == V41)
# A fixed instant inside the synthetic provider's closed window.
SEEDED = datetime(2026, 9, 24, 22, 3, 13, tzinfo=UTC)
SEEDED_MODEL, SEEDED_HOST = "acme/seeded-9", "SyntheticHost"


def _cost(model, provider=None, ts=None, *, fresh=0, output=0, eph5=0,
          eph1h=0, unsplit_create=0, read=0):
    return pricing.compute_cost(
        model, fresh=fresh, output=output, eph5=eph5, eph1h=eph1h,
        unsplit_create=unsplit_create, read=read, ts=ts,
        res=pricing.resolve(model, ts, provider))


def _assert_provider_resolution_returns_loaded_row(
        model: str, normalized_model: str, host: str) -> None:
    """Pin the seeded history window and the undated current provider row."""
    result = pricing.resolve(model, SEEDED, provider=host)
    assert result.kind == "exact" and result.key == normalized_model
    assert result.rates is pricing.rate_for(model, SEEDED, provider=host)
    assert pricing.rate_for(model, provider=host) is \
        pricing.PROVIDER_RATES[(normalized_model, host)]


@pytest.fixture(name="synthetic_seeded_provider")
def _synthetic_seeded_provider_fixture(monkeypatch):
    doc = seed_doc(
        tracked={SEEDED_MODEL: {"id": SEEDED_MODEL}},
        providers={SEEDED_MODEL: {
            SEEDED_HOST: [
                {"from": None, "fresh": 0.285, "read": 0.0057, "output": 1.14},
                {"from": "2026-10-01T00:00:00Z", "fresh": 0.57,
                 "read": 0.0114, "output": 2.28}],
            "UndatedHost": [{"from": None, "fresh": 1, "read": 0.1, "output": 10}]}})
    for name, value in pricing.load_tables(doc).items():
        monkeypatch.setattr(pricing, name, value)


def test_a_record_with_a_provider_is_priced_from_the_provider_table(synthetic_seeded_provider):
    r = pricing.resolve(SEEDED_MODEL, SEEDED, provider=SEEDED_HOST)
    assert r.kind == "exact"
    assert {field: r.rates[field] for field in pricing.RATE_FIELDS} == {
        "fresh": 0.285, "create_5m": 0.285, "create_1h": 0.285,
        "read": 0.0057, "output": 1.14}
    assert _cost(SEEDED_MODEL, SEEDED_HOST, SEEDED, fresh=1_000_000, output=1_000_000,
                 read=1_000_000) == pytest.approx(
                     0.285 + 1.14 + 0.0057, rel=1e-12)


def test_fold_exposes_only_estimate_status():
    row = _row("synthetic/model-9", None, 0, 1000, cost=0.01)
    for folded in (fold_per_model([row], pair_bounds={}),
                   fold_per_model_provider([row], pair_bounds={})):
        for entry in (folded[0], _session_total(folded)):
            assert "estimated_rate" in entry
            assert "banded_rate" not in entry


def test_a_cache_write_prices_at_the_input_rate_when_the_host_lists_none(synthetic_seeded_provider):
    rates = pricing.rate_for(SEEDED_MODEL, SEEDED, SEEDED_HOST)
    assert rates["create_5m"] == rates["create_1h"] == rates["fresh"] == 0.285
    assert _cost(SEEDED_MODEL, SEEDED_HOST, SEEDED, eph5=1_000_000, eph1h=1_000_000,
                 unsplit_create=1_000_000) == pytest.approx(
                     3 * 0.285, rel=1e-12)


@pytest.mark.parametrize("create_5m,create_1h,unsplit_create,want", [
    pytest.param(4.0, 4.0, 1_000_000, 12.0, id="unsplit-input-rate"),
    pytest.param(5.0, 8.0, 0, 13.0, id="listed-1h-tier"),
])
def test_provider_cache_writes_price_each_tier(
        monkeypatch, create_5m, create_1h, unsplit_create, want):
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {
        ("acme/v9", "SyntheticHost"): {"fresh": 4.0, "create_5m": create_5m,
                                       "create_1h": create_1h, "read": 0.2,
                                       "output": 20.0}})
    rates = pricing.rate_for("acme/v9", SEEDED, provider="SyntheticHost")
    assert rates["fresh"] == 4.0
    assert (rates["create_5m"], rates["create_1h"]) == (create_5m, create_1h)
    assert _cost("acme/v9", "SyntheticHost", SEEDED, eph5=1_000_000,
                 eph1h=1_000_000, unsplit_create=unsplit_create) == pytest.approx(
                     want, rel=1e-12)


def test_the_provider_table_is_keyed_on_the_normalised_model_id(
        monkeypatch: pytest.MonkeyPatch) -> None:
    model = "deepseek/acme-v4-1-flash"
    host = "SyntheticHost"
    variant = model.upper()
    rates = {"fresh": 1.0, "create_5m": 2.0, "create_1h": 3.0,
             "read": 0.1, "output": 10.0}
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {(model, host): rates})
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {})
    monkeypatch.setattr(pricing, "PROVIDER_STARTS", {})
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {})
    assert pricing.rate_for(variant, provider=host) is rates
    assert pricing.rate_for(model, provider=host) is rates


@pytest.fixture(params=["scalar", "grouped"], name="append_history")
def _append_history_fixture(request):
    first = {"from": "2026-10-01T00:00:00Z", "fresh": 7, "read": 0.7, "output": 70}
    middle = {"from": "2026-10-02T00:00:00Z", "fresh": 9, "read": 0.9, "output": 90}
    last = {**first, "from": "2026-10-03T00:00:00Z"}
    dated = ([{**first, "from": [first["from"], last["from"]]}, middle]
             if request.param == "grouped" else [first, middle, last])
    return [{"from": None, "fresh": 5, "read": 0.5, "output": 50}, *dated]


def test_provider_resolution_assertions_survive_later_dated_appends(
        monkeypatch: pytest.MonkeyPatch, append_history: list[dict]) -> None:
    """A later rate entry moves today's provider row into a dated window."""
    base_model, base_host = "acme/provider-9", "SyntheticHost"
    modal_model, modal_host = "glm-acme-9", "SyntheticVendor"
    modal_id = f"z-ai/{modal_model}"
    document = encode_pricing_doc(seed_doc(
        tracked={base_model: {"id": base_model},
                 modal_model: {"id": modal_id, "vendor_host": modal_host}},
        providers={base_model: {base_host: append_history},
                   modal_model: {modal_host: append_history}}))
    assert pricing.load_tables(document)["PROVIDER_RATES"][(base_model, base_host)]["fresh"] == 7
    document = expand_pricing_doc(document)

    def append_later(entries: list[dict]) -> None:
        occurrences, _ = flatten_provider_history(entries, "test append", _instant)
        latest = occurrences[-1]
        newest = _instant(latest["from"], "test append") if latest["from"] is not None else SEEDED
        instant = max(SEEDED, newest).astimezone(UTC) + timedelta(seconds=1)
        values = {field: latest[field] for field in pricing.RATE_FIELDS}
        values["fresh"] += 0.01
        entries.append({"from": instant.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        **values, "note": "test append"})
        fetched = _instant(document["provider_rates_fetched"], "test fetch stamp")
        document["provider_rates_fetched"] = max(fetched, instant).astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ")

    append_later(document["providers"][base_model][base_host])
    append_later(document["providers"][modal_model][modal_host])
    for model, host in ((base_model, base_host), (modal_model, modal_host)):
        appended = document["providers"][model][host][-1]
        assert appended["from"] == "2026-10-03T00:00:01Z"
        assert (appended["fresh"], appended["read"], appended["output"]) == (7.01, 0.7, 70)
    tables = pricing.load_tables(document)
    for name, value in tables.items():
        monkeypatch.setattr(pricing, name, value)

    _assert_provider_resolution_returns_loaded_row(
        base_model, base_model, base_host)
    _assert_provider_resolution_returns_loaded_row(
        modal_id, modal_model, modal_host)


# --- NULL provider: exactly today's pricing ---------------------------------


def test_a_model_without_first_party_tracking_uses_the_default():
    """Default fallback is independent of the maintained vendor-tracked set."""
    model = "acme/untracked-v9"
    r = pricing.resolve(model)
    assert r.kind == "default"
    assert r.rates is pricing.DEFAULT_RATES
    default = pricing.DEFAULT_RATES
    want = default["fresh"] + default["output"]
    # rel=1e-12: the same rates on both sides; per-million scaling
    # can cost each term a last-bit rounding, never a real difference.
    assert _cost(model, fresh=1_000_000, output=1_000_000) == \
        pytest.approx(want, rel=1e-12)
    assert _cost(model, None, fresh=1_000_000, output=1_000_000) == \
        pytest.approx(want, rel=1e-12)


def test_the_zai_subscription_glm_is_not_repriced():
    listed = pricing.PROVIDER_RATES[("glm-5-3-flash", "Z.AI")]
    tokens = {"fresh": 1_000_000, "output": 1_000_000, "read": 1_000_000}
    assert _cost("glm-5.3-flash", **tokens) == pytest.approx(
        listed["fresh"] + listed["output"] + listed["read"], rel=1e-12)
    for cutover, _ in pricing.PROVIDER_DATED_RATES.get(("glm-5-3-flash", "Z.AI"), []):
        instant = cutover - timedelta(microseconds=1)
        promo = pricing.rate_for("glm-5.3-flash", instant)  # sv-test-data: allow (same-row runtime cost identity)
        assert _cost("glm-5.3-flash", ts=instant, **tokens) == pytest.approx(
            promo["fresh"] + promo["output"] + promo["read"], rel=1e-12)
    # Unknown hosts use the model's Z.AI row; OpenRouter host rates do not reach it.
    assert ("glm-5-3-flash", "NoSuchHost") not in pricing.PROVIDER_RATES
    assert _cost("glm-5.3-flash", "NoSuchHost", **tokens) == \
        _cost("glm-5.3-flash", **tokens)


def test_an_unknown_provider_falls_back_to_the_model_rate(
        synthetic_provider_dated_rate):
    for model in ("glm-5.3-flash", V41, "claude-opus-4-8"):
        want = pricing.resolve(model)
        got = pricing.resolve(model, provider="NoSuchHost")
        assert (got.rates, got.kind) == (want.rates, want.kind)
    case = synthetic_provider_dated_rate
    lowercase = case.host.lower()
    assert pricing.resolve(case.model, provider=case.host).kind == "exact"
    assert pricing.resolve(case.model, provider=lowercase).kind == "default"


def test_stealth_stays_free_with_or_without_a_provider():
    for provider in (None, "Stealth", "NoSuchHost"):
        r = pricing.resolve("stealth/space-bunny-alpha", provider=provider)
        assert r.kind == "exact"
        assert all(v == 0 for v in r.rates.values())
    assert _cost("stealth/space-bunny-alpha", "Stealth",
                 fresh=10**9, output=10**9) == 0


def test_the_dated_permaslug_resolves_to_the_same_row_as_the_slug(
        monkeypatch: pytest.MonkeyPatch) -> None:
    slug = "deepseek/acme-v4-flash-0731"
    host = "SyntheticHost"
    rates = {"fresh": 7.0, "create_5m": 7.0, "create_1h": 7.0,
             "read": 0.7, "output": 70.0}
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {(slug, host): rates})
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {})
    monkeypatch.setattr(pricing, "PROVIDER_STARTS", {})
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {})
    resolved_slug = pricing.resolve(slug, SEEDED, provider=host)
    permaslug = pricing.resolve(
        "deepseek/acme-v4-flash-20260731", SEEDED, provider=host)
    assert resolved_slug.key == permaslug.key == slug
    assert resolved_slug.rates is permaslug.rates is rates


def test_the_permaslug_never_takes_the_undated_models_rate(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # Novita hosts both: the undated v4-flash at 0.14/0.28 and the 0731
    # snapshot at 0.4092/1.2276. A dated suffix read as "same model" would
    # bill the permaslug at the undated row.
    bare = "deepseek/acme-v4-flash"
    snapshot = "deepseek/acme-v4-flash-0731"
    host = "SyntheticHost"
    bare_rates = {"fresh": 7.0, "create_5m": 7.0, "create_1h": 7.0,
                  "read": 0.7, "output": 70.0}
    snapshot_rates = {"fresh": 8.0, "create_5m": 8.0, "create_1h": 8.0,
                      "read": 0.8, "output": 80.0}
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {
        (bare, host): bare_rates, (snapshot, host): snapshot_rates})
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {})
    monkeypatch.setattr(pricing, "PROVIDER_STARTS", {})
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {})
    assert pricing.rate_for(bare, SEEDED, provider=host) is bare_rates
    assert pricing.rate_for(
        "deepseek/acme-v4-flash-20260731", SEEDED,
        provider=host) is snapshot_rates


# --- SV-DATED-RATES holds for provider rows ----------------------------------


@pytest.fixture(name="synthetic_provider_window")
def _synthetic_provider_window_fixture(monkeypatch, synthetic_seeded_provider):
    """A made-up dated window on one (model, provider) row. The rates are
    unlike any real price so no assertion reads as a pricing fact.

    PROVIDER_SCHEDULES is emptied too: an appended entry could carry a
    schedule on the touched rows, and a live window answering the schedule
    lookup would price `past` at the window's rates instead of the list
    price the assertion names."""
    cutover = datetime(2026, 9, 20, tzinfo=UTC)
    key = (SEEDED_MODEL, SEEDED_HOST)
    before = {"fresh": 7.0, "create_5m": 7.0, "create_1h": 7.0,
              "read": 0.7, "output": 70.0}
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {key: [(cutover, before)]})
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {})
    monkeypatch.setattr(pricing, "RATE_EPOCHS", [cutover])
    return cutover, before, pricing.PROVIDER_RATES[key]


def test_a_provider_window_applies_before_its_cutover(synthetic_provider_window):
    cutover, before, after = synthetic_provider_window
    assert pricing.rate_for(SEEDED_MODEL, datetime(2026, 9, 19, tzinfo=UTC), SEEDED_HOST) == before
    assert pricing.rate_for(SEEDED_MODEL, cutover, SEEDED_HOST) == after
    assert pricing.rate_for(SEEDED_MODEL, None, SEEDED_HOST) == after, "no ts => list"
    assert (pricing.rate_for(SEEDED_MODEL, datetime(2026, 9, 19, tzinfo=UTC), "UndatedHost") ==
            pricing.rate_for(SEEDED_MODEL, None, "UndatedHost"))


def _row(model, provider, epoch, fresh=0, output=0, cost=0.0, read=0):
    # (model, provider, rate_epoch, long_context, turns, fresh,
    #  cache_create, cache_read, output, eph5, eph1h, cost_total)
    return (model, provider, epoch, False, 1, fresh, 0, read, output, 0, 0, cost)


# The fold tests price SYNTHETIC provider rows: the rates are
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

    pair_bounds = {
        (SYNTH_MODEL, HOST_A): [],
        (SYNTH_MODEL, HOST_B): [],
        (SYNTH_MODEL, ""): [],
    }
    out = fold_per_model(rows, pair_bounds=pair_bounds)
    assert len(out) == 1
    per_model = out[0]
    assert per_model["model"] == SYNTH_MODEL
    assert per_model["turns"] == 3
    # abs=1e-4: cost_total is rounded to 4 decimals against an unrounded
    # sum — 5e-5 of rounding, and perturbed values can sit exactly on a
    # 4-decimal midpoint where float representation tips past the bound.
    assert per_model["cost_total"] == pytest.approx(
        via_a + via_b + direct, abs=1e-4)
    # Five buckets + the total, each rounded to 4 decimals: they can
    # disagree by up to 6 * 5e-5 = 3e-4 whatever the rates are.
    assert sum(per_model["cost_buckets"].values()) == \
        pytest.approx(per_model["cost_total"], abs=3e-4)
    # The NULL-provider row is still a DEFAULT-rate estimate.
    assert per_model["estimated_rate"] is True

    split = {e["provider"]: e for e in fold_per_model_provider(
        rows, pair_bounds=pair_bounds)}
    assert set(split) == {HOST_A, HOST_B, None}
    for provider, want in ((HOST_A, via_a), (HOST_B, via_b), (None, direct)):
        e = split[provider]
        assert e["model"] == SYNTH_MODEL
        # abs=1e-4, same midpoint-margin reason as the model-total half.
        assert e["cost_total"] == pytest.approx(want, abs=1e-4)
        # Five rounded buckets against the unrounded sum they decompose:
        # up to 5 * 5e-5 = 2.5e-4, and 3e-4 covers it with margin.
        assert sum(e["cost_buckets"].values()) == pytest.approx(want, abs=3e-4)
    assert split[HOST_A]["estimated_rate"] is False
    assert split[None]["estimated_rate"] is True


@pytest.mark.parametrize("history", ["cutover", "several-entries"])
def test_fold_reconciles_across_provider_entries(
        monkeypatch, synthetic_provider_window, history):
    if history == "cutover":
        model, host = SEEDED_MODEL, SEEDED_HOST
        boundaries = [synthetic_provider_window[0]]
        spans = synthetic_provider_window[1:]
        pricing_ts = (datetime(2026, 9, 19, tzinfo=UTC), boundaries[0])
        fresh_tokens = 1_000_000
    else:
        model, host = SYNTH_MODEL, "HostCo"
        boundaries = [datetime(2026, 8, 1, tzinfo=UTC),
                      datetime(2026, 9, 1, tzinfo=UTC)]
        spans = tuple({"fresh": fresh, "create_5m": fresh, "create_1h": fresh,
                       "read": fresh / 10, "output": fresh * 10}
                      for fresh in (7.0, 5.0, 3.0))
        key = (model, host)
        monkeypatch.setitem(pricing.PROVIDER_RATES, key, spans[-1])
        monkeypatch.setitem(pricing.PROVIDER_DATED_RATES, key,
                            list(zip(boundaries, spans[:-1], strict=True)))
        monkeypatch.setattr(pricing, "RATE_EPOCHS", boundaries)
        pricing_ts = (boundaries[0] - timedelta(microseconds=1), *boundaries)
        # Keep the original rounding regression: 0.01485 folds to 0.0149.
        fresh_tokens = 990

    stored = [_cost(model, host, ts, fresh=fresh_tokens) for ts in pricing_ts]
    assert stored == [
        pytest.approx(fresh_tokens * r["fresh"] / 1_000_000,
                      rel=1e-12 if history == "cutover" else 1e-6)
        for r in spans
    ], "each record must price at its own span's rates"
    rows = [_row(model, host, epoch, fresh_tokens, cost=cost)
            for epoch, cost in enumerate(stored)]
    pair_bounds = {(model, host): boundaries}
    for m in (fold_per_model(rows, pair_bounds=pair_bounds)
              + fold_per_model_provider(rows, pair_bounds=pair_bounds)):
        if history == "cutover":
            assert m["cost_total"] == pytest.approx(round(sum(stored), 4), abs=1e-6)
            assert sum(m["cost_buckets"].values()) == pytest.approx(
                m["cost_total"], abs=3e-4)
        else:
            assert m["cost_total"] == pytest.approx(sum(stored), abs=1e-4)
            assert sum(m["cost_buckets"].values()) == pytest.approx(m["cost_total"])
            assert m["cost_buckets"]["fresh"] == pytest.approx(sum(stored), abs=1e-4)


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
    # At 990 tokens/span the raw schedule sum 0.09860994 folds to 0.0986,
    # which the old default approx tolerance rejected.
    tokens_per_span = 990
    stored = (_cost(SYNTH_MODEL, "HostCo", noon, fresh=tokens_per_span,
                    read=tokens_per_span, output=tokens_per_span)
              + _cost(SYNTH_MODEL, "HostCo", evening, fresh=tokens_per_span,
                      read=tokens_per_span, output=tokens_per_span))
    assert stored == pytest.approx(
        ((3.0 + 0.006 + 30.0) + (6.0 + 0.6 + 60.0))
        * tokens_per_span / 1_000_000), \
        "the records must price by the window and the default respectively"
    rows = [_row(SYNTH_MODEL, "HostCo", 0, fresh=2 * tokens_per_span,
                 output=2 * tokens_per_span, read=2 * tokens_per_span, cost=stored)]

    pair_bounds = {(SYNTH_MODEL, "HostCo"): []}
    for m in (fold_per_model(rows, pair_bounds=pair_bounds)
              + fold_per_model_provider(rows, pair_bounds=pair_bounds)):
        assert m["cost_total"] == pytest.approx(stored, abs=1e-4)
        # The buckets carry the 4-decimal rounding _fold applies: five of
        # them can drift 5e-5 each from the scaled sum, hence the abs.
        assert sum(m["cost_buckets"].values()) == pytest.approx(
            m["cost_total"], abs=3e-4)


def test_fold_of_null_provider_rows_is_unchanged(monkeypatch):
    # Rows with no provider fold exactly as the model-only fold did. Pinned
    # on a synthetic models row: the stored cost and the fold's
    # re-derivation read the same made-up rates, so no live rate — list or
    # refreshed — can move either side.
    model = "acme/grommet-7"
    rates = {"fresh": 0.21, "create_5m": 0.21, "create_1h": 0.21,
             "read": 0.021, "output": 0.71}
    monkeypatch.setitem(pricing.MODEL_RATES, model, rates)
    stored = _cost(model, fresh=2_000_000, output=500_000)
    out = fold_per_model(
        [_row(model, None, 0, 2_000_000, 500_000, stored)],
        pair_bounds={(model, ""): []})
    assert len(out) == 1
    m = out[0]
    # One rounded bucket quantity each: 5e-5 of 4-decimal rounding, and
    # abs=1e-4 is twice that. A re-derived round(want, 4) would not do:
    # these expressions are value-shaped, not the fold's own accumulation,
    # so a midpoint straddle could tip its rounding the other way.
    assert m["cost_buckets"]["fresh"] == pytest.approx(
        2 * rates["fresh"], abs=1e-4)
    assert m["cost_buckets"]["output"] == pytest.approx(
        0.5 * rates["output"], abs=1e-4)
    assert m["estimated_rate"] is False


def _load_selection():
    """Import scripts/ci/refresh_selection.py by path (scripts/ci is not a
    package); the module puts its own directory on sys.path to reach
    refresh_prices."""
    path = ROOT / "scripts" / "ci" / "refresh_selection.py"
    spec = importlib.util.spec_from_file_location("refresh_selection", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["refresh_selection"] = module
    spec.loader.exec_module(module)
    return module


def test_a_synthetic_resolve_entry_loads_through_the_override_reader():
    pin = {"tag": "fixture/fp8", "select": "cheapest",
           "ignore": ["max_completion_tokens"], "why": "synthetic listing artifact"}
    key, host = "acme/synthetic-9", "Fixture"
    doc = seed_doc(tracked={key: {"id": key, "resolve": {host: pin}}})
    assert pricing.load_tables(doc)["MODEL_RATES"]
    reader = _load_selection()._override  # pylint: disable=protected-access
    override = reader(f"{key} via {host}", doc["openrouter"]["models"][key]["resolve"][host])
    assert override == pin
