"""MODEL_RATES is the single source of truth for cost in this repo
(SV-PARSER-SPEC): a rate move bumps PRICING_VERSION.

Per SV-TEST-DATA, assertions read the rates they need from the loaded
tables at run time; what is pinned here is the resolution and pricing
ARITHMETIC, never a committed rate value.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend import pricing
from tests.refresh_fixture_builders import LIVE_RATE_KEY_SETS, seed_doc

UTC = timezone.utc


@pytest.mark.parametrize("model", [
    "claude-opus-4-7", "claude-sonnet-4-5", "claude-haiku-4-5",
])
def test_live_model_resolves_exact_with_all_five_fields(model):
    r = pricing.rate_for(model)
    assert set(r) in LIVE_RATE_KEY_SETS
    assert pricing.resolve(model).kind == "exact"


def test_fable_5_suffixes_fold_to_its_own_row(
        monkeypatch: pytest.MonkeyPatch) -> None:
    key = "claude-acme-fable-5"
    rates = dict(zip(pricing.RATE_FIELDS, (1, 2, 3, 4, 5)))
    monkeypatch.setattr(pricing, "MODEL_RATES", {key: rates})
    monkeypatch.setattr(pricing, "DATED_RATES", {})

    base = pricing.resolve(key)
    suffixed = pricing.resolve(f"{key}[1m]")
    assert base.kind == suffixed.kind == "exact"
    assert base.key == suffixed.key == key
    assert base.rates is suffixed.rates is rates


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5"])
def test_live_versioned_model_resolves_exact_to_its_own_row(model):
    r = pricing.resolve(model)
    assert r.kind == "exact"
    assert r.key == model
    assert set(r.rates) in LIVE_RATE_KEY_SETS


@pytest.mark.parametrize("base,version,probe,first_rate", [
    ("claude-acme-fable-5", "claude-acme-fable-5-1",
     "anthropic.claude-acme-fable-5-1[1m]", 1),
    ("claude-acme-opus-5", "claude-acme-opus-5-5",
     "anthropic.claude-acme-opus-5-5[1m]", 11),
    ("claude-acme-fable-5", "claude-acme-fable-5-1",
     "claude-acme-fable-5-1[1m]", 1),
    ("claude-acme-opus-4", "claude-acme-opus-4-8", "claude-acme-opus-4-8", 1),
    ("claude-acme-opus-4", "claude-acme-opus-4-7", "claude-acme-opus-4-7", 1),
])
def test_synthetic_versioned_rows_resolve_separately(
        monkeypatch, base, version, probe, first_rate):
    rows = {
        base: dict(zip(pricing.RATE_FIELDS, range(first_rate, first_rate + 5))),
        version: dict(zip(pricing.RATE_FIELDS, range(first_rate + 5, first_rate + 10))),
    }
    monkeypatch.setattr(pricing, "MODEL_RATES", rows)
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    older = pricing.resolve(base)
    newer = pricing.resolve(probe)
    assert older.key == base and older.rates is rows[base]
    assert newer.key == version and newer.rates is rows[version]
    assert older.rates is not newer.rates


@pytest.mark.parametrize("model,families", [
    ("claude-fable-99", ("fable", "mythos")),
    ("claude-opus-99", ("opus",)),
    ("claude-opus-4-99", ("opus",)),
])
def test_unknown_model_uses_the_current_family_tier_fallback(model, families):
    r = pricing.resolve(model)
    assert r.kind == "tier"
    assert r.rates is pricing._latest(*families)  # pylint: disable=protected-access


def test_tier_fallback_follows_the_highest_table_version(monkeypatch):
    """A newer row moves its family's fallback with no second edit;
    a two-part version outranks its one-part prefix (5-5 > 5)."""
    newer = dict(pricing._list_rates("claude-opus-5-5"), fresh=3.00)  # pylint: disable=protected-access
    monkeypatch.setitem(pricing.MODEL_RATES, "claude-opus-10", newer)
    assert pricing._latest("opus") is newer  # pylint: disable=protected-access
    monkeypatch.delitem(pricing.MODEL_RATES, "claude-opus-10")
    assert pricing._latest("opus") is pricing._list_rates(  # pylint: disable=protected-access
        "claude-opus-5-5")


def test_compute_cost_prices_fresh_at_the_fresh_rate():
    fresh = pricing.rate_for("claude-opus-4-7")["fresh"]  # sv-test-data: allow (same algorithm and order: the expected term uses the loaded row's fresh rate)
    cost = pricing.compute_cost(
        "claude-opus-4-7",  # sv-test-data: allow (same algorithm and order: both sides multiply the same rate by one token then divide by one million)
        fresh=1, output=0, eph5=0, eph1h=0, unsplit_create=0, read=0,
    )
    assert cost == pytest.approx(fresh / 1_000_000, rel=1e-12)


def test_compute_cost_applies_grouped_adjustments(
        monkeypatch: pytest.MonkeyPatch) -> None:
    model = "acme/adjustments-9"
    rates: dict[str, float] = dict.fromkeys(pricing.RATE_FIELDS, 0.0)
    rates.update({"fresh": 1.0, "output": 2.0, "web_search": 0.25})
    monkeypatch.setattr(pricing, "MODEL_RATES", {model: rates})
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS", {
        model: {"input_mult": 2.0, "output_mult": 3.0},
    })

    cost = pricing.compute_cost(
        model, fresh=1_000_000, output=1_000_000, eph5=0, eph1h=0,
        unsplit_create=0, read=0,
        adjustments=pricing.CostAdjustments(
            long_context=True, web_search_requests=2),
    )

    assert cost == pytest.approx(8.5)


def test_unsplit_cache_charges_at_1h_rate(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A cache write with no declared TTL is priced as the 1h tier.

    Measured over the corpus: main sessions write 98.7% of their cache at
    1h, and 96% of all 5m writes come from subagents. 1h is the norm an
    undeclared write should assume, and a token-plan provider (Kimi,
    Codex, Z.ai) has every reason to keep its cache long.
    """
    model = "acme/ttl-9"
    rates = dict(zip(pricing.RATE_FIELDS, (1.0, 2.0, 4.0, 0.5, 8.0)))
    monkeypatch.setattr(pricing, "MODEL_RATES", {model: rates})
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    cost = pricing.compute_cost(
        model, fresh=0, output=0, eph5=0, eph1h=0, unsplit_create=1, read=0,
    )
    assert cost == pytest.approx(rates["create_1h"] / 1_000_000)
    assert rates["create_1h"] != rates["create_5m"]


def test_expired_windows_keep_pricing_their_own_period(synthetic_glm_history):
    """An expired window is NOT dead weight. Every PARSER_VERSION bump
    reparses the whole bucket, and a record from inside a retained window
    must price by that window. The synthetic vendor history keeps this
    check independent of the refresh's corrections to live rows."""
    windows = pricing.PROVIDER_DATED_RATES[(SYNTH_GLM, "SyntheticVendor")]
    cutover, window_rates = windows[-1]
    listed = pricing.PROVIDER_RATES[(SYNTH_GLM, "SyntheticVendor")]
    before = pricing.compute_cost(
        SYNTH_GLM, fresh=1, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=cutover - timedelta(seconds=1),
    )
    after = pricing.compute_cost(
        SYNTH_GLM, fresh=1, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=cutover,
    )
    # rel=1e-12: the same window's rates on both sides; the per-million
    # scaling can cost the term one last-bit rounding, never a real one.
    assert before == pytest.approx(window_rates["fresh"] / 1_000_000, rel=1e-12)
    assert after == pytest.approx(listed["fresh"] / 1_000_000, rel=1e-12)


def test_rate_epochs_match_the_dated_windows():
    assert pricing.RATE_EPOCHS == sorted(
        {end for w in pricing.DATED_RATES.values() for end, _ in w}
        | {end for w in pricing.PROVIDER_DATED_RATES.values() for end, _ in w}
        | set(pricing.PROVIDER_STARTS.values())
    )


# --- dated-rate machinery (exercised via a synthetic window) ---------------
# SV-DATED-RATES outlives any individual promotion, so these drive the code
# path through conftest's synthetic_dated_rate rather than a live entry.


@pytest.mark.parametrize("ts,expected", [
    (datetime(2026, 7, 21, tzinfo=UTC), "before"),
    (datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC), "before"),
    (datetime(2026, 9, 1, tzinfo=UTC), "after"),
])
def test_dated_window_boundary_is_exclusive_at_the_cutover(
        synthetic_dated_rate, ts, expected):
    w = synthetic_dated_rate
    assert pricing.rate_for(w.model, ts=ts) == getattr(w, expected)


def test_omitting_ts_yields_list_price_never_the_discount(synthetic_dated_rate):
    # Conservative: an unknown timestamp must never silently apply a promo.
    assert pricing.rate_for(synthetic_dated_rate.model) == synthetic_dated_rate.after


def test_dated_window_does_not_leak_to_other_models(synthetic_dated_rate):
    assert synthetic_dated_rate.model not in ("claude-opus-4-8", "claude-fable-5",
                                              "claude-haiku-4-5")
    for m in ("claude-opus-4-8", "claude-fable-5", "claude-haiku-4-5"):
        # The expectation is computed by the same algorithm over the same
        # tables at the same instant (SV-TEST-DATA): m prices from m's OWN
        # row alone — the fixture's window prices its model, never these —
        # which holds whatever the rows' appended history looks like.
        for ts in (datetime(2026, 7, 21, tzinfo=UTC),
                   datetime(2026, 9, 1, tzinfo=UTC), None):
            expected = pricing._in_window(pricing._key_windows(m), ts,  # pylint: disable=protected-access
                                          pricing._list_rates(m))  # pylint: disable=protected-access
            assert pricing.rate_for(m, ts=ts) == expected


def test_tier_fallback_never_inherits_a_dated_promotion(monkeypatch):
    # An unrecognised sonnet falls back to the current-generation row's
    # LIST rates, not its promotional ones, even inside the window. The
    # family rows are tracked vendor rows since the migration, so the
    # promo hangs on the vendor row the fallback prices through — found
    # here by identity, so a new Sonnet release moves the test with it.
    fallback = pricing._latest("sonnet")  # pylint: disable=protected-access
    row = next(row for row, v in pricing.PROVIDER_RATES.items()
               if v is fallback)
    cutover = datetime(2026, 9, 1, tzinfo=UTC)
    promo = {"fresh": 9.00, "create_5m": 11.25, "create_1h": 18.00,
             "read": 0.90, "output": 45.00}
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES",
                        {row: [(cutover, promo)]})
    monkeypatch.setattr(pricing, "RATE_EPOCHS", [cutover])
    r = pricing.resolve("claude-sonnet-99", ts=datetime(2026, 7, 21, tzinfo=UTC))
    assert r.kind == "tier"
    assert r.rates == fallback


def test_rate_epochs_are_exposed_sorted_for_read_time_grouping(synthetic_dated_rate):
    assert pricing.RATE_EPOCHS == [synthetic_dated_rate.cutover]


# --- provider rows (exercised via a synthetic row) --------------------------
# SV-PROVIDER-RATES and SV-DATED-RATES on the provider half of the table.
# The live provider rows are single-entry today (no moves, no first-seen
# hosts since the table was seeded), so these drive the machinery through
# conftest's synthetic_provider_dated_rate rather than a live entry — the
# same reason the model side uses synthetic_dated_rate above.


def _synthetic_provider_doc(w):
    """A pricing.json-shaped document carrying only the synthetic row
    (plus the claude-opus-4-7 row the default estimate needs, at no
    cutover)."""
    def entry(rates, frm):
        return {"from": frm, **{f: rates[f] for f in pricing.RATE_FIELDS}}
    return seed_doc(
        models={"claude-opus-4-7": [entry(w.after, None)]},
        providers={w.model: {w.host: [
            entry(w.before, w.start.isoformat()),
            entry(w.after, w.cutover.isoformat()),
        ]}},
        fetched=w.cutover.isoformat(),
    )


def test_rate_epochs_include_provider_window_ends_and_row_starts(
        synthetic_provider_dated_rate):
    """RATE_EPOCHS is the union the read-time fold groups by, and the
    provider half of the table contributes its window ends AND row starts
    to it. The union is built once at import from the live file, so this
    calls load_tables on a made-up doc: the synthetic instants can never
    appear in the module global, and patching RATE_EPOCHS would make the
    assertion true by construction."""
    tables = pricing.load_tables(_synthetic_provider_doc(
        synthetic_provider_dated_rate))
    assert tables["RATE_EPOCHS"] == [synthetic_provider_dated_rate.start,
                                     synthetic_provider_dated_rate.cutover]


def test_provider_window_applies_before_its_cutover(
        synthetic_provider_dated_rate):
    w = synthetic_provider_dated_rate
    assert pricing.rate_for(
        w.model, ts=w.cutover - timedelta(seconds=1), provider=w.host,
    ) == w.before
    assert pricing.resolve(
        w.model, ts=w.cutover - timedelta(seconds=1), provider=w.host,
    ).kind == "exact"


def test_provider_list_rates_apply_from_the_cutover(
        synthetic_provider_dated_rate):
    w = synthetic_provider_dated_rate
    assert pricing.rate_for(w.model, ts=w.cutover, provider=w.host) == w.after
    assert pricing.rate_for(
        w.model, ts=w.cutover.replace(year=2027), provider=w.host,
    ) == w.after


def test_provider_row_without_a_timestamp_yields_list_price(
        synthetic_provider_dated_rate):
    # Conservative: an unknown timestamp must never silently apply a promo.
    assert pricing.rate_for(
        synthetic_provider_dated_rate.model,
        provider=synthetic_provider_dated_rate.host,
    ) == synthetic_provider_dated_rate.after


def test_provider_record_without_a_provider_prices_by_the_model_alone(
        synthetic_provider_dated_rate):
    w = synthetic_provider_dated_rate
    r = pricing.resolve(w.model, ts=w.cutover - timedelta(seconds=1))
    assert (r.kind, r.rates) == ("default", pricing.DEFAULT_RATES)


def test_provider_compute_cost_splits_at_the_cutover(
        synthetic_provider_dated_rate):
    w = synthetic_provider_dated_rate

    def cost(ts):
        # Spelled out, not **kwargs: a dict[str, int] splat lets pyright
        # bind an int to compute_cost's bool long_context and fails types.
        return pricing.compute_cost(
            w.model, fresh=1_000_000, output=0, eph5=0, eph1h=0,
            unsplit_create=0, read=0, ts=ts,
            res=pricing.resolve(w.model, ts, w.host))

    just_before = cost(w.cutover - timedelta(seconds=1))
    at = cost(w.cutover)
    assert (just_before, at) == (w.before["fresh"], w.after["fresh"])


# --- OpenRouter variant suffixes fold to the bare id -------------
# A variant suffix (:nitro,:floor) is a service tier, not a price: the
# tiered id must resolve to the bare model's provider row, not fall to the
# default estimate. Only:free changes price (zero), and resolve() prices
# it before the provider lookup. Exercised through the synthetic provider
# row, like every provider behaviour above.


@pytest.mark.parametrize("suffix,exact_row", [
    ("nitro", False), ("floor", False), ("nitro", True),
])
def test_variant_suffix_resolves_to_the_matching_provider_row(
        synthetic_provider_dated_rate, monkeypatch, suffix, exact_row):
    w = synthetic_provider_dated_rate
    model = f"{w.model}:{suffix}"
    if exact_row:
        monkeypatch.setitem(pricing.PROVIDER_RATES, (model, w.host), w.after)
    r = pricing.resolve(model, ts=w.cutover, provider=w.host)
    assert (r.kind, r.key) == ("exact", model if exact_row else w.model)
    assert r.rates == w.after
    assert r.rates != pricing.FREE_RATES


@pytest.mark.parametrize("suffix", ["free", "FREE"])
def test_free_suffix_keeps_its_zero_price_and_its_own_key(
        synthetic_provider_dated_rate, suffix):
    w = synthetic_provider_dated_rate
    r = pricing.resolve(f"{w.model}:{suffix}", ts=w.cutover, provider=w.host)
    assert (r.kind, r.key) == ("exact", f"{w.model}:free")
    assert r.rates == pricing.FREE_RATES


def test_variant_suffix_prices_by_the_bare_row_dated_window(
        synthetic_provider_dated_rate):
    """The fold looks up the bare row, so the record prices by that row's
    windows and start: inside the window before the cutover, list from the
    cutover on, and by the model alone before the row begins."""
    w = synthetic_provider_dated_rate
    model, host = f"{w.model}:nitro", w.host
    assert pricing.rate_for(
        model, ts=w.cutover - timedelta(seconds=1), provider=host) == w.before
    assert pricing.rate_for(model, ts=w.cutover, provider=host) == w.after
    r = pricing.resolve(model, ts=w.start - timedelta(seconds=1),
                        provider=host)
    assert (r.kind, r.rates) == ("default", pricing.DEFAULT_RATES)


def test_dated_snapshot_still_matches_its_generic_key(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A dated-snapshot suffix folds to its isolated synthetic base row."""
    opus = dict(zip(pricing.RATE_FIELDS, (1, 2, 3, 4, 5)))
    haiku = dict(zip(pricing.RATE_FIELDS, (6, 7, 8, 9, 10)))
    opus_key = "claude-acme-opus-4"
    haiku_key = "claude-acme-haiku-4-5"
    rows = {opus_key: opus, haiku_key: haiku}
    monkeypatch.setattr(pricing, "MODEL_RATES", rows)
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    assert pricing.rate_for(f"{opus_key}-20250514") is opus
    assert pricing.rate_for(opus_key) is opus
    assert pricing.rate_for(f"{haiku_key}-20251001") is haiku
    assert pricing.rate_for(haiku_key) is haiku


@pytest.mark.parametrize("key,variants", [
    ("claude-acme-opus-4-8", (
        "anthropic/claude-acme-opus.4.8", "us.anthropic.claude-acme-opus-4-8",
        "eu.anthropic.claude-acme-opus-4-8", "CLAUDE-ACME-OPUS-4-8",
    )),
    ("glm-5-3-flash", ("glm-5.3-flash", "GLM-5.3-Flash[1m]")),
])
def test_spelling_variants_normalise_to_the_exact_key(monkeypatch, key, variants):
    rates = dict(zip(pricing.RATE_FIELDS, (1, 2, 3, 4, 5)))
    monkeypatch.setattr(pricing, "MODEL_RATES", {key: rates})
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    for variant in variants:
        result = pricing.resolve(variant)
        assert (result.kind, result.key) == ("exact", key)
        assert result.rates is rates
        assert pricing.rate_for(variant) is rates, variant


def test_fast_variants_are_not_silently_billed_at_standard_rates():
    # Fast mode is premium-priced and we have no published rate for it;
    # resolve must flag it rather than pass it off as an exact match.
    assert pricing.resolve("claude-opus-4-8-fast").kind != "exact"


GLM_CUTOVER = datetime(2026, 9, 10, 2, 28, 4, tzinfo=UTC)
GLM_LIST = {"fresh": 0.15, "create_5m": 0.15, "create_1h": 0.15,
            "read": 0.03, "output": 0.50}
GLM_PROMO = {"fresh": 0.075, "create_5m": 0.075, "create_1h": 0.075,
             "read": 0.015, "output": 0.25}
SYNTH_GLM = "glm-acme-9"


@pytest.fixture(name="synthetic_glm_history")
def _synthetic_glm_history_fixture(monkeypatch):
    changes = [(2, 28, 4, GLM_LIST), (2, 30, 55, GLM_PROMO),
               (6, 5, 57, GLM_LIST), (6, 12, 51, GLM_PROMO),
               (9, 38, 44, GLM_LIST)]
    history = [{"from": None, **GLM_PROMO}, *[
        {"from": datetime(2026, 9, 10, h, m, s, tzinfo=UTC).isoformat(), **rates}
        for h, m, s, rates in changes]]
    doc = seed_doc(
        tracked={SYNTH_GLM: {"id": f"z-ai/{SYNTH_GLM}", "vendor_host": "SyntheticVendor"}},
        providers={SYNTH_GLM: {"SyntheticVendor": history}})
    for name, value in pricing.load_tables(doc).items():
        monkeypatch.setattr(pricing, name, value)


@pytest.mark.parametrize("hour,minute,second,before,after", [
    (2, 28, 4, GLM_PROMO, GLM_LIST), (2, 30, 55, GLM_LIST, GLM_PROMO),
    (6, 5, 57, GLM_PROMO, GLM_LIST), (6, 12, 51, GLM_LIST, GLM_PROMO),
    (9, 38, 44, GLM_PROMO, GLM_LIST),
])
def test_glm_flash_promo_windows_end_exclusive_at_logged_instants(
        synthetic_glm_history, hour, minute, second, before, after):
    cutover = datetime(2026, 9, 10, hour, minute, second, tzinfo=UTC)
    assert pricing.rate_for(SYNTH_GLM, ts=cutover - timedelta(seconds=1)) == before
    assert pricing.rate_for(SYNTH_GLM, ts=cutover) == after


@pytest.mark.parametrize("offset,fresh,output,read,writes,want,max_error", [
    (-1, 0, 0, 0, 1_000_000, 0.225, 0.225e-6),
    (0, 0, 0, 0, 1_000_000, 0.45, 0.45e-6),
    (-1, 2_000_000, 1_000_000, 4_000_000, 0,
     2 * 0.075 + 0.25 + 4 * 0.015, 1e-9),
])
def test_glm_flash_compute_cost_known_vectors(
        synthetic_glm_history, offset, fresh, output, read, writes, want, max_error):
    cost = pricing.compute_cost(
        SYNTH_GLM,
        fresh=fresh, output=output, eph5=writes, eph1h=writes,
        unsplit_create=writes, read=read, ts=GLM_CUTOVER + timedelta(seconds=offset),
    )
    assert cost == pytest.approx(want)
    assert abs(cost - want) < max_error


# --- OpenRouter free and stealth lanes (zero-priced) ------------------------
# An OpenRouter id ending in `:free` or starting with `stealth/` is a $0
# model, and the id list churns weekly, so the match is on the id's SHAPE
# rather than an enumerated table row. kind is EXACT so the deliberate
# zero is not flagged as an estimate (same reasoning as bonsai-2-27b).

FREE_MODELS = (
    "thinkingmachines/inkling:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
)


@pytest.mark.parametrize("model", (*FREE_MODELS, "stealth/space-bunny-alpha"))
def test_openrouter_free_shapes_price_at_zero(model):
    r = pricing.resolve(model)
    assert (r.kind, r.key) == ("exact", model)
    assert r.rates == pricing.FREE_RATES
    assert all(v == 0 for v in r.rates.values())
    assert r.estimated is False


def test_free_models_cost_nothing():
    for model in (*FREE_MODELS, "stealth/space-bunny-alpha"):
        assert pricing.compute_cost(
            model, fresh=1_000_000, output=1_000_000,
            eph5=1_000_000, eph1h=1_000_000, unsplit_create=1_000_000,
            read=1_000_000,
        ) == 0


def test_free_match_survives_spelling_variants():
    """Case, whitespace and dot-folding must not dodge the match; the
    check runs on the raw id AND the normalised form, and it outranks an
    exact table key."""
    assert pricing.resolve("Stealth/Space-Bunny-Alpha").rates == pricing.FREE_RATES
    assert pricing.resolve(" NVIDIA/Nemotron-3:FREE ").rates == pricing.FREE_RATES
    # _normalise strips everything before 'claude', so only the raw-id
    # check still sees this id's stealth/ prefix — and the free match
    # must win over the claude-opus-4-8 key the normalised form hits.
    r = pricing.resolve("stealth/claude-opus-4-8")  # sv-test-data: allow (structural: the free match must outrank a live table key)
    assert (r.kind, r.rates) == ("exact", pricing.FREE_RATES)


def test_nonfree_openrouter_id_folds_to_its_tracked_bare_form():
    bare = "gpt-6-sol"
    model = f"openai/{bare}"
    tracked = pricing.VENDOR_BARE[bare]
    host = pricing.VENDOR_HOSTS[tracked]
    r = pricing.resolve(model)
    assert (r.kind, r.key) == ("exact", tracked)
    assert r.rates == pricing.PROVIDER_RATES[(tracked, host)]


def test_free_matching_does_not_touch_claude_ids():
    r = pricing.resolve("claude-opus-4-8")  # sv-test-data: allow (load-time identity: result.rates is the object stored at this loaded exact key)
    assert r.kind == "exact"
    assert r.rates is pricing._list_rates("claude-opus-4-8")  # pylint: disable=protected-access


def test_bonsai_resolves_exact_and_prices_by_its_own_row():
    """The local model remains an exact match routed to its own table row."""
    r = pricing.resolve("bonsai-2-27b")  # sv-test-data: allow (load-time identity: the result must reference the existing bonsai table entry)
    assert (r.kind, r.key) == ("exact", "bonsai-2-27b")
    own = pricing.rate_for("bonsai-2-27b")  # sv-test-data: allow (load-time identity: all three references point to one loaded row object)
    assert own is r.rates is pricing.MODEL_RATES["bonsai-2-27b"]
