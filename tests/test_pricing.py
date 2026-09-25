"""MODEL_RATES is the single source of truth for cost in this repo
(SV-PARSER-SPEC). If a rate changes, bump constants.PARSER_VERSION.
"""
from datetime import datetime, timedelta, timezone

from backend import pricing


def test_opus_4_7_rates_match_canonical():
    r = pricing.rate_for("claude-opus-4-7")
    assert r == {
        "fresh": 5.00, "create_5m": 6.25, "create_1h": 10.00,
        "read": 0.50, "output": 25.00,
    }


def test_fable_5_rates_are_double_opus_4x():
    f = pricing.rate_for("claude-fable-5")
    o = pricing.rate_for("claude-opus-4-8")
    assert f == {
        "fresh": 10.00, "create_5m": 12.50, "create_1h": 20.00,
        "read": 1.00, "output": 50.00,
    }
    assert all(f[k] == 2 * o[k] for k in f)
    # model ids carry suffixes like claude-fable-5[1m]
    assert pricing.rate_for("claude-fable-5[1m]") == f


def test_fable_5_1_cache_reads_are_a_quarter_of_fable_5():
    """Fable 5.1 / Mythos 5.1 price cache hits at 0.025x base input, not 0.1x."""
    f51 = pricing.rate_for("claude-fable-5-1")
    assert f51 == {
        "fresh": 10.00, "create_5m": 12.50, "create_1h": 20.00,
        "read": 0.25, "output": 50.00,
    }
    assert f51["read"] == f51["fresh"] * 0.025
    assert pricing.rate_for("claude-mythos-5-1") == f51
    assert pricing.rate_for("claude-fable-5-1[1m]") == f51


def test_opus_5_5_rates_and_cache_reads_at_5_percent():
    """Opus 5.5 is $4/$20 and prices cache hits at 0.05x base input."""
    o55 = pricing.rate_for("claude-opus-5-5")
    assert o55 == {
        "fresh": 4.00, "create_5m": 5.00, "create_1h": 8.00,
        "read": 0.20, "output": 20.00,
    }
    assert o55["read"] == o55["fresh"] * 0.05
    assert pricing.resolve("claude-opus-5-5").kind == "exact"
    assert pricing.rate_for("claude-opus-5-5[1m]") == o55
    assert pricing.rate_for("anthropic.claude-opus-5-5") == o55
    # must not fall through to Opus 5's $5/$25
    assert pricing.rate_for("claude-opus-5")["fresh"] == 5.00


def test_fable_5_1_does_not_misroute_to_fable_5():
    """The 0.1x read rate of Fable 5 would be a silent 4x overcount on 5.1."""
    assert pricing.resolve("claude-fable-5-1").kind == "exact"
    assert pricing.rate_for("claude-fable-5")["read"] == 1.00
    assert pricing.rate_for("claude-mythos-5")["read"] == 1.00


def test_unknown_fable_falls_back_to_current_generation():
    r = pricing.resolve("claude-fable-9")
    assert r.kind == "tier"
    assert r.rates is pricing.MODEL_RATES["claude-fable-5-1"]


def test_unknown_opus_falls_back_to_current_generation():
    r = pricing.resolve("claude-opus-6")
    assert r.kind == "tier"
    assert r.rates is pricing.MODEL_RATES["claude-opus-5-5"]


def test_tier_fallback_follows_the_highest_table_version(monkeypatch):
    """A newer row moves its family's fallback with no second edit;
    a two-part version outranks its one-part prefix (5-5 > 5)."""
    newer = dict(pricing.MODEL_RATES["claude-opus-5-5"], fresh=3.00)
    monkeypatch.setitem(pricing.MODEL_RATES, "claude-opus-10", newer)
    assert pricing._latest("opus") is newer  # pylint: disable=protected-access
    monkeypatch.delitem(pricing.MODEL_RATES, "claude-opus-10")
    assert pricing._latest("opus") is pricing.MODEL_RATES["claude-opus-5-5"]  # pylint: disable=protected-access


def test_opus_4_8_does_not_misroute_to_legacy_opus_4():
    r = pricing.rate_for("claude-opus-4-8")
    assert r["fresh"] == 5.00 and r["output"] == 25.00


def test_sonnet_4_5_rates():
    r = pricing.rate_for("claude-sonnet-4-5")
    assert r["fresh"] == 3.00 and r["create_5m"] == 3.75


def test_haiku_4_5_rates():
    r = pricing.rate_for("claude-haiku-4-5")
    assert r["fresh"] == 1.00 and r["read"] == 0.10


def test_unknown_model_falls_back_to_default():
    r = pricing.rate_for("claude-zzzz-9999")
    assert r == pricing.DEFAULT_RATES


def test_substring_order_does_not_misroute_4_7_to_4():
    r47 = pricing.rate_for("claude-opus-4-7")
    r4 = pricing.rate_for("claude-opus-4")
    assert r47["fresh"] == 5.00
    assert r4["fresh"] == 15.00


def test_compute_cost_split_known_vector():
    cost = pricing.compute_cost(
        "claude-opus-4-7",
        fresh=1_000_000, output=0, eph5=0, eph1h=0, unsplit_create=0, read=0,
    )
    assert cost == 5.00


def test_unsplit_cache_charges_at_1h_rate():
    """A cache write with no declared TTL is priced as the 1h tier.

    Measured over the corpus: main sessions write 98.7% of their cache at
    1h, and 96% of all 5m writes come from subagents. 1h is the norm an
    undeclared write should assume, and a token-plan provider (Kimi,
    Codex, Z.ai) has every reason to keep its cache long.
    """
    cost = pricing.compute_cost(
        "claude-sonnet-4-5",
        fresh=0, output=0, eph5=0, eph1h=0, unsplit_create=1_000_000, read=0,
    )
    assert cost == 6.00   # NOT 3.75 (5m rate)


def test_split_cache_charges_each_bucket_separately():
    cost = pricing.compute_cost(
        "claude-sonnet-4-5",
        fresh=0, output=0,
        eph5=1_000_000, eph1h=1_000_000,
        unsplit_create=0, read=0,
    )
    # 1M @ 3.75 + 1M @ 6.00
    assert cost == 9.75


# --- Claude Sonnet 5: flat pricing, no dated window ------------------------
# The 2.00/10.00 launch price was announced as introductory through
# 2026-08-31, but Anthropic made it the standard price and cancelled the
# 2026-09-01 rise to 3.00/15.00. There is no cutover to price around.

UTC = timezone.utc


def test_sonnet_5_rates_are_flat_2_10():
    want = {
        "fresh": 2.00, "create_5m": 2.50, "create_1h": 4.00,
        "read": 0.20, "output": 10.00,
    }
    assert pricing.rate_for("claude-sonnet-5") == want
    # Same on both sides of the cancelled cutover, and with no timestamp.
    assert pricing.rate_for("claude-sonnet-5", ts=datetime(2026, 7, 21, tzinfo=UTC)) == want
    assert pricing.rate_for("claude-sonnet-5", ts=datetime(2026, 9, 1, tzinfo=UTC)) == want
    assert pricing.rate_for("claude-sonnet-5", ts=datetime(2027, 1, 1, tzinfo=UTC)) == want


def _sonnet_5_cost_per_mtok_input(ts=None) -> float:
    return pricing.compute_cost(
        "claude-sonnet-5", fresh=1_000_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=ts,
    )


def test_compute_cost_for_sonnet_5_is_timestamp_independent():
    assert _sonnet_5_cost_per_mtok_input() == 2.00
    assert _sonnet_5_cost_per_mtok_input(datetime(2026, 7, 21, tzinfo=UTC)) == 2.00
    assert _sonnet_5_cost_per_mtok_input(datetime(2026, 9, 1, tzinfo=UTC)) == 2.00


def test_expired_windows_keep_pricing_their_own_period():
    """An expired window is NOT dead weight. Every PARSER_VERSION bump
    reparses the whole bucket, and a record from inside the window must
    come out at the price that was in force then — dropping the window
    would silently reprice history at list on the next reparse."""
    cutover = datetime(2026, 9, 9, 16, 0, tzinfo=UTC)
    assert cutover < datetime.now(tz=UTC)  # the window has expired
    before = pricing.compute_cost(
        "glm-5-3-flash", fresh=1_000_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=cutover - timedelta(seconds=1),
    )
    after = pricing.compute_cost(
        "glm-5-3-flash", fresh=1_000_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=cutover,
    )
    assert (before, after) == (0.075, 0.15)


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
        during = pricing.rate_for(m, ts=datetime(2026, 7, 21, tzinfo=UTC))
        after = pricing.rate_for(m, ts=datetime(2026, 9, 1, tzinfo=UTC))
        assert during == after == pricing.rate_for(m)


def test_tier_fallback_never_inherits_a_dated_promotion(synthetic_dated_rate):
    # An unrecognised sonnet falls back to Sonnet 5's LIST rates, not its
    # promotional ones, even inside the window.
    r = pricing.resolve("claude-sonnet-9", ts=datetime(2026, 7, 21, tzinfo=UTC))
    assert r.kind == "tier"
    assert r.rates == synthetic_dated_rate.after


def test_rate_epochs_are_exposed_sorted_for_read_time_grouping(synthetic_dated_rate):
    assert pricing.RATE_EPOCHS == [synthetic_dated_rate.cutover]


# --- provider rows (exercised via a synthetic row) --------------------------
# SV-PROVIDER-RATES and SV-DATED-RATES on the provider half of the table.
# The live provider rows are single-entry today (no moves, no first-seen
# hosts since the table was seeded), so these drive the machinery through
# conftest's synthetic_provider_dated_rate rather than a live entry — the
# same reason the model side uses synthetic_dated_rate above.


def _synthetic_provider_doc(w):
    """A pricing.json-shaped document carrying only the synthetic row."""
    def entry(rates, frm):
        return {"from": frm, **{f: rates[f] for f in pricing.RATE_FIELDS}}
    return {
        "models": {},
        "providers": {w.model: {w.host: [
            entry(w.before, w.start.isoformat()),
            entry(w.after, w.cutover.isoformat()),
        ]}},
        "provider_rates_fetched": w.cutover.isoformat(),
    }


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
            unsplit_create=0, read=0, ts=ts, provider=w.host)

    just_before = cost(w.cutover - timedelta(seconds=1))
    at = cost(w.cutover)
    assert (just_before, at) == (w.before["fresh"], w.after["fresh"])


# --- resolution robustness -------------------------------------------------


def test_future_opus_does_not_inherit_legacy_opus_4_pricing():
    # 'claude-opus-4' is a prefix of 'claude-opus-4-9' but a naive substring
    # match would bill a future Opus at retired 15/75 rates.
    r = pricing.resolve("claude-opus-4-9")
    assert r.kind == "tier"
    assert r.rates is pricing.MODEL_RATES["claude-opus-5-5"]
    assert r.rates["fresh"] != 15.00


def test_dated_snapshot_still_matches_its_generic_key():
    assert pricing.rate_for("claude-opus-4-20250514")["fresh"] == 15.00
    assert pricing.rate_for("claude-haiku-4-5-20251001")["fresh"] == 1.00


def test_provider_prefixed_and_dotted_ids_normalise_to_the_exact_key():
    exact = pricing.rate_for("claude-opus-4-8")
    for variant in (
        "anthropic/claude-opus-4.8",
        "us.anthropic.claude-opus-4-8",
        "eu.anthropic.claude-opus-4-8",
        "CLAUDE-OPUS-4-8",
    ):
        assert pricing.rate_for(variant) == exact, variant


def test_resolve_reports_exact_match():
    assert pricing.resolve("claude-opus-4-8").kind == "exact"


def test_resolve_reports_tier_fallback_for_unknown_claude_model():
    res = pricing.resolve("claude-sonnet-6")
    assert res.kind == "tier"
    # Current-generation Sonnet rates, whatever they are today.
    assert res.rates == pricing.MODEL_RATES["claude-sonnet-5"]


def test_resolve_reports_default_for_wholly_unknown_model():
    assert pricing.resolve("gpt-5").kind == "default"


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


def test_glm_flash_resolves_exact_despite_dots_and_suffixes():
    r = pricing.resolve("glm-5.3-flash")
    assert (r.kind, r.key) == ("exact", "glm-5-3-flash")
    assert pricing.resolve("GLM-5.3-Flash[1m]").kind == "exact"
    assert pricing.rate_for("glm-5.3-flash") == GLM_LIST


def test_glm_flash_promo_window_ends_exclusive_at_utc8_midnight():
    just_before = GLM_CUTOVER - timedelta(seconds=1)
    assert pricing.rate_for("glm-5.3-flash", ts=just_before) == GLM_PROMO
    assert pricing.rate_for("glm-5.3-flash", ts=GLM_CUTOVER) == GLM_LIST
    assert pricing.rate_for(
        "glm-5.3-flash", ts=datetime(2027, 1, 1, tzinfo=UTC)) == GLM_LIST


def test_glm_flash_cache_writes_cost_nothing():
    # 1M each of 5m writes, 1h writes and unsplit legacy writes: all free.
    cost = pricing.compute_cost(
        "glm-5.3-flash",
        fresh=0, output=0,
        eph5=1_000_000, eph1h=1_000_000, unsplit_create=1_000_000, read=0,
        ts=GLM_CUTOVER - timedelta(seconds=1),
    )
    assert cost == 0.0


def test_glm_flash_compute_cost_promo_known_vector():
    cost = pricing.compute_cost(
        "glm-5.3-flash",
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
    r = pricing.resolve("stealth/claude-opus-4-8")
    assert (r.kind, r.rates) == ("exact", pricing.FREE_RATES)


def test_nonfree_openrouter_id_is_unchanged():
    """A paid OpenRouter id keeps today's resolution: DEFAULT fallback,
    because only text before 'claude' is stripped, not an openai/ prefix."""
    r = pricing.resolve("openai/gpt-6-sol")
    assert r.kind == "default"
    assert r.rates == pricing.DEFAULT_RATES
    # the unprefixed id still resolves exact against the table
    assert pricing.resolve("gpt-6-sol").kind == "exact"


def test_free_matching_does_not_touch_claude_ids():
    r = pricing.resolve("claude-opus-4-8")
    assert r.kind == "exact"
    assert r.rates is pricing.MODEL_RATES["claude-opus-4-8"]


def test_bonsai_local_lane_is_priced_at_zero_and_resolves_exact():
    """bonsai-2-27b is served by a local llama.cpp, so it has no price.

    Without an entry it would fall to DEFAULT (Opus 4.7 list) and a free
    lane would invent a four-figure bill; the entry must be EXACT so the
    API does not flag it as an estimate either.
    """
    r = pricing.resolve("bonsai-2-27b")
    assert (r.kind, r.key) == ("exact", "bonsai-2-27b")
    assert pricing.compute_cost(
        "bonsai-2-27b", fresh=1_000_000, output=1_000_000,
        eph5=1_000_000, eph1h=1_000_000, unsplit_create=1_000_000,
        read=1_000_000,
    ) == 0
