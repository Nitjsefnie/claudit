"""Price Codex records at the rate in force when they ran.
Synthetic rollouts straddle rate boundaries without repurposing the committed
fixtures. Above-threshold records bill the long-context meter on every plan."""
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


def test_a_codex_record_is_priced_at_the_rate_of_its_own_timestamp(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A corpus spanning a synthetic rate boundary uses both windows.
    Controlled rows isolate timestamp selection from the live rate table."""
    dated_rates = {"fresh": 5.00, "create_5m": 6.25, "create_1h": 6.25,
                   "read": 0.50, "output": 30.00}
    list_rates = {"fresh": 4.00, "create_5m": 5.00, "create_1h": 5.00,
                  "read": 0.40, "output": 20.00}
    monkeypatch.setitem(pricing.MODEL_RATES, "gpt-5-6-sol", list_rates)
    monkeypatch.setitem(
        pricing.DATED_RATES, "gpt-5-6-sol",
        [(_to_dt("2026-08-21T19:00:00.000Z"), dated_rates)],
    )
    blob = _codex_usage_lines(snapshots=[
        ("2026-08-21T18:00:00.000Z", 1_000_000, 10_000, 100_000, 1_000),
        ("2026-08-21T20:00:00.000Z", 1_100_000, 11_000, 100_000, 1_000),
    ])
    out = parse.parse_file("codex/dated_rates.jsonl", blob)

    assert len(out["records"]) == 2
    before, after = out["records"]
    # 100k fresh + 1k output, at 5.00/30.00 then at 4.00/20.00.
    assert float(before["cost_usd"]) == 0.53
    assert float(after["cost_usd"]) == 0.42


@pytest.mark.parametrize("model, plan, file_key, check_fresh, check_membership, snapshots, flags", [
    pytest.param("gpt-5.6-sol", "pro", "codex/sub_long.jsonl", True, False,
                 [("2026-08-24T12:00:00.000Z", 1_000_000, 10_000, 300_000, 1_000)], [True], id="subscription"),
    pytest.param("gpt-5.6-sol", None, "codex/api_long.jsonl", False, False,
                 [("2026-08-24T12:00:00.000Z", 1_000_000, 10_000, 300_000, 1_000)], [True], id="no-plan"),
    pytest.param("gpt-5.5", "pro", "codex/gpt55_long.jsonl", True, True,
                 [("2026-08-24T12:00:00.000Z", 1_000_000, 10_000, 300_000, 1_000)], [True], id="gpt-5-5"),
    pytest.param("gpt-5.6-sol", "pro", "codex/per_record.jsonl", False, False,
                 [("2026-08-24T12:00:00.000Z", 1_000_000, 10_000, 100_000, 1_000),
                  ("2026-08-24T12:05:00.000Z", 1_300_000, 11_000, 300_000, 1_000)], [False, True], id="per-record"),
])
def test_records_bill_the_long_context_meter(  # pylint: disable=too-many-locals
        model, plan, file_key, check_fresh, check_membership, snapshots, flags):
    if check_membership:
        assert pricing.is_long_context_model(model)
    blob = _codex_usage_lines(model=model, plan=plan, snapshots=snapshots)
    out = parse.parse_file(file_key, blob)
    assert len(out["records"]) == len(snapshots)
    for rec, snapshot, metered_flag in zip(out["records"], snapshots, flags, strict=True):
        ts, _total_in, _total_out, fresh, output = snapshot
        if check_fresh:
            assert rec["fresh_tokens"] == 300_000 > pricing.LONG_CONTEXT_THRESHOLD
        assert rec["long_context"] is metered_flag
        cost = pricing.compute_cost(
            model, fresh=fresh, output=output,
            eph5=0, eph1h=0, unsplit_create=0, read=0,
            ts=_to_dt(ts), adjustments=pricing.CostAdjustments(long_context=metered_flag),
        )
        assert float(rec["cost_usd"]) == pytest.approx(round(cost, 6), rel=1e-9)


def test_a_stored_dotted_id_matches_its_dashed_membership_key(monkeypatch):
    """The reprice pass asks `is_long_context_model(row.model)` of the
    STORED id, which carries the corpus's dots; membership is priced-data
    keys in dashed spelling. The normalisation resolve() applies must
    therefore be the same one the membership check applies. Membership
    itself is patched synthetic (SV-TEST-DATA: it is repository data)."""
    monkeypatch.setattr(pricing, "LONG_CONTEXT_MODELS",
                        frozenset({"gpt-5-6-sol", "gpt-6-1-sol"}))
    assert pricing.is_long_context_model("gpt-5.6-sol")
    assert pricing.is_long_context_model("gpt-6.1-sol")
    assert not pricing.is_long_context_model("gpt-6-1-ghost")


def test_a_gpt_5_5_codex_record_prices_through_its_own_row():
    """gpt-5.5 has a models row: the dotted wire id resolves
    EXACT through the parser path, never the catch-all default. The
    expected cost is derived from the same loaded tables at the record's
    own ts, so the pin is twofold: the row's existence (kind exact) and
    the parser's agreement with it."""
    ts = _to_dt("2026-08-24T12:00:00.000Z")
    blob = _codex_usage_lines(model="gpt-5.5", snapshots=[
        ("2026-08-24T12:00:00.000Z", 1_000_000, 10_000, 100_000, 1_000),
    ])
    out = parse.parse_file("codex/gpt55_flat.jsonl", blob)

    rec = out["records"][0]
    assert rec["long_context"] is False
    resolved = pricing.resolve("gpt-5.5", ts, None)  # sv-test-data: allow (derived: the pinned claim is that the dotted id resolves exact, not the default — the value the row itself carries)
    assert resolved.kind == "exact"
    metered = pricing.compute_cost(
        "gpt-5.5", fresh=100_000, output=1_000,  # sv-test-data: allow (derived: expected priced at the record's own ts from the same loaded tables)
        eph5=0, eph1h=0, unsplit_create=0, read=0, ts=ts,
    )
    assert float(rec["cost_usd"]) == pytest.approx(round(metered, 6), rel=1e-9)
