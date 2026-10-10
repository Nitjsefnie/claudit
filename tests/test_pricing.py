"""MODEL_RATES is the single source of truth for cost in this repo
(SV-PARSER-SPEC). If a rate changes, bump constants.PARSER_VERSION.

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


def test_opus_4_7_resolves_exact_with_all_five_fields():
    r = pricing.rate_for("claude-opus-4-7")  # sv-test-data: allow (structure: a row's rates are the five RATE_FIELDS, plus the optional web_search rate a provider row carries)
    assert set(r) in LIVE_RATE_KEY_SETS
    assert pricing.resolve("claude-opus-4-7").kind == "exact"  # sv-test-data: allow (structure: the existing exact table key is never removed or renamed)


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


def test_opus_5_5_resolves_exact_distinct_from_opus_5():
    """The live table check pins only exact-key resolution."""
    o55 = pricing.resolve("claude-opus-5-5")  # sv-test-data: allow (structure: exact key survives appends)
    assert o55.kind == "exact"
    assert o55.key == "claude-opus-5-5"
    assert set(o55.rates) in LIVE_RATE_KEY_SETS


def test_synthetic_versioned_rows_resolve_separately(monkeypatch):
    rows = {
        "claude-acme-fable-5": dict(zip(pricing.RATE_FIELDS, (1, 2, 3, 4, 5))),
        "claude-acme-fable-5-1": dict(zip(pricing.RATE_FIELDS, (6, 7, 8, 9, 10))),
        "claude-acme-opus-5": dict(zip(pricing.RATE_FIELDS, (11, 12, 13, 14, 15))),
        "claude-acme-opus-5-5": dict(zip(pricing.RATE_FIELDS, (16, 17, 18, 19, 20))),
    }
    for key, rates in rows.items():
        monkeypatch.setitem(pricing.MODEL_RATES, key, rates)

    for base, version in (("claude-acme-fable-5", "claude-acme-fable-5-1"),
                          ("claude-acme-opus-5", "claude-acme-opus-5-5")):
        older = pricing.resolve(base)
        newer = pricing.resolve(f"anthropic.{version}[1m]")
        assert older.key == base and older.rates is rows[base]
        assert newer.key == version and newer.rates is rows[version]
        assert older.rates is not newer.rates


def test_sonnet_5_5_resolves_exact_to_its_own_row():
    """Sonnet 5.5 carries its own row and never rides the Sonnet family
    fallback (issue #328): before the row landed it resolved kind='tier'
    with key=None. The live table check pins only exact-key resolution."""
    s55 = pricing.resolve("claude-sonnet-5-5")  # sv-test-data: allow (structure: exact key survives appends)
    assert s55.kind == "exact"
    assert s55.key == "claude-sonnet-5-5"
    assert set(s55.rates) in LIVE_RATE_KEY_SETS


def test_fable_5_1_does_not_misroute_to_fable_5(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Suffix resolution keeps each synthetic version on its own row."""
    fable_key = "claude-acme-fable-5"
    fable51_key = "claude-acme-fable-5-1"
    fable = dict(zip(pricing.RATE_FIELDS, (1, 2, 3, 4, 5)))
    fable51 = dict(zip(pricing.RATE_FIELDS, (6, 7, 8, 9, 10)))
    rows = {fable_key: fable, fable51_key: fable51}
    monkeypatch.setattr(pricing, "MODEL_RATES", rows)
    monkeypatch.setattr(pricing, "DATED_RATES", {})

    base = pricing.resolve(fable_key)
    version = pricing.resolve(f"{fable51_key}[1m]")
    assert base.key == fable_key and base.rates is fable
    assert version.key == fable51_key and version.rates is fable51
    assert base.rates is not version.rates


def test_unknown_fable_falls_back_to_current_generation():
    r = pricing.resolve("claude-fable-9")
    assert r.kind == "tier"
    # The family rows are tracked vendor rows since the migration: the
    # merged view's list price is the assertion's source.
    assert r.rates is pricing._list_rates("claude-fable-5-1")  # pylint: disable=protected-access


def test_unknown_opus_falls_back_to_current_generation():
    r = pricing.resolve("claude-opus-6")
    assert r.kind == "tier"
    assert r.rates is pricing._list_rates("claude-opus-5-5")  # pylint: disable=protected-access


def test_tier_fallback_follows_the_highest_table_version(monkeypatch):
    """A newer row moves its family's fallback with no second edit;
    a two-part version outranks its one-part prefix (5-5 > 5)."""
    newer = dict(pricing._list_rates("claude-opus-5-5"), fresh=3.00)  # pylint: disable=protected-access
    monkeypatch.setitem(pricing.MODEL_RATES, "claude-opus-10", newer)
    assert pricing._latest("opus") is newer  # pylint: disable=protected-access
    monkeypatch.delitem(pricing.MODEL_RATES, "claude-opus-10")
    assert pricing._latest("opus") is pricing._list_rates(  # pylint: disable=protected-access
        "claude-opus-5-5")


def test_opus_4_8_does_not_misroute_to_legacy_opus_4(
        monkeypatch: pytest.MonkeyPatch) -> None:
    legacy_key = "claude-acme-opus-4"
    modern_key = f"{legacy_key}-8"
    rows = {
        legacy_key: dict(zip(pricing.RATE_FIELDS, (1, 2, 3, 4, 5))),
        modern_key: dict(zip(pricing.RATE_FIELDS, (6, 7, 8, 9, 10))),
    }
    monkeypatch.setattr(pricing, "MODEL_RATES", rows)
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    modern = pricing.resolve(modern_key)
    legacy = pricing.resolve(legacy_key)
    assert modern.key == modern_key
    assert legacy.key == legacy_key
    assert modern.rates is rows[modern_key]
    assert legacy.rates is rows[legacy_key]
    assert modern.rates is not legacy.rates


def test_sonnet_4_5_resolves_exact_with_all_five_fields():
    r = pricing.rate_for("claude-sonnet-4-5")  # sv-test-data: allow (structure: a row's rates are the five RATE_FIELDS, plus the optional web_search rate a provider row carries)
    assert set(r) in LIVE_RATE_KEY_SETS
    assert pricing.resolve("claude-sonnet-4-5").kind == "exact"  # sv-test-data: allow (structure: the existing exact table key is never removed or renamed)


def test_haiku_4_5_resolves_exact_with_all_five_fields():
    r = pricing.rate_for("claude-haiku-4-5")  # sv-test-data: allow (structure: a row's rates are the five RATE_FIELDS, plus the optional web_search rate a provider row carries)
    assert set(r) in LIVE_RATE_KEY_SETS
    assert pricing.resolve("claude-haiku-4-5").kind == "exact"  # sv-test-data: allow (structure: the existing exact table key is never removed or renamed)


def test_unknown_model_falls_back_to_default():
    r = pricing.rate_for("claude-zzzz-9999")
    assert r == pricing.DEFAULT_RATES


def test_substring_order_does_not_misroute_4_7_to_4(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # Exact matching preserves both keys without comparing their values.
    shorter = "claude-acme-opus-4"
    longer = f"{shorter}-7"
    rows = {
        shorter: dict(zip(pricing.RATE_FIELDS, (1, 2, 3, 4, 5))),
        longer: dict(zip(pricing.RATE_FIELDS, (6, 7, 8, 9, 10))),
    }
    monkeypatch.setattr(pricing, "MODEL_RATES", rows)
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    long_result = pricing.resolve(longer)
    short_result = pricing.resolve(shorter)
    assert long_result.key == longer and long_result.rates is rows[longer]
    assert short_result.key == shorter and short_result.rates is rows[shorter]
    assert long_result.rates is not short_result.rates


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


def test_split_cache_charges_each_bucket_separately():
    r = pricing.rate_for("claude-sonnet-4-5")  # sv-test-data: allow (same algorithm and order: expected terms use this row's rates divided by one million)
    cost = pricing.compute_cost(
        "claude-sonnet-4-5",  # sv-test-data: allow (same algorithm and order: expected terms use this loaded row's rates divided by one million)
        fresh=0, output=0,
        eph5=1, eph1h=1,
        unsplit_create=0, read=0,
    )
    # Same rates and operation order as compute_cost, scaled per million.
    assert cost == pytest.approx(
        r["create_5m"] / 1_000_000 + r["create_1h"] / 1_000_000,
        rel=1e-12)


def test_expired_windows_keep_pricing_their_own_period():
    """An expired window is NOT dead weight. Every PARSER_VERSION bump
    reparses the whole bucket, and a record from inside the window must
    come out at the price that was in force then — dropping the window
    would silently reprice history at list on the next reparse. The glm
    row is a tracked vendor row since the migration, so the boundary and
    both sides' rates read from its provider-row windows."""
    windows = pricing.PROVIDER_DATED_RATES[("glm-5-3-flash", "Z.AI")]
    cutover, window_rates = windows[-1]
    listed = pricing.PROVIDER_RATES[("glm-5-3-flash", "Z.AI")]
    before = pricing.compute_cost(
        "glm-5-3-flash", fresh=1, output=0, eph5=0, eph1h=0,  # sv-test-data: allow (same algorithm and order: one token uses the runtime window's fresh rate divided by one million)
        unsplit_create=0, read=0, ts=cutover - timedelta(seconds=1),
    )
    after = pricing.compute_cost(
        "glm-5-3-flash", fresh=1, output=0, eph5=0, eph1h=0,  # sv-test-data: allow (same algorithm and order: one token uses the runtime list rate divided by one million)
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


def test_dated_window_applies_before_its_cutover(synthetic_dated_rate):
    w = synthetic_dated_rate
    assert pricing.rate_for(w.model, ts=datetime(2026, 7, 21, tzinfo=UTC)) == w.before


def test_list_rates_apply_from_the_cutover(synthetic_dated_rate):
    w = synthetic_dated_rate
    assert pricing.rate_for(w.model, ts=w.cutover) == w.after


def test_dated_window_boundary_is_exclusive_at_the_cutover(synthetic_dated_rate):
    w = synthetic_dated_rate
    last = pricing.rate_for(w.model, ts=datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC))
    assert last == w.before
    assert pricing.rate_for(w.model, ts=datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)) == w.after


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
    r = pricing.resolve("claude-sonnet-9", ts=datetime(2026, 7, 21, tzinfo=UTC))
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


def test_provider_row_before_its_start_prices_by_the_model_alone(
        synthetic_provider_dated_rate):
    """A row that begins at a time does not exist for a record before it."""
    w = synthetic_provider_dated_rate
    r = pricing.resolve(w.model, ts=w.start - timedelta(seconds=1),
                        provider=w.host)
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


# --- OpenRouter variant suffixes fold to the bare id (issue 72) -------------
# A variant suffix (:nitro, :floor) is a service tier, not a price: the
# tiered id must resolve to the bare model's provider row, not fall to the
# default estimate. Only :free changes price (zero), and resolve() prices
# it before the provider lookup. Exercised through the synthetic provider
# row, like every provider behaviour above.


def test_nitro_variant_suffix_resolves_to_the_bare_provider_row(
        synthetic_provider_dated_rate):
    w = synthetic_provider_dated_rate
    r = pricing.resolve(f"{w.model}:nitro", ts=w.cutover, provider=w.host)
    assert (r.kind, r.key) == ("exact", w.model)
    assert r.rates == w.after
    assert r.rates != pricing.FREE_RATES


def test_floor_variant_suffix_resolves_to_the_bare_provider_row(
        synthetic_provider_dated_rate):
    w = synthetic_provider_dated_rate
    r = pricing.resolve(f"{w.model}:floor", ts=w.cutover, provider=w.host)
    assert (r.kind, r.key) == ("exact", w.model)
    assert r.rates == w.after
    assert r.rates != pricing.FREE_RATES


def test_free_suffix_keeps_its_zero_price_and_its_own_key(
        synthetic_provider_dated_rate):
    """The free match outranks the provider lookup, so a :free id returns
    the free rates under its own key — the variant fold must not touch it."""
    w = synthetic_provider_dated_rate
    r = pricing.resolve(f"{w.model}:free", ts=w.cutover, provider=w.host)
    assert (r.kind, r.key) == ("exact", f"{w.model}:free")
    assert r.rates == pricing.FREE_RATES


def test_uppercase_free_suffix_keeps_its_zero_price_and_its_own_key(
        synthetic_provider_dated_rate):
    """The free guard is case-insensitive: ':FREE' resolves exactly like
    ':free' — the free rates, exact, under the normalised (lowercase)
    key — never the provider row's priced rates under a mixed-case key."""
    w = synthetic_provider_dated_rate
    r = pricing.resolve(f"{w.model}:FREE", ts=w.cutover, provider=w.host)
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


def test_an_exact_variant_row_wins_over_the_bare_fold(
        synthetic_provider_dated_rate, monkeypatch):
    """The fold is a fallback: when the table holds the variant id itself,
    that row is the match. Pins the candidate order (exact id first)."""
    w = synthetic_provider_dated_rate
    monkeypatch.setitem(pricing.PROVIDER_RATES,
                        (f"{w.model}:nitro", w.host), w.after)
    r = pricing.resolve(f"{w.model}:nitro", ts=w.cutover, provider=w.host)
    assert (r.kind, r.key) == ("exact", f"{w.model}:nitro")
    assert r.rates == w.after


# --- resolution robustness -------------------------------------------------


def test_future_opus_uses_the_current_opus_tier_fallback():
    # 'claude-opus-4' is a prefix of 'claude-opus-4-9', but the unknown
    # future model must use the family fallback rather than an exact row.
    r = pricing.resolve("claude-opus-4-9")
    assert r.kind == "tier"
    assert r.rates is pricing._list_rates("claude-opus-5-5")  # pylint: disable=protected-access


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


def test_provider_prefixed_and_dotted_ids_normalise_to_the_exact_key(
        monkeypatch: pytest.MonkeyPatch) -> None:
    key = "claude-acme-opus-4-8"
    rates = dict(zip(pricing.RATE_FIELDS, (1, 2, 3, 4, 5)))
    monkeypatch.setattr(pricing, "MODEL_RATES", {key: rates})
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    for variant in (
        f"anthropic/{key.replace('-', '.')}".replace(".acme.", "-acme-"),
        f"us.anthropic.{key}",
        f"eu.anthropic.{key}",
        key.upper(),
    ):
        assert pricing.rate_for(variant) is rates, variant


def test_resolve_reports_exact_match():
    assert pricing.resolve("claude-opus-4-8").kind == "exact"  # sv-test-data: allow (structure-only: claude-opus-4-8 is an existing exact model key)


def test_resolve_reports_tier_fallback_for_unknown_claude_model():
    res = pricing.resolve("claude-sonnet-6")
    assert res.kind == "tier"
    # Current-generation Sonnet rates, whatever they are today — read from
    # the merged view, since the family rows are tracked vendor rows now.
    assert res.rates == pricing._list_rates("claude-sonnet-5-5")  # pylint: disable=protected-access


def test_resolve_reports_default_for_wholly_unknown_model():
    # A synthetic id: no rate row the refresh can add may ever match it,
    # so "wholly unknown" stays true against refreshed data (issue #824).
    assert pricing.resolve("totally-unknown-model").kind == "default"


def test_fast_variants_are_not_silently_billed_at_standard_rates():
    # Fast mode is premium-priced and we have no published rate for it;
    # resolve must flag it rather than pass it off as an exact match.
    assert pricing.resolve("claude-opus-4-8-fast").kind != "exact"


# --- GLM-5.3-Flash (Z.ai) ---------------------------------------------------
# The only non-Anthropic model in the table: cache WRITES are free and
# reads are 0.2x input, so the 1.25x/2x/0.1x Anthropic relations do not
# hold. Launch promotion is 50% off list through 2026-09-09 16:00 UTC.

GLM_CUTOVER = datetime(2026, 9, 9, 16, 0, tzinfo=UTC)
GLM_LIST = {"fresh": 0.15, "create_5m": 0.00, "create_1h": 0.00,
            "read": 0.03, "output": 0.50}
GLM_PROMO = {"fresh": 0.075, "create_5m": 0.00, "create_1h": 0.00,
             "read": 0.015, "output": 0.25}


def test_glm_flash_resolves_exact_despite_dots_and_suffixes(
        monkeypatch: pytest.MonkeyPatch) -> None:
    rates = dict(zip(pricing.RATE_FIELDS, (1, 2, 3, 4, 5)))
    monkeypatch.setattr(pricing, "MODEL_RATES", {"glm-5-3-flash": rates})
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    for model in ("glm-5.3-flash", "GLM-5.3-Flash[1m]"):
        result = pricing.resolve(model)
        assert (result.kind, result.key) == ("exact", "glm-5-3-flash")
        assert result.rates is rates


def test_glm_flash_promo_window_ends_exclusive_at_utc8_midnight():
    just_before = GLM_CUTOVER - timedelta(seconds=1)
    assert pricing.rate_for("glm-5.3-flash", ts=just_before) == GLM_PROMO  # sv-test-data: allow (closed-window pin at the GLM promo boundary)
    assert pricing.rate_for("glm-5.3-flash", ts=GLM_CUTOVER) == GLM_LIST  # sv-test-data: allow (closed-window pin at the GLM promo boundary)


def test_glm_flash_cache_writes_cost_nothing():
    # 1M each of 5m writes, 1h writes and unsplit legacy writes: all free.
    cost = pricing.compute_cost(
        "glm-5.3-flash",  # sv-test-data: allow (closed-window pin at the GLM promo boundary)
        fresh=0, output=0,
        eph5=1_000_000, eph1h=1_000_000, unsplit_create=1_000_000, read=0,
        ts=GLM_CUTOVER - timedelta(seconds=1),
    )
    assert cost == 0.0


def test_glm_flash_compute_cost_promo_known_vector():
    cost = pricing.compute_cost(
        "glm-5.3-flash",  # sv-test-data: allow (closed-window pin at the GLM promo boundary)
        fresh=2_000_000, output=1_000_000,
        eph5=0, eph1h=0, unsplit_create=0, read=4_000_000,
        ts=GLM_CUTOVER - timedelta(seconds=1),
    )
    # 2M input @ 0.075 + 1M output @ 0.25 + 4M cached reads @ 0.015
    assert abs(cost - (2 * 0.075 + 0.25 + 4 * 0.015)) < 1e-9


# --- OpenRouter free and stealth lanes (zero-priced) ------------------------
# An OpenRouter id ending in `:free` or starting with `stealth/` is a $0
# model, and the id list churns weekly, so the match is on the id's SHAPE
# rather than an enumerated table row. kind is EXACT so the deliberate
# zero is not flagged as an estimate (same reasoning as bonsai-2-27b).

FREE_MODELS = (
    "thinkingmachines/inkling:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
)


def test_openrouter_free_suffix_prices_at_zero():
    for model in FREE_MODELS:
        r = pricing.resolve(model)
        assert (r.kind, r.key) == ("exact", model)
        assert r.rates == pricing.FREE_RATES
        assert all(v == 0 for v in r.rates.values())
        assert r.estimated is False


def test_openrouter_stealth_prefix_prices_at_zero():
    r = pricing.resolve("stealth/space-bunny-alpha")
    assert (r.kind, r.key) == ("exact", "stealth/space-bunny-alpha")
    assert r.rates == pricing.FREE_RATES
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
