"""The per-(model, provider) rate fingerprint (issue #351).

pair_fingerprint(model, provider) digests exactly the rate data
pricing.resolve() consults for the pair, plus the pricing modules'
source, so two equal fingerprints imply compute_cost prices every row
of the pair identically whatever its tokens and timestamp — the
soundness the reprice pass's SQL restamp rests on. Every expectation
here prices against SYNTHETIC tables (SV-TEST-DATA): the fixture
patches the pricing module attributes this module reads and clears
both memo caches around each mutation, never reading the committed
src/pricing.json rows.
"""
from __future__ import annotations

import importlib
import inspect
import re
from datetime import datetime, timezone

import pytest

from backend import pricing, rate_fingerprint

UTC = timezone.utc

# Rates deliberately unlike any real price, so an assertion against
# them can never be mistaken for a pricing fact (the conftest
# synthetic-rate fixtures' rule).
_R1 = {"fresh": 1.5, "create_5m": 1.875, "create_1h": 3.0,
       "read": 0.15, "output": 7.5}
_R2 = {"fresh": 2.5, "create_5m": 3.125, "create_1h": 5.0,
       "read": 0.25, "output": 12.5}
_R3 = {"fresh": 0.1, "create_5m": 0.125, "create_1h": 0.2,
       "read": 0.01, "output": 0.5}
_R4 = {"fresh": 4.5, "create_5m": 5.625, "create_1h": 9.0,
       "read": 0.45, "output": 22.5}
_R5 = {"fresh": 5.5, "create_5m": 6.875, "create_1h": 11.0,
       "read": 0.55, "output": 27.5}
_R6 = {"fresh": 6.5, "create_5m": 8.125, "create_1h": 13.0,
       "read": 0.65, "output": 32.5}
_R7 = {"fresh": 7.5, "create_5m": 9.375, "create_1h": 15.0,
       "read": 0.75, "output": 37.5}

# The pair-scope every sensitivity mutation is judged over: the
# affected pair's fingerprint must move, the unrelated pair's must not.
_AFFECTED_MODEL = "claude-opus-9"
_UNRELATED_MODEL = "claude-sonnet-5"
_PROVIDER_PAIR = ("vendor/m-9", "HostX")
_PROVIDER_MODEL, _PROVIDER_HOST = _PROVIDER_PAIR


def _refresh() -> None:
    """Empty both memo caches, so a mid-test table patch is visible to
    the next fingerprint call (the autouse fixture only spans tests)."""
    pricing._MATCH_KEY_CACHE.clear()  # pylint: disable=protected-access
    pricing._VENDOR_MATCH_CACHE.clear()  # pylint: disable=protected-access
    rate_fingerprint.clear_fingerprint_cache()


def _prefixed_tables(monkeypatch) -> dict:
    """One tracked bare row shared by prefixed host and vendor fallback."""
    rates = {"fresh": 10.0, "create_5m": 10.0, "create_1h": 10.0,
             "read": 1.0, "output": 20.0, "web_search": 0.015}
    for name, value in {
        "VENDOR_PREFIXES": ["acme"], "VENDOR_BARE": {"toy": "toy"},
        "VENDOR_HOSTS": {"toy": "Host"},
        "PROVIDER_RATES": {("toy", "Host"): rates},
        "PROVIDER_DATED_RATES": {}, "PROVIDER_STARTS": {},
        "PROVIDER_SCHEDULES": {},
    }.items():
        monkeypatch.setattr(pricing, name, value)
    _refresh()
    return rates


@pytest.mark.parametrize("provider", ["Host", None])
@pytest.mark.parametrize(("field", "value"), [("fresh", 30.0), ("web_search", 0.045)])
def test_prefixed_pair_fingerprint_tracks_consulted_bare_rates(monkeypatch, provider, field, value):
    rates = _prefixed_tables(monkeypatch)
    before = rate_fingerprint.pair_fingerprint("acme/toy", provider)
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {("toy", "Host"): {**rates, field: value}})
    _refresh()
    assert rate_fingerprint.pair_fingerprint("acme/toy", provider) != before


@pytest.mark.parametrize("model", ["other/toy", "acme/toy:free", "stealth/toy"])
def test_untracked_and_free_prefixes_do_not_consult_the_bare_row(monkeypatch, model):
    rates = _prefixed_tables(monkeypatch)
    before = rate_fingerprint.pair_fingerprint(model, "Host")
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {("toy", "Host"): {**rates, "fresh": 30.0}})
    _refresh()
    assert rate_fingerprint.pair_fingerprint(model, "Host") == before


@pytest.fixture(name="rate_tables")
def _rate_tables_fixture(monkeypatch):
    """Synthetic rate tables, fully patched: two model keys, two
    provider rows (distinct prices, so the provider pairs are
    distinguishable), a synthetic default, and synthetic tier fallbacks
    (production derives those from MODEL_RATES at import; a patched
    MODEL_RATES needs the same re-derivation here)."""
    monkeypatch.setattr(pricing, "MODEL_RATES", {
        _AFFECTED_MODEL: dict(_R1),
        _UNRELATED_MODEL: dict(_R2),
    })
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {
        _PROVIDER_PAIR: dict(_R1),
        ("vendor/m-9", "HostY"): dict(_R2),
    })
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {})
    monkeypatch.setattr(pricing, "PROVIDER_STARTS", {})
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {})
    monkeypatch.setattr(pricing, "DEFAULT_RATES", dict(_R3))
    # The tier table derives lazily under the cache-clearing contract:
    # empty the derived state FIRST, then seed the memo with these rows,
    # so _tier_fallbacks() honors them instead of re-deriving from the
    # patched tables (which carry no versioned family rows).
    rate_fingerprint.clear_fingerprint_cache()
    monkeypatch.setattr(pricing, "_TIER_FALLBACKS", (
        (re.compile(r"fable"), dict(_R4)),
        (re.compile(r"opus"), dict(_R5)),
        (re.compile(r"sonnet"), dict(_R6)),
    ))
    yield
    rate_fingerprint.clear_fingerprint_cache()


def test_same_tables_give_identical_fingerprints(rate_tables):
    """Stability: with the tables unchanged, repeated calls — and the
    same pair spelled through equal inputs — yield one fingerprint."""
    first = rate_fingerprint.pair_fingerprint(_AFFECTED_MODEL, None)
    second = rate_fingerprint.pair_fingerprint(_AFFECTED_MODEL, None)
    assert first == second
    assert len(first) == 64, "the fingerprint is a sha256 hex digest"


def _mutations():
    """Each sensitivity mutation as (name, patch) — `patch` installs the
    changed rate data and returns the pair whose fingerprint must move.

    Every mutation changes exactly one input resolve() consults: a
    dated window, a list price, a provider window, a provider list, a
    schedule, a row start, a longer matching key, the tier fallback's
    underlying newest key, the default, and the meter membership.
    """
    end = datetime(2030, 1, 1, tzinfo=UTC)
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def dated_window(monkeypatch):
        monkeypatch.setattr(pricing, "DATED_RATES", {
            _AFFECTED_MODEL: [(end, dict(_R7))]})
        return _AFFECTED_MODEL, None

    def model_list(monkeypatch):
        monkeypatch.setattr(pricing, "MODEL_RATES", {
            **pricing.MODEL_RATES, _AFFECTED_MODEL: dict(_R7)})
        return _AFFECTED_MODEL, None

    def provider_window(monkeypatch):
        monkeypatch.setattr(pricing, "PROVIDER_RATES",
                            {**pricing.PROVIDER_RATES,
                             _PROVIDER_PAIR: dict(_R1)})
        monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES",
                            {_PROVIDER_PAIR: [(end, dict(_R7))]})
        return _PROVIDER_MODEL, _PROVIDER_HOST

    def provider_list(monkeypatch):
        monkeypatch.setattr(pricing, "PROVIDER_RATES",
                            {**pricing.PROVIDER_RATES,
                             _PROVIDER_PAIR: dict(_R7)})
        return _PROVIDER_MODEL, _PROVIDER_HOST

    def provider_schedule(monkeypatch):
        # A whole-day window and a wrapped weekend one: days serialize
        # sorted, so an unsorted frozenset must not leak into the fp.
        monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {
            _PROVIDER_PAIR: {0: [(None, None, None, dict(_R4)),
                                 (frozenset({"saturday", "monday"}),
                                  1320, 120, dict(_R5))]},
        })
        return _PROVIDER_MODEL, _PROVIDER_HOST

    def provider_start(monkeypatch):
        monkeypatch.setattr(pricing, "PROVIDER_STARTS",
                            {_PROVIDER_PAIR: start})
        return _PROVIDER_MODEL, _PROVIDER_HOST

    def longer_key_steals(monkeypatch):
        exact = f"{_AFFECTED_MODEL}-20260101"
        monkeypatch.setattr(pricing, "MODEL_RATES", {
            **pricing.MODEL_RATES, exact: dict(_R7)})
        return exact, None

    def newer_family_key_moves_tier(monkeypatch):
        monkeypatch.setattr(pricing, "MODEL_RATES", {
            **pricing.MODEL_RATES, "claude-sonnet-6": dict(_R7)})
        # The tier table derives lazily from the tables in force; seed
        # the memo with the sonnet family re-derived from the patched
        # table so it is what resolves (the other families keep the
        # fixture's rows).
        monkeypatch.setattr(pricing, "_TIER_FALLBACKS", (
            (re.compile(r"fable"), dict(_R4)),
            (re.compile(r"opus"), dict(_R5)),
            (re.compile(r"sonnet"),
             pricing._latest("sonnet")),  # pylint: disable=protected-access
        ))
        return "claude-sonnet-99", None

    def default_rates(monkeypatch):
        monkeypatch.setattr(pricing, "DEFAULT_RATES", dict(_R7))
        return "weird-thing-9", None

    def meter_membership(monkeypatch):
        # The reprice pass's meter re-derivation consults membership, so
        # the document carries it unconditionally ("metered"): moving a
        # key in or out of the set must move every pair's fingerprint,
        # not only a member model's.
        monkeypatch.setattr(pricing, "LONG_CONTEXT_MODELS",
                            frozenset({"gpt-9-metered-check"}))
        return "weird-thing-9", None

    def provider_search_rate(monkeypatch):
        # The listing's per-search rate is an input resolve() consults:
        # moving it must force a recompute rather than a clean restamp.
        monkeypatch.setattr(pricing, "PROVIDER_RATES", {
            **pricing.PROVIDER_RATES,
            _PROVIDER_PAIR: {**_R1, "web_search": 0.0137},
        })
        return _PROVIDER_MODEL, _PROVIDER_HOST

    return [
        ("dated window", dated_window),
        ("model list", model_list),
        ("provider window", provider_window),
        ("provider list", provider_list),
        ("provider schedule", provider_schedule),
        ("provider start", provider_start),
        ("provider web search", provider_search_rate),
        ("longer key steals", longer_key_steals),
        ("newer family key", newer_family_key_moves_tier),
        ("default rates", default_rates),
        ("meter membership", meter_membership),
    ]


# The default and the meter membership are part of every pair's
# fingerprint by design (the structure carries both unconditionally),
# so those are the two mutations with no unaffected pair; the
# unrelated-pair test covers the other eight.
_PAIR_SCOPED = [m for m in _mutations()
                if m[0] not in ("default rates", "meter membership")]

# Each mutation's affected pair — fingerprinted before AND after the
# patch in the sensitivity test.
_PROBES: dict[str, tuple[str, str | None]] = {
    "dated window": (_AFFECTED_MODEL, None),
    "model list": (_AFFECTED_MODEL, None),
    "provider window": (_PROVIDER_MODEL, _PROVIDER_HOST),
    "provider list": (_PROVIDER_MODEL, _PROVIDER_HOST),
    "provider schedule": (_PROVIDER_MODEL, _PROVIDER_HOST),
    "provider start": (_PROVIDER_MODEL, _PROVIDER_HOST),
    "provider web search": (_PROVIDER_MODEL, _PROVIDER_HOST),
    "longer key steals": (f"{_AFFECTED_MODEL}-20260101", None),
    "newer family key": ("claude-sonnet-99", None),
    "default rates": ("weird-thing-9", None),
    "meter membership": ("weird-thing-9", None),
}


def test_search_rate_is_part_of_the_pair_fingerprint(rate_tables, monkeypatch):
    pair = _PROVIDER_PAIR
    first_rates = {**_R1, "web_search": 0.0137}
    monkeypatch.setattr(pricing, "PROVIDER_RATES",
                        {pair: first_rates})
    _refresh()
    before = rate_fingerprint.pair_fingerprint(*pair)
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {
        pair: {**first_rates, "web_search": 0.045},
    })
    _refresh()

    assert rate_fingerprint.pair_fingerprint(*pair) != before


@pytest.mark.parametrize("name,patch", _mutations(), ids=[n for n, _ in _mutations()])
def test_mutation_changes_the_affected_pairs_fingerprint(
        rate_tables, monkeypatch, name, patch):
    """Sensitivity: each mutation of the rate data resolve() consults
    moves the affected pair's fingerprint."""
    model, provider = _PROBES[name]
    _refresh()
    before = rate_fingerprint.pair_fingerprint(model, provider)
    patch(monkeypatch)
    _refresh()
    after = rate_fingerprint.pair_fingerprint(model, provider)
    assert after != before, f"{name} must move the pair's fingerprint"


@pytest.mark.parametrize("name,patch", _PAIR_SCOPED, ids=[n for n, _ in _PAIR_SCOPED])
def test_mutation_leaves_the_unrelated_pair_alone(rate_tables, monkeypatch,
                                                  name, patch):
    """A pair whose consulted rate data a mutation did not touch keeps
    its fingerprint — the property that lets the reprice pass restamp
    the unaffected pairs set-based."""
    before = rate_fingerprint.pair_fingerprint(_UNRELATED_MODEL, None)
    patch(monkeypatch)
    _refresh()
    after = rate_fingerprint.pair_fingerprint(_UNRELATED_MODEL, None)
    assert after == before, f"{name} must not touch the unrelated pair"


def test_normalisation_folds_spellings_and_free_is_table_free(
        rate_tables, monkeypatch):
    """Spellings that normalise together fingerprint together; a free
    id prices at zero whatever the tables say, so its fingerprint is
    the same under any tables; and distinct pairs never collide."""
    assert (rate_fingerprint.pair_fingerprint("Claude-Opus-4.8", None)
            == rate_fingerprint.pair_fingerprint("claude-opus-4-8", None))
    free = "gpt-9:free"
    stealth = "stealth/gpt-9"
    assert (rate_fingerprint.pair_fingerprint(free, None)
            == rate_fingerprint.pair_fingerprint(free.upper(), None))
    assert (rate_fingerprint.pair_fingerprint(stealth, None)
            == rate_fingerprint.pair_fingerprint("STEALTH/gpt-9", None))

    free_fp = rate_fingerprint.pair_fingerprint(free, None)
    stealth_fp = rate_fingerprint.pair_fingerprint(stealth, None)
    monkeypatch.setattr(pricing, "MODEL_RATES", {
        **pricing.MODEL_RATES, free.split(":", maxsplit=1)[0]: dict(_R7)})
    monkeypatch.setattr(pricing, "DEFAULT_RATES", dict(_R4))
    _refresh()
    assert rate_fingerprint.pair_fingerprint(free, None) == free_fp, (
        "a free id's fingerprint is independent of the tables")
    assert rate_fingerprint.pair_fingerprint(stealth, None) == stealth_fp, (
        "a stealth id's fingerprint is independent of the tables")

    pairs = [("claude-opus-9", None), ("claude-sonnet-5", None),
             ("claude-sonnet-99", None), ("weird-thing-9", None),
             ("vendor/m-9", "HostX"), ("vendor/m-9", "HostY")]
    with_provider_rows = {
        pair: rate_fingerprint.pair_fingerprint(*pair)
        for pair in pairs}
    assert len(set(with_provider_rows.values())) == len(pairs), (
        "distinct pairs never collide")


def test_memo_and_cache_clear(rate_tables, monkeypatch):
    """The memo serves the second call, and clear_fingerprint_cache()
    exposes a patched table on the next call."""
    first = rate_fingerprint.pair_fingerprint(_AFFECTED_MODEL, None)
    assert rate_fingerprint.pair_fingerprint(_AFFECTED_MODEL, None) == first
    memo = rate_fingerprint._FP_CACHE  # pylint: disable=protected-access
    assert memo[(_AFFECTED_MODEL, None)] == first, "the memo holds the pair"

    monkeypatch.setattr(pricing, "MODEL_RATES", {
        **pricing.MODEL_RATES, _AFFECTED_MODEL: dict(_R7)})
    _refresh()
    memo = rate_fingerprint._FP_CACHE  # pylint: disable=protected-access
    assert not memo, "clear_fingerprint_cache empties the memo"
    assert rate_fingerprint.pair_fingerprint(_AFFECTED_MODEL, None) != first, (
        "after the clear, the patched table's fingerprint is served")


# --------------------------------------------------------------------------
# The reprice pass's own derivation is hashed (issue #377): a logic
# change to ANY module the pass computes stored state from must move
# the fingerprint, so the #249 route (rule change shipped with a
# PRICING_VERSION bump) can never reprice nothing.
# --------------------------------------------------------------------------

def test_parser_and_reprice_derivation_modules_are_hashed():
    """The digest covers the parser's search-count semantics and the
    reprice pass that applies those stored counts to current rates."""
    names = {m.__name__ for m in rate_fingerprint.hashed_modules()}
    assert names == {"backend.pricing", "backend.pricing_load",
                     "backend.model_names", "backend.meter_tables",
                     "backend.long_context", "backend.parse",
                     "backend.parse_common", "backend.parse_codex",
                     "backend.ingest_reprice"}


def test_reprice_pass_source_edit_moves_the_fingerprint(monkeypatch):
    """A source edit to the reprice pass moves every pair fingerprint:
    the digest is computed over the pass's module too, so an edit is
    visible to the clean restamp even though the rate tables stand
    still. Simulates the edit by mutating what inspect.getsource
    reports for the pass's module and recomputing the digest the way
    import does."""
    model = "fp377-model"
    before = rate_fingerprint.pair_fingerprint(model, None)
    real_getsource = inspect.getsource

    def _mutated(module):
        text = real_getsource(module)
        if module.__name__ == "backend.ingest_reprice":
            text += "\n# simulated rule edit (issue #377)\n"
        return text

    monkeypatch.setattr(inspect, "getsource", _mutated)
    importlib.reload(rate_fingerprint)
    after = rate_fingerprint.pair_fingerprint(model, None)
    monkeypatch.undo()
    importlib.reload(rate_fingerprint)
    assert after != before, (
        "editing the reprice pass's source must move the fingerprint")


def test_claude_search_count_source_edit_moves_the_fingerprint(monkeypatch):
    model = "fp-search-model"
    before = rate_fingerprint.pair_fingerprint(model, None)
    real_getsource = inspect.getsource

    def _mutated(module):
        text = real_getsource(module)
        if module.__name__ == "backend.parse":
            text += "\n# simulated server_tool_use count rule change\n"
        return text

    monkeypatch.setattr(inspect, "getsource", _mutated)
    importlib.reload(rate_fingerprint)
    after = rate_fingerprint.pair_fingerprint(model, None)
    monkeypatch.undo()
    importlib.reload(rate_fingerprint)
    assert after != before


_FAMILY_RATES = {"fresh": 1.0, "create_5m": 1.25, "create_1h": 2.0,
                 "read": 0.1, "output": 4.0}


def test_lazy_tier_table_skips_empty_families_and_forgets_on_clear(monkeypatch):
    """The lazy tier table skips a family no key carries a row for — the
    bench's bounded document and patched-tables tests name none — and a
    table derived under one state does not outlive the clearing contract:
    the next derivation reads the tables then in force."""
    monkeypatch.setattr(pricing, "VENDOR_BARE", {})
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {})
    pricing.clear_tier_fallbacks()
    try:
        monkeypatch.setattr(pricing, "MODEL_RATES",
                            {"suite-synth-model": dict(_FAMILY_RATES)})
        assert pricing._tier_fallbacks() == ()  # pylint: disable=protected-access
        pricing.clear_tier_fallbacks()
        monkeypatch.setattr(pricing, "MODEL_RATES",
                            {"claude-opus-9": dict(_FAMILY_RATES)})
        populated = pricing._tier_fallbacks()  # pylint: disable=protected-access
        assert [p.pattern for p, _ in populated] == ["opus"]
        pricing.clear_tier_fallbacks()
        monkeypatch.setattr(pricing, "MODEL_RATES",
                            {"suite-synth-model": dict(_FAMILY_RATES)})
        assert pricing._tier_fallbacks() == ()  # pylint: disable=protected-access
    finally:
        pricing.clear_tier_fallbacks()
