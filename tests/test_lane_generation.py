"""Lane generation windows and Codex wire timing boundaries."""
from __future__ import annotations

import json
from pathlib import Path

from backend import flags, parse


FIX = Path(__file__).resolve().parents[1] / "fixtures" / "codex"


def test_codex_generation_orderings():
    for ordering, expected in [
        ("new", [(None, None), (6, 4), (8, 5)]),
        ("old", [(6, 4), (8, 5), (None, None)]),
    ]:
        blob = (FIX / f"rollout_generation_{ordering}_ordering.jsonl").read_bytes()
        records = parse.parse_file("codex/synthetic.jsonl", blob)["records"]
        assert [(r["generation_s"], r["output_tokens_per_s"]) for r in records] == expected


def test_codex_generation_search_only_duplicate_preserves_window():
    rows = [json.loads(line) for line in
            (FIX / "rollout_generation_new_ordering.jsonl").read_bytes().splitlines()]
    output_index = next(i for i, row in enumerate(rows)
                        if row["payload"].get("type") == "custom_tool_call_output")
    rows.insert(output_index, {
        "timestamp": "2026-06-14T12:00:06Z", "type": "response_item",
        "payload": {"type": "web_search_call"},
    })
    rows.insert(output_index + 2, rows[output_index + 1] | {"timestamp": None})
    records = _records(rows)
    assert [r["web_search_requests"] for r in records] == [None, 1, None, None]
    assert [r["output_tokens"] for r in records] == [12, 0, 24, 40]
    assert [r["generation_s"] for r in records] == [None, None, 6, None]
    assert [r["output_tokens_per_s"] for r in records] == [None, None, 4, None]
    policy = flags.resolve_config({"CLAUDIT_COLLECT_GENERATION_SPEED": "0"}).collection
    records = _records(rows, policy)
    assert [r["reply_latency_s"] for r in records] == [1, None, None, None]
    assert all(r["generation_s"] is r["output_tokens_per_s"] is None for r in records)


def _records(rows, collection=flags.ALL_COLLECTIONS_ON):
    blob = b"\n".join(json.dumps(row).encode() for row in rows)
    return parse.parse_file("codex/synthetic.jsonl", blob, collection=collection)["records"]


def _codex_generation_rows(response_type, payload, end):
    rows = [json.loads(line) for line in
            (FIX / "rollout_generation_old_ordering.jsonl").read_bytes().splitlines()]
    return rows[:4] + [{"timestamp": end, "type": response_type, "payload": payload},
                       rows[7] | {"timestamp": "2026-06-14T13:00:02Z"}]


def test_codex_generation_responses_and_limits():
    for rtype, payload in [
        ("response_item", {"type": "message", "role": "assistant"}),
        ("response_item", {"type": "agent_message"}),
        ("response_item", {"type": "reasoning"}),
        ("response_item", {"type": "web_search_call"}),
        ("response_item", {"type": "local_shell_call"}),
        ("event_msg", {"type": "agent_message"}),
        ("event_msg", {"type": "agent_reasoning"}),
        ("event_msg", {"type": "item_completed", "item": {"type": "Reasoning"}}),
    ]:
        rows = _codex_generation_rows(rtype, payload, "2026-06-14T12:00:05Z")
        rows.insert(4, {"timestamp": "2026-06-14T12:00:01Z", "type": "response_item",
                        "payload": {"type": "reasoning"}})
        record, = _records(rows)
        assert (record["generation_s"], record["output_tokens_per_s"]) == (5, 4.8)
    for end, seconds, speed in [
        ("2026-06-14T13:00:00Z", 3600, 24 / 3600),
        ("2026-06-14T13:00:01Z", None, None),
        ("2026-06-14T12:00:00Z", None, None),
        ("2026-06-14T11:59:59Z", None, None),
        (None, None, None),
    ]:
        record, = _records(_codex_generation_rows("response_item", {"type": "reasoning"}, end))
        assert (record["generation_s"], record["output_tokens_per_s"]) == (seconds, speed)
