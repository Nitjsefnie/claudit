"""Value-rich input and independent storage expectations for the fact writer."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

from backend import constants, db, pricing, rate_fingerprint
from tests import dims_seed


def persist_fact_fixture(monkeypatch):
    ts = datetime(2026, 5, 7, 12, 34, 56, 123456, tzinfo=timezone.utc)
    uid = "01234567-89ab-4cde-8fab-0123456789ab"
    monkeypatch.setattr(pricing, "long_context_factors", lambda _model: (2.25, 1.75))
    dim_values = {
        "model": "copy-model-雪", "provider": "copy-host-ž",
        "stop_reason": "end_turn", "effort": "high", "cli_version": "copy-cli",
        "tool_name": "Read", "error_kind": "tool_error",
        "agent_type": "explorer", "agent_model": "copy-agent-猫",
        "pricing_version": constants.PRICING_VERSION,
        "rate_fingerprint": rate_fingerprint.pair_fingerprint("copy-model-雪", "copy-host-ž"),
    }
    with db.viz_conn() as c:
        ids = {f"{name}_id": dims_seed.dim_id(c, f"dim_{name}", value)
               for name, value in dim_values.items()}
        no_provider_fp = dims_seed.dim_id(
            c, "dim_rate_fingerprint",
            rate_fingerprint.pair_fingerprint("copy-model-雪", None))
    record = {
        "line_num": 11, "uuid": uid, "request_id": "", "ts": ts,
        "model": dim_values["model"], "provider": dim_values["provider"],
        "fresh_tokens": 101, "cache_creation_tokens": 202,
        "cache_read_tokens": 303, "cache_write_tokens": 404,
        "output_tokens": 505, "eph5_tokens": 606, "eph1h_tokens": 707,
        "web_search_requests": 8, "text_chars": 909,
        "reply_latency_s": 1.125, "generation_s": 2.25, "output_tokens_per_s": 3.5,
        "stop_reason": "end_turn", "effort": "high", "thinking_tokens": 99,
        "cli_version": "copy-cli", "turn_flags": ["interrupt", "user_prompt"],
        "turn_tool_results": 12, "is_replay": True,
        "long_context": True, "cost_usd": 0.01234567,
    }
    tool = {
        "line_num": 21, "idx": 3, "ts": ts, "tool_name": "Read",
        "model": dim_values["model"], "tool_use_id": "tool-雪\\\t\n",
        "is_error": True, "error_kind": "tool_error", "error_text": "échec 雪\t\n\\N",
        "lines_added": 13, "lines_deleted": 17, "agent_type": "explorer",
        "agent_model": dim_values["agent_model"], "dispatch_prompt_chars": 123,
        "dispatch_brief_ref": True, "dispatch_name": "équipe 猫",
        "result_chars": 456, "read_kind": "whole",
        "read_targets": ["/tmp/雪,猫", 'quote"\\tab\tline\n', "", "NULL"],
        "write_targets": [], "is_reread": True,
    }
    records = [
        record,
        {**record, "line_num": 12, "uuid": uid + ":37",
         "request_id": "req-猫", "long_context": False,
         "turn_flags": [], "is_replay": False, "cost_usd": 0.00123456,
         "reply_latency_s": 1.2344999999999999},
        {**record, "line_num": 13, "uuid": "fallback-雪\\\t\n",
         "request_id": "", "provider": None, "stop_reason": None,
         "effort": None, "cli_version": None, "thinking_tokens": 0,
         "web_search_requests": None, "reply_latency_s": None,
         "generation_s": None, "output_tokens_per_s": None,
         "turn_flags": None, "turn_tool_results": None,
         "cache_write_tokens": None, "long_context": None, "is_replay": None,
         "cost_usd": 0.012345499999999998},
    ]
    tools = [tool, {**tool, "line_num": 22, "read_kind": "slice", "read_targets": [],
                    "write_targets": ["/tmp/écrit"], "is_error": False,
                    "error_kind": None, "error_text": None, "is_reread": False,
                    "is_replay": False},
             {**tool, "line_num": 23, "read_kind": None, "read_targets": None,
              "write_targets": None, "model": None, "tool_use_id": None,
              "is_error": False, "error_kind": None, "error_text": None,
              "agent_type": None, "agent_model": None, "dispatch_prompt_chars": None,
              "dispatch_brief_ref": None, "dispatch_name": None,
              "result_chars": None, "is_reread": None, "is_replay": True}]
    expected = _fact_expectations(records, tools, ids, no_provider_fp, uid)
    obj = SimpleNamespace(key="claude/copy-project/copy-session/copy-session.jsonl",
                          etag="copy-etag", size=123, last_modified=ts)
    proj = {"project_id": "copy-project", "display_name": "Projet 雪",
            "first_seen_at": ts, "last_seen_at": ts}
    parsed = {"records": records, "tool_uses": tools, "ctx_turns": [],
              "turn_count": 0, "prompt_count": 0}
    return obj, proj, parsed, expected


def _fact_expectations(records, tools, ids, no_provider_fp, uid):
    expected = {"records": [], "tool_uses": [], "records_price": []}
    for i, rec in enumerate(records):
        stored = {k: deepcopy(v) for k, v in rec.items() if k not in (
            "uuid", "cost_usd", "long_context", "model", "provider", "stop_reason",
            "effort", "cli_version")}
        stored.update({k: ids[k] if rec[k[:-3]] is not None else None for k in (
            "model_id", "provider_id", "stop_reason_id", "effort_id", "cli_version_id")})
        stored.update(uuid_col=UUID(uid) if i < 2 else None,
                      seq=37 if i == 1 else None,
                      fallback="fallback-雪\\\t\n" if i == 2 else None,
                      turn_flags=[2050, 0, None][i], is_canonical=True,
                      reply_latency_s=[Decimal("1.125"), Decimal("1.235"), None][i])
        expected["records"].append(stored)
        expected["records_price"].append({
            "line_num": rec["line_num"],
            "cost_usd": [Decimal("0.012346"), Decimal("0.001235"), Decimal("0.012346")][i],
            "long_context": [True, False, None][i],
            "long_context_input_mult": 2.25 if i == 0 else None,
            "long_context_output_mult": 1.75 if i == 0 else None,
            "request_fee_usd": None, "pricing_version_id": ids["pricing_version_id"],
            "rate_fingerprint_id": ids["rate_fingerprint_id"] if i < 2 else no_provider_fp,
        })
    for i, tu in enumerate(tools):
        stored = {k: deepcopy(v) for k, v in tu.items() if k not in (
            "model", "tool_name", "error_kind", "agent_type", "agent_model")}
        stored.update({k: ids[k] if tu[k[:-3]] is not None else None for k in (
            "model_id", "tool_name_id", "error_kind_id", "agent_type_id", "agent_model_id")})
        stored.update(read_kind=[True, False, None][i],
                      read_targets=deepcopy(tools[0]["read_targets"]) if i == 0 else None,
                      write_targets=["/tmp/écrit"] if i == 1 else None,
                      is_replay=[None, False, True][i], is_canonical=True)
        expected["tool_uses"].append(stored)
    for line, (value, metric, cost) in enumerate([
        (0.0000005, "0.000", "0.000001"), (1.0000005, "1.000", "1.000001"),
        (12.3456785, "12.346", "12.345679"), (0.1, "0.100", "0.100000"),
        (2.675, "2.675", "2.675000"),
        (Decimal("0.0000005"), "0.000", "0.000001"),
        ("12.3456785", "12.346", "12.345679"),
    ], 14):
        records.append({**records[0], "line_num": line, "cost_usd": value,
                        "reply_latency_s": value, "generation_s": value,
                        "output_tokens_per_s": value, "long_context": False})
        expected["records"].append({
            **expected["records"][0], "line_num": line,
            "reply_latency_s": Decimal(metric), "generation_s": Decimal(metric),
            "output_tokens_per_s": Decimal(metric)})
        expected["records_price"].append({
            **expected["records_price"][0], "line_num": line, "cost_usd": Decimal(cost), "long_context": False,
            "long_context_input_mult": None, "long_context_output_mult": None})
    return expected
