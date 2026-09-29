"""Model-resolution tests moved out of test_pricing.py (synthetic rows,
plus live-table exact-key checks in the style the live table allows)."""
from __future__ import annotations

import pytest

from backend import pricing


def test_fable_5_1_and_mythos_5_1_price_identically(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The read ratio and identical prices are checked on synthetic rows."""
    fable_key = "claude-acme-fable-5-1"
    mythos_key = "claude-acme-mythos-5-1"
    fable_rates = dict(zip(pricing.RATE_FIELDS, (8.0, 10.0, 16.0, 0.2, 40.0)))
    mythos_rates = dict(fable_rates)
    monkeypatch.setitem(pricing.MODEL_RATES, fable_key, fable_rates)
    monkeypatch.setitem(pricing.MODEL_RATES, mythos_key, mythos_rates)

    fable = pricing.resolve(fable_key)
    mythos = pricing.resolve(f"anthropic.{mythos_key}[1m]")
    assert fable.key == fable_key and fable.rates is fable_rates
    assert mythos.key == mythos_key and mythos.rates is mythos_rates
    for rates in (fable_rates, mythos_rates):
        assert rates["read"] == pytest.approx(rates["fresh"] * 0.025)
    assert fable.rates == mythos.rates
    fable_cost = pricing.compute_cost(
        fable_key, fresh=0, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=1_000_000,
    )
    mythos_cost = pricing.compute_cost(
        f"anthropic.{mythos_key}[1m]", fresh=0, output=0,
        eph5=0, eph1h=0, unsplit_create=0, read=1_000_000,
    )
    assert fable_cost == pytest.approx(fable_rates["read"], rel=1e-12)
    assert mythos_cost == pytest.approx(mythos_rates["read"], rel=1e-12)
    assert fable_cost == mythos_cost


def test_opus_5_5_suffix_and_provider_aliases_keep_their_synthetic_row(
        monkeypatch: pytest.MonkeyPatch) -> None:
    older_key = "claude-acme-opus-5"
    newer_key = "claude-acme-opus-5-5"
    rows = {
        older_key: dict(zip(pricing.RATE_FIELDS, (1, 2, 3, 4, 5))),
        newer_key: dict(zip(pricing.RATE_FIELDS, (6, 7, 8, 9, 10))),
    }
    monkeypatch.setattr(pricing, "MODEL_RATES", rows)
    monkeypatch.setattr(pricing, "DATED_RATES", {})

    for variant in (newer_key, f"{newer_key}[1m]", f"anthropic.{newer_key}"):
        result = pricing.resolve(variant)
        assert result.kind == "exact"
        assert result.key == newer_key
        assert result.rates is rows[newer_key]
    older = pricing.resolve(older_key)
    assert older.rates is rows[older_key]
    assert older.rates is not rows[newer_key]


def test_match_key_memoizes_per_normalised_id() -> None:
    """Issue #350: the reprice pass matches keys for over a million rows
    naming few distinct models, so the longest-key scan is memoized per
    norm — the second call is a dict hit with the same answer. A norm
    matching nothing caches that too; the conftest autouse fixture keeps
    every test's entries out of every other's."""
    norm = "synthetic-norm-issue-350"
    first = pricing._match_key(norm)  # pylint: disable=protected-access
    assert first is None, "no table key prefixes a synthetic norm"
    assert norm in pricing._MATCH_KEY_CACHE  # pylint: disable=protected-access
    assert pricing._match_key(norm) is first  # pylint: disable=protected-access


def test_gpt_6_1_sol_resolves_exact_distinct_from_gpt_6_sol():
    """Dotted and dashed GPT-6.1 Sol ids fold to their own row (issue
    #357): before it landed, the dotted id resolved kind='default' at the
    generic rates. The shorter gpt-6-sol key cannot absorb it —
    _match_key needs the key to be a literal prefix and 'gpt-6-1-sol'
    diverges from 'gpt-6-sol' at the version digit. The live table check
    pins only exact-key resolution."""
    sol = pricing.resolve("gpt-6.1-sol")  # sv-test-data: allow (structure: exact table key survives appends)
    dashed = pricing.resolve("gpt-6-1-sol")  # sv-test-data: allow (structure: exact table key survives appends)
    shorter = pricing.resolve("gpt-6-sol")  # sv-test-data: allow (structure: exact table key survives appends)
    assert sol.kind == dashed.kind == "exact"
    assert sol.key == dashed.key == "gpt-6-1-sol"
    assert shorter.key == "gpt-6-sol"
    assert set(sol.rates) == set(pricing.RATE_FIELDS)
    assert sol.rates is not shorter.rates
