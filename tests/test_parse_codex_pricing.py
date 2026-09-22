"""Codex records are priced at the rate in force WHEN they ran.

The GPT-5.6 family repriced twice — Terra and Luna on 2026-07-30, Sol on
2026-08-21 — so a corpus that spans either instant cannot be priced off one
table. The other half of the same story is the long-context meter, which is
an API-billing tier that a ChatGPT-plan rollout is never charged: its
subscription rate card has one short-context column and no long-context one.

Both are asserted on rollouts built here rather than on the fixtures under
fixtures/codex, which are dated 2026-06-14 and would have to be re-dated —
and thereby re-purposed — to straddle a boundary.
"""
import json
import pytest

from backend import parse, pricing
from backend.parse_common import _to_dt


def _rate_limits(plan: str) -> dict:
    """The rate_limits payload a subscription rollout carries on every
    token_count. plan_type is the only field the parser reads; the rest is
    here so the shape stays recognisable as the real one.
    """
    return {"limit_id": "codex", "plan_type": plan,
            "primary": {"used_percent": 7.0, "window_minutes": 10080},
            "rate_limit_reached_type": None,
            "spend_control_reached": None}


def _codex_usage_lines(*, snapshots, model="gpt-5.6-sol",
                       plan: str | None = "pro"):
    """A minimal rollout: one turn_context plus one token_count per snapshot.

    Each snapshot is (timestamp, total_input, total_output, last_input,
    last_output). `plan` is the ChatGPT plan the rollout declares; pass None
    for the API-key shape, which carries no rate_limits at all.
    """
    rate_limits = None if plan is None else _rate_limits(plan)
    lines = [{"timestamp": "2026-07-01T00:00:00.000Z", "type": "turn_context",
              "payload": {"model": model}}]
    for ts, tot_in, tot_out, last_in, last_out in snapshots:
        payload = {"type": "token_count", "info": {
            "total_token_usage": {
                "input_tokens": tot_in, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "output_tokens": tot_out,
                "reasoning_output_tokens": 0,
                "total_tokens": tot_in + tot_out},
            "last_token_usage": {
                "input_tokens": last_in, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "output_tokens": last_out,
                "reasoning_output_tokens": 0,
                "total_tokens": last_in + last_out},
            "model_context_window": 400000}}
        if rate_limits is not None:
            payload["rate_limits"] = rate_limits
        lines.append({"timestamp": ts, "type": "event_msg",
                      "payload": payload})
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


def test_a_codex_record_is_priced_at_the_rate_of_its_own_timestamp():
    """Sol went from 5/30 to 4/20 on 2026-08-21. A corpus spanning that
    instant priced entirely at today's table underbills everything before
    it — silently, because the row still carries a plausible number.
    """
    blob = _codex_usage_lines(snapshots=[
        ("2026-08-21T18:00:00.000Z", 1_000_000, 10_000, 100_000, 1_000),
        ("2026-08-21T20:00:00.000Z", 1_100_000, 11_000, 100_000, 1_000),
    ])
    out = parse.parse_file("codex/dated_rates.jsonl", blob)

    assert len(out["records"]) == 2
    before, after = out["records"]
    # 100k fresh + 1k output, at 5.00/30.00 then at 4.00/20.00.
    assert float(before["cost_usd"]) == pytest.approx(0.53, rel=1e-9)
    assert float(after["cost_usd"]) == pytest.approx(0.42, rel=1e-9)


def test_a_subscription_record_is_never_billed_on_the_long_context_meter():
    """Codex on a ChatGPT plan bills credits off one short-context rate
    card — it has no long-context column at all. Applying the API meter to
    a subscription rollout inflates its priciest requests by 2x input.
    """
    blob = _codex_usage_lines(snapshots=[
        ("2026-08-24T12:00:00.000Z", 1_000_000, 10_000, 300_000, 1_000),
    ])
    out = parse.parse_file("codex/sub_long.jsonl", blob)

    rec = out["records"][0]
    assert rec["fresh_tokens"] == 300_000 > pricing.LONG_CONTEXT_THRESHOLD
    flat = pricing.compute_cost(
        "gpt-5.6-sol", fresh=300_000, output=1_000,
        eph5=0, eph1h=0, unsplit_create=0, read=0,
        ts=_to_dt("2026-08-24T12:00:00.000Z"),
    )
    assert float(rec["cost_usd"]) == pytest.approx(round(flat, 6), rel=1e-9)


def test_a_rollout_with_no_plan_still_gets_the_long_context_meter():
    """The meter is real on the pay-as-you-go API; only the subscription is
    exempt. A rollout that declares no plan is the API shape, and dropping
    the meter for it would underbill by half on input.
    """
    blob = _codex_usage_lines(plan=None, snapshots=[
        ("2026-08-24T12:00:00.000Z", 1_000_000, 10_000, 300_000, 1_000),
    ])
    out = parse.parse_file("codex/api_long.jsonl", blob)

    rec = out["records"][0]
    metered = pricing.compute_cost(
        "gpt-5.6-sol", fresh=300_000, output=1_000,
        eph5=0, eph1h=0, unsplit_create=0, read=0,
        long_context=True, ts=_to_dt("2026-08-24T12:00:00.000Z"),
    )
    assert float(rec["cost_usd"]) == pytest.approx(round(metered, 6), rel=1e-9)


def test_the_subscription_verdict_is_sticky_across_the_file():
    """rate_limits rides every token_count in the corpus, but a payload
    that omits it must not flip a known subscription back onto the meter.
    """
    blob = _codex_usage_lines(snapshots=[
        ("2026-08-24T12:00:00.000Z", 1_000_000, 10_000, 100_000, 1_000),
    ])
    tail = _codex_usage_lines(plan=None, snapshots=[
        ("2026-08-24T12:05:00.000Z", 1_300_000, 11_000, 300_000, 1_000),
    ]).split(b"\n", 1)[1]
    out = parse.parse_file("codex/sticky.jsonl", blob + tail)

    rec = out["records"][-1]
    flat = pricing.compute_cost(
        "gpt-5.6-sol", fresh=300_000, output=1_000,
        eph5=0, eph1h=0, unsplit_create=0, read=0,
        ts=_to_dt("2026-08-24T12:05:00.000Z"),
    )
    assert float(rec["cost_usd"]) == pytest.approx(round(flat, 6), rel=1e-9)
