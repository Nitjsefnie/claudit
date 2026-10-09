"""Per-model long-context meter thresholds and factors (issue #878).

long_context_meters groups model keys by threshold; optional per-model
input/output factors override the global defaults. Every member therefore
has an explicit threshold. A Claude-format record above its model's threshold bills
the band and carries the flag; every other Claude-format row keeps the
NULL marker (issue #249's reprice-equals-reparse law forces the decision
into the parse path). The Codex lane and the reprice re-derivation read the
same threshold.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from backend import parse, pricing, rate_fingerprint
from backend.ingest_reprice import _record_updates, _StaleRow
from backend.parse_common import _to_dt

FIXTURE = (Path(__file__).resolve().parents[1] / "fixtures" / "parser"
           / "claude_meter_band.jsonl")

# A synthetic member at a 200k threshold, priced from controlled rows
# (SV-TEST-DATA): the live table's sonnet row is never pinned.
MEMBER = "claude-sonnet-4-5"
THRESHOLD = 200_000
RATES = {"fresh": 3.0, "create_5m": 3.75, "create_1h": 6.0, "read": 0.3,
         "output": 15.0}

MEMBERS = frozenset({"gpt-5-6-sol", MEMBER})
# The LOADED table keeps the complete per-model meter entry. The rates
# and factors are synthetic so no assertion can be mistaken for a live
# pricing fact (SV-TEST-DATA).
METERS = {
    MEMBER: {"threshold": THRESHOLD, "input_mult": 5.0, "output_mult": 5.0},
}


def _install_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """The synthetic membership, meters and rates the decisions consult."""
    monkeypatch.setattr(pricing, "LONG_CONTEXT_MODELS", MEMBERS)
    monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS", METERS)
    monkeypatch.setattr(pricing, "MODEL_RATES",
                        {**pricing.MODEL_RATES, MEMBER: RATES})
    rate_fingerprint.clear_fingerprint_cache()


# ---------------------------------------------------------------------------
# pricing: the threshold lookup and the meter flag
# ---------------------------------------------------------------------------


def test_long_context_threshold_reads_the_model_meter(monkeypatch):
    _install_tables(monkeypatch)
    assert pricing.long_context_threshold(MEMBER) == THRESHOLD
    assert pricing.long_context_threshold("anthropic/claude-sonnet-4.5") == THRESHOLD
    # A member without a meter entry keeps the global default.
    assert pricing.long_context_threshold("gpt-5.6-sol") == (
        pricing.LONG_CONTEXT_THRESHOLD)
    assert pricing.long_context_threshold(None) == pricing.LONG_CONTEXT_THRESHOLD


def test_meter_flag_decides_per_model(monkeypatch):
    _install_tables(monkeypatch)
    assert pricing.meter_flag(MEMBER, THRESHOLD + 1) is True
    assert pricing.meter_flag(MEMBER, THRESHOLD) is False, "the band is open"
    # A member below its own threshold stays flat.
    assert pricing.meter_flag(MEMBER, 1_000) is False
    # A non-member keeps the NULL marker whatever its tally.
    assert pricing.meter_flag("claude-opus-4-7", 10_000_000) is None


def test_compute_cost_uses_the_models_meter_factors(monkeypatch):
    _install_tables(monkeypatch)
    asymmetric_meter = {"threshold": THRESHOLD, "input_mult": 6.0,
                        "output_mult": 3.0}
    monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS",
                        {MEMBER: asymmetric_meter})
    assert pricing.long_context_factors(MEMBER) == (6.0, 3.0)
    monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS", {
        MEMBER: asymmetric_meter,
        "gpt-5-6-sol": {"threshold": 200_000}})
    assert pricing.long_context_factors("gpt-5.6-sol") == (
        pricing.LONG_CONTEXT_INPUT_MULT, pricing.LONG_CONTEXT_OUTPUT_MULT)
    monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS",
                        {MEMBER: asymmetric_meter})

    got = pricing.compute_cost(
        MEMBER, fresh=100_000, output=2_000, eph5=20_000, eph1h=30_000,
        unsplit_create=40_000, read=10_000,
        adjustments=pricing.CostAdjustments(long_context=True))
    expected = (
        100_000 * RATES["fresh"] * 6.0
        + 20_000 * RATES["create_5m"] * 6.0
        + 70_000 * RATES["create_1h"] * 6.0
        + 10_000 * RATES["read"] * 6.0
        + 2_000 * RATES["output"] * 3.0
    ) / 1_000_000
    assert got == pytest.approx(expected)


def test_compute_cost_defaults_an_omitted_meter_factor_independently(
        monkeypatch):
    """A one-sided model override inherits only its missing output side."""
    _install_tables(monkeypatch)
    monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS", {
        MEMBER: {"threshold": THRESHOLD, "input_mult": 6.0}})
    assert pricing.long_context_factors(MEMBER) == (
        6.0, pricing.LONG_CONTEXT_OUTPUT_MULT)
    got = pricing.compute_cost(
        MEMBER, fresh=10_000, output=2_000, eph5=0, eph1h=0,
        unsplit_create=0, read=4_000,
        adjustments=pricing.CostAdjustments(long_context=True))
    expected = (
        10_000 * RATES["fresh"] * 6.0
        + 4_000 * RATES["read"] * 6.0
        + 2_000 * RATES["output"] * pricing.LONG_CONTEXT_OUTPUT_MULT
    ) / 1_000_000
    assert got == pytest.approx(expected)


# ---------------------------------------------------------------------------
# parse: the Claude-format path stores the decision
# ---------------------------------------------------------------------------


def _claude_line(model: str, in_tokens: int, read: int = 0) -> bytes:
    return b"".join(json.dumps(line).encode() + b"\n" for line in [
        {"type": "user", "timestamp": "2026-06-01T12:00:00Z", "uuid": "u1",
         "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "timestamp": "2026-06-01T12:00:01Z",
         "uuid": "a1", "requestId": "req-1", "sessionId": "sess-1",
         "message": {"role": "assistant", "model": model,
                     "content": [{"type": "text", "text": "hello"}],
                     "usage": {"input_tokens": in_tokens,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": read,
                               "output_tokens": 500}}},
    ])


def test_the_claude_path_bills_a_member_above_its_band(monkeypatch):
    _install_tables(monkeypatch)
    out = parse.parse_file("k/sess-1/sess-1.jsonl", FIXTURE.read_bytes())
    rec = out["records"][0]
    assert rec["long_context"] is True
    assert rec["fresh_tokens"] + rec["cache_read_tokens"] == 260_000
    metered = pricing.compute_cost(
        MEMBER, fresh=250_000, output=500, eph5=0, eph1h=0,
        unsplit_create=0, read=10_000,
        adjustments=pricing.CostAdjustments(long_context=True),
        ts=_to_dt("2026-06-01T12:00:01Z"))
    assert float(rec["cost_usd"]) == pytest.approx(round(metered, 6))


def test_the_claude_path_keeps_the_null_marker_for_non_members(monkeypatch):
    _install_tables(monkeypatch)
    out = parse.parse_file("k/sess-2/sess-2.jsonl",
                           _claude_line("claude-opus-4-7", 10_000_000))
    assert out["records"][0]["long_context"] is None


def test_the_claude_path_stores_false_below_the_band(monkeypatch):
    _install_tables(monkeypatch)
    out = parse.parse_file("k/sess-3/sess-3.jsonl", _claude_line(MEMBER, 1_000))
    assert out["records"][0]["long_context"] is False


# ---------------------------------------------------------------------------
# parse_codex: the lane decision consults the model's own threshold
# ---------------------------------------------------------------------------


def _codex_blob(model: str, last_input: int) -> bytes:
    usage = {"input_tokens": last_input, "cached_input_tokens": 0,
             "cache_write_input_tokens": 0, "output_tokens": 1_000,
             "reasoning_output_tokens": 0, "total_tokens": last_input + 1_000}
    lines = [
        {"timestamp": "2026-07-01T00:00:00.000Z", "type": "turn_context",
         "payload": {"model": model}},
        {"timestamp": "2026-07-01T00:00:01.000Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "total_token_usage": usage, "last_token_usage": usage,
             "model_context_window": 400000}}},
    ]
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


def test_the_codex_lane_decides_on_the_model_threshold(monkeypatch):
    blob = _codex_blob("gpt-5.6-sol", 250_000)
    # The global default leaves the record flat.
    out = parse.parse_file("codex/t1.jsonl", blob)
    assert out["records"][0]["long_context"] is False
    # The model's own meter pulls the band down to 200k.
    monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS",
                        {**METERS, "gpt-5-6-sol": {"threshold": 200_000}})
    out = parse.parse_file("codex/t2.jsonl", blob)
    assert out["records"][0]["long_context"] is True


# ---------------------------------------------------------------------------
# reprice: the re-derivation reads the per-model threshold
# ---------------------------------------------------------------------------


def _stale_row(model: str, window: int, flag: bool | None) -> _StaleRow:
    return _StaleRow(
        file_key="k/s/s.jsonl", line_num=1, model=model, fresh_tokens=window,
        cache_creation_tokens=0, cache_read_tokens=0, output_tokens=100,
        eph5_tokens=0, eph1h_tokens=0, ts=None, long_context=flag,
        provider=None, cost_usd=Decimal(0), pricing_version="0",
        rate_fingerprint=None, request_fee_usd=None)


def test_reprice_derives_the_flag_from_the_model_threshold(monkeypatch):
    _install_tables(monkeypatch)
    # THE LEARNING TRANSITION (the review's C1): a pre-fold NULL row of a
    # newly learned member reprices to the band decision — what a reparse
    # would store — not kept flat.
    learned = _record_updates(_stale_row(MEMBER, THRESHOLD + 5_000, None))
    assert learned["long_context"] is True
    assert learned["cost_usd"] == pytest.approx(
        (205_000 * RATES["fresh"] * 5.0
         + 100 * RATES["output"] * 5.0) / 1_000_000)
    above = _record_updates(_stale_row(MEMBER, THRESHOLD + 5_000, False))
    assert above["long_context"] is True
    below = _record_updates(_stale_row(MEMBER, 5_000, False))
    assert below["long_context"] is False
    # The decision ignores the provider, because the parse's does: a
    # provider-tagged member row re-derives the same.
    prov_row = _record_updates(
        _StaleRow(**{**_stale_row(MEMBER, THRESHOLD + 5_000, None)._asdict(),
                     "provider": "Acme"}))
    assert prov_row["long_context"] is True
    # A NON-MEMBER row keeps its stored flag: the parse stores NULL for a
    # Claude-format non-member and the threshold test's result for a
    # Codex-format one, and a kept flag matches one of the two paths.
    non_member = _record_updates(_stale_row("claude-opus-4-7", 10_000_000, None))
    assert non_member["long_context"] is None
    kept_false = _record_updates(_stale_row("claude-opus-4-7", 10_000_000, False))
    assert kept_false["long_context"] is False


def test_reprice_unbills_a_lapsed_members_stored_true(monkeypatch):
    """Issue #833: the membership lapsed but the stored flag is the
    member era's TRUE — below the global threshold the one non-member
    shape no parse path stores. The reprice re-derives the Codex path's
    threshold test for a non-member's stored TRUE: the row unbills; a
    TRUE above the global threshold is the Codex path's own shape and
    stands; FALSE and NULL non-member rows keep (every path's shape)."""
    _install_tables(monkeypatch)
    lapsed = _record_updates(_stale_row("claude-opus-4-7", 250_000, True))
    assert lapsed["long_context"] is False
    codex_shaped = _record_updates(_stale_row("claude-opus-4-7", 300_000, True))
    assert codex_shaped["long_context"] is True
    kept_false = _record_updates(_stale_row("claude-opus-4-7", 250_000, False))
    assert kept_false["long_context"] is False
    kept_null = _record_updates(_stale_row("claude-opus-4-7", 250_000, None))
    assert kept_null["long_context"] is None


# ---------------------------------------------------------------------------
# rate_fingerprint: the meters ride the digest
# ---------------------------------------------------------------------------


def test_the_fingerprint_covers_the_meters(monkeypatch):
    monkeypatch.setattr(pricing, "LONG_CONTEXT_MODELS", MEMBERS)
    monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS", {
        MEMBER: {"threshold": THRESHOLD, "input_mult": 5.0,
                 "output_mult": 5.0}})
    rate_fingerprint.clear_fingerprint_cache()
    before = rate_fingerprint.pair_fingerprint(MEMBER, None)
    monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS", {
        MEMBER: {"threshold": THRESHOLD, "input_mult": 6.0,
                 "output_mult": 5.0}})
    rate_fingerprint.clear_fingerprint_cache()
    assert rate_fingerprint.pair_fingerprint(MEMBER, None) != before


def test_the_fingerprint_covers_membership(monkeypatch):
    monkeypatch.setattr(pricing, "LONG_CONTEXT_MODELS", frozenset())
    monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS", {})
    rate_fingerprint.clear_fingerprint_cache()
    before = rate_fingerprint.pair_fingerprint(MEMBER, None)
    monkeypatch.setattr(pricing, "LONG_CONTEXT_MODELS", MEMBERS)
    rate_fingerprint.clear_fingerprint_cache()
    assert rate_fingerprint.pair_fingerprint(MEMBER, None) != before
