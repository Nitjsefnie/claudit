"""Per-request fees (issue #469): the note-ride, resolution and fold.

OpenRouter lists per-request fees (`web_search` at $X/request) that its
endpoints charge beside token rates. The refresh records the fee in the
provider row's entry `note` (RECORDED_FEES in scripts/ci/refresh_prices.py,
SV-RATE-REFRESH); this side makes claudit PRICE it. The loader parses each
entry's note into a per-request fee; resolve() returns the fee in force at
the record's own ts; compute_cost folds it into cost_usd; the reprice pass
stores it beside the cost in records.request_fee_usd (additive, nullable,
SV-SCHEMA-AUTOAPPLY), so "cost_usd is what the session cost" survives
without a fee-aware panel surface.

Driven by synthetic documents and tables throughout: the fee amounts are
deliberately unlike any real price (SV-TEST-DATA).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from tests.refresh_fixture_builders import DEFAULT_ROW


from backend import pricing
from backend.pricing_load import RATE_FIELDS, load_tables

UTC = timezone.utc

# Deliberately unlike any real fee.
FEE = 0.0137
FEE2 = 0.045


def _fee_note(amount: float) -> str:
    """The one note format the refresh writes (fee_notes) and the loader
    parses — quoted here, never regenerated from the script's source."""
    return (f"web_search ${amount}/request not modelled: per-request, "
            "unpriceable from token counts")


def _entry(rates: dict, **extra) -> dict:
    return {"from": None, **{f: rates[f] for f in RATE_FIELDS}, **extra}


def _doc(provider_entries: list[dict] | None = None,
         model_note: str | None = None) -> dict:
    """A shape-valid pricing.json fragment, layout as the refresh writes."""
    model_entries = [_entry({"fresh": 3.0, "create_5m": 3.75,
                             "create_1h": 6.0, "read": 0.3, "output": 15.0})]
    if model_note is not None:
        model_entries[0]["note"] = model_note
    doc = {
        "models": {"acme/acme-9": model_entries,
                   "claude-opus-4-7": [dict(DEFAULT_ROW)]},
        "providers": {
            "acme/acme-9": {"HostCo": provider_entries
                            if provider_entries is not None
                            else [_entry({"fresh": 2.0, "create_5m": 2.5,
                                          "create_1h": 4.0, "read": 0.2,
                                          "output": 10.0},
                                         note=_fee_note(FEE))]},
        },
        "long_context_models": [],
        "openrouter": {"data_region": "global", "models": {},
                       "vendor": {"prefixes": ["anthropic", "openai",
                                               "moonshotai", "z-ai"]}},
        "provider_rates_fetched": "2026-09-24T22:03:13Z",
    }
    return doc


# --- the loader parses the note into per-entry fees -----------------------


def test_provider_entry_note_parses_into_provider_fees() -> None:
    tables = load_tables(_doc())
    key = ("acme/acme-9", "HostCo")
    assert tables["PROVIDER_FEES"][key] == {0: FEE}


def test_model_row_note_parses_into_model_fees() -> None:
    tables = load_tables(_doc(model_note=_fee_note(FEE2)))
    assert tables["FEES"] == {"acme/acme-9": {0: FEE2}}


def test_dated_entry_fee_rides_its_own_index() -> None:
    older = _entry({"fresh": 2.0, "create_5m": 2.5, "create_1h": 4.0,
                    "read": 0.2, "output": 10.0}, note=_fee_note(FEE))
    newer = {**older, "from": "2026-08-01T00:00:00Z",
             "note": "17% off; " + _fee_note(FEE2)}
    tables = load_tables(_doc([older, newer]))
    key = ("acme/acme-9", "HostCo")
    assert tables["PROVIDER_FEES"][key] == {0: FEE, 1: FEE2}


def test_entry_without_note_carries_no_fee() -> None:
    tables = load_tables(_doc([_entry({"fresh": 2.0, "create_5m": 2.5,
                                       "create_1h": 4.0, "read": 0.2,
                                       "output": 10.0})]))
    assert not tables["PROVIDER_FEES"]
    assert not tables["FEES"]


def test_fee_shaped_but_malformed_note_refuses() -> None:
    bad = "web_search $0.01/request not modelled: extra words"
    with pytest.raises(ValueError, match="acme/acme-9 via HostCo"):
        load_tables(_doc(provider_entries=[
            _entry({"fresh": 2.0, "create_5m": 2.5, "create_1h": 4.0,
                    "read": 0.2, "output": 10.0}, note=bad)]))


def test_non_fee_note_ignores_the_fee_parser() -> None:
    tables = load_tables(_doc(provider_entries=[
        _entry({"fresh": 2.0, "create_5m": 2.5, "create_1h": 4.0,
                "read": 0.2, "output": 10.0}, note="12% off")]))
    assert not tables["PROVIDER_FEES"]
    assert not tables["FEES"]


# --- resolution -----------------------------------------------------------


def test_resolve_returns_the_entry_in_force_fee(monkeypatch) -> None:
    older = _entry({"fresh": 2.0, "create_5m": 2.5, "create_1h": 4.0,
                    "read": 0.2, "output": 10.0}, note=_fee_note(FEE))
    newer = {**older, "from": "2026-08-01T00:00:00Z",
             "note": "17% off; " + _fee_note(FEE2)}
    tables = load_tables(_doc([older, newer]))
    monkeypatch.setattr(pricing, "PROVIDER_FEES",
                        tables["PROVIDER_FEES"])
    monkeypatch.setattr(pricing, "PROVIDER_RATES",
                        tables["PROVIDER_RATES"])
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES",
                        tables["PROVIDER_DATED_RATES"])
    before = datetime(2026, 7, 1, tzinfo=UTC)
    after = datetime(2026, 8, 2, tzinfo=UTC)
    assert pricing.request_fee("acme/acme-9", before, "HostCo") == FEE
    assert pricing.request_fee("acme/acme-9", after, "HostCo") == FEE2
    assert pricing.resolve("acme/acme-9", before, "HostCo").request_fee == FEE


def test_resolve_without_ts_prices_the_list_entry_fee(monkeypatch) -> None:
    tables = load_tables(_doc())
    monkeypatch.setattr(pricing, "PROVIDER_FEES", tables["PROVIDER_FEES"])
    monkeypatch.setattr(pricing, "PROVIDER_RATES", tables["PROVIDER_RATES"])
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES",
                        tables["PROVIDER_DATED_RATES"])
    assert pricing.request_fee("acme/acme-9", None, "HostCo") == FEE


def test_model_branch_resolves_a_model_row_fee(monkeypatch) -> None:
    tables = load_tables(_doc(model_note=_fee_note(FEE2)))
    monkeypatch.setattr(pricing, "FEES", tables["FEES"])
    monkeypatch.setattr(pricing, "MODEL_RATES", tables["MODEL_RATES"])
    monkeypatch.setattr(pricing, "DATED_RATES", tables["DATED_RATES"])
    assert pricing.request_fee("acme/acme-9", None, None) == FEE2
    # The provider branch wins when a provider row exists, like rates.
    monkeypatch.setattr(pricing, "PROVIDER_FEES", {})
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {})
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {})
    assert pricing.request_fee("acme/acme-9", None, "HostCo") == FEE2


def test_no_table_no_fee(monkeypatch) -> None:
    monkeypatch.setattr(pricing, "PROVIDER_FEES", {})
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {})
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {})
    monkeypatch.setattr(pricing, "FEES", {})
    # No provider row: the model row carries no fee note either -> 0.
    assert pricing.request_fee("acme/acme-9", None, "HostCo") == 0.0
    # Free ids price at zero before any table.
    assert pricing.request_fee("acme/acme-9:free", None, None) == 0.0


def test_compute_cost_folds_the_fee(monkeypatch) -> None:
    tables = load_tables(_doc())
    monkeypatch.setattr(pricing, "PROVIDER_FEES", tables["PROVIDER_FEES"])
    monkeypatch.setattr(pricing, "PROVIDER_RATES", tables["PROVIDER_RATES"])
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES",
                        tables["PROVIDER_DATED_RATES"])
    # The provider row's own rates (fresh 2.0) plus the entry's fee, once.
    with_fee = pricing.compute_cost(
        "acme/acme-9", fresh=1_000_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=None,
        res=pricing.resolve("acme/acme-9", None, "HostCo"))
    assert with_fee == pytest.approx(2.0 + FEE)
    # Same provider, ts None: the LIST entry's fee.
    assert pricing.compute_cost(
        "acme/acme-9", fresh=0, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=None,
        res=pricing.resolve("acme/acme-9", None, "HostCo")) \
        == pytest.approx(FEE)


def test_compute_cost_keeps_lane_callers_fee_free(monkeypatch) -> None:
    tables = load_tables(_doc())
    monkeypatch.setattr(pricing, "PROVIDER_FEES", tables["PROVIDER_FEES"])
    monkeypatch.setattr(pricing, "PROVIDER_RATES", tables["PROVIDER_RATES"])
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES",
                        tables["PROVIDER_DATED_RATES"])
    # The lane path resolves with no host, so it prices exactly as
    # before the fee existed.
    assert pricing.compute_cost(
        "acme/acme-9", fresh=0, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=None) == 0.0


def test_fee_shaped_note_with_a_non_ascii_fee_key_refuses() -> None:
    """The note format is the refresh's own ASCII output; a fee key
    outside the ASCII vocabulary the browser's \\w matches would load in
    Python and refuse in the browser — the loaders stay aligned."""
    bad = "wéb_search $0.01/request not modelled: per-request, " \
          "unpriceable from token counts"
    with pytest.raises(ValueError, match="acme/acme-9 via HostCo"):
        load_tables(_doc(provider_entries=[
            _entry({"fresh": 2.0, "create_5m": 2.5, "create_1h": 4.0,
                    "read": 0.2, "output": 10.0}, note=bad)]))


def test_compute_cost_accepts_a_precomputed_resolution(monkeypatch) -> None:
    """The parse path resolves once and passes the Resolution: pricing the
    tokens and naming the fee must not depend on who resolved."""
    tables = load_tables(_doc())
    monkeypatch.setattr(pricing, "PROVIDER_FEES", tables["PROVIDER_FEES"])
    monkeypatch.setattr(pricing, "PROVIDER_RATES", tables["PROVIDER_RATES"])
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES",
                        tables["PROVIDER_DATED_RATES"])
    res = pricing.resolve("acme/acme-9", None, "HostCo")
    with_res = pricing.compute_cost(
        "acme/acme-9", fresh=1_000_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=None, res=res)
    again = pricing.compute_cost(
        "acme/acme-9", fresh=1_000_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=None,
        res=pricing.resolve("acme/acme-9", None, "HostCo"))
    assert with_res == pytest.approx(again)
    # The host's own rates (fresh 2.0) plus exactly one fee.
    assert with_res == pytest.approx(2.0 + FEE)
    assert res.request_fee == FEE
