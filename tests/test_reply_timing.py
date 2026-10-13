"""Claude reply timing: injected text anchors, failed attempts invalidate."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from orjson import dumps, loads
import pytest

from backend import db, flags, ingest, parse, parser_version, r2
from backend.flags import ALL_COLLECTIONS_ON, CollectionPolicy
from tests import scratch_db

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"


def test_injected_message_anchors_latency():
    out = parse.parse_file(
        "k/sess/sess.jsonl", (FIX / "injected_message_anchors_latency.jsonl").read_bytes())
    assert out["records"][0]["reply_latency_s"] == pytest.approx(10.0)
    assert out["prompt_count"] == 1
    assert out["prompt_ts"] == ["2026-06-23T10:00:00+00:00"]
    assert out["turn_count"] == 1
    assert out["ctx_turns"] == [{
        "idx": 1, "ts": "2026-06-23T11:00:10+00:00", "line": 3,
        "input": 100, "output": 10, "delta": 100,
    }]


def test_injected_list_message_anchors_latency():
    out = parse.parse_file(
        "k/sess/sess.jsonl", (FIX / "injected_list_message_anchors_latency.jsonl").read_bytes())
    assert out["records"][0]["reply_latency_s"] == pytest.approx(10.0)
    assert out["prompt_count"] == 1
    assert out["prompt_ts"] == ["2026-06-23T10:00:00+00:00"]
    assert out["turn_count"] == 1
    assert out["ctx_turns"] == [{
        "idx": 1, "ts": "2026-06-23T11:00:10+00:00", "line": 3,
        "input": 100, "output": 10, "delta": 100,
    }]


def test_api_error_nulls_reply_latency():
    out = parse.parse_file(
        "k/sess/sess.jsonl", (FIX / "api_error_nulls_reply_latency.jsonl").read_bytes())
    first, second = out["records"]
    assert first["reply_latency_s"] is None
    assert second["reply_latency_s"] == pytest.approx(10.0)
    assert "api_error" in first["turn_flags"]
    assert "api_error" not in second["turn_flags"]


def test_other_system_preserves_reply_latency():
    out = parse.parse_file(
        "k/sess/sess.jsonl", (FIX / "other_system_preserves_reply_latency.jsonl").read_bytes())
    assert out["records"][0]["reply_latency_s"] == pytest.approx(10.0)


def _parse_fixture(name: str, *, list_text: bool = False,
                   collection: CollectionPolicy = ALL_COLLECTIONS_ON) -> dict:
    blob = (FIX / f"{name}.jsonl").read_bytes()
    if list_text:
        rows = [loads(line) for line in blob.splitlines()]
        for row in rows:
            if row["type"] == "user":
                text = row["message"]["content"]
                row["message"]["content"] = [{"type": "text", "text": text}]
        blob = b"\n".join(dumps(row) for row in rows)
    return parse.parse_file("k/sess/sess.jsonl", blob, collection=collection)


def test_generation_s_last_line():
    first, second = _parse_fixture("generation_s_last_line")["records"]
    assert first["generation_s"] == 9
    assert first["output_tokens_per_s"] == 10
    assert first["output_tokens"] == 120  # max-merged billing count differs
    assert first["reply_latency_s"] == 2
    assert second["generation_s"] == 5
    assert second["output_tokens_per_s"] == 6
    assert second["reply_latency_s"] is None


@pytest.mark.parametrize("name", ["generation_s_failed_attempt", "generation_s_error_stub"])
def test_generation_s_failed_attempt(name):
    first, second = _parse_fixture(name)["records"]
    assert first["generation_s"] is None
    assert first["output_tokens_per_s"] is None
    assert second["generation_s"] == 4
    assert second["output_tokens_per_s"] == 10


@pytest.mark.parametrize("name", ["generation_s_mid_system_failure", "generation_s_mid_stub_failure"])
def test_generation_s_failure_inside_span(name):
    record, = _parse_fixture(name)["records"]
    assert record["generation_s"] is None
    assert record["output_tokens_per_s"] is None


def test_generation_s_consumed_anchor():
    first, second = _parse_fixture("generation_s_consumed_anchor")["records"]
    assert first["generation_s"] == 2
    assert second["generation_s"] is None
    assert second["output_tokens_per_s"] is None


def test_generation_s_continuation_anchor():
    records = _parse_fixture("generation_s_continuation_anchor")["records"]
    assert [r["generation_s"] for r in records] == [9, 6]
    assert [r["output_tokens_per_s"] for r in records] == [10, 10]


def test_generation_s_no_last_usage():
    record, = _parse_fixture("generation_s_no_last_usage")["records"]
    assert record["generation_s"] == 5
    assert record["output_tokens_per_s"] is None


def test_generation_s_no_id():
    record, = _parse_fixture("generation_s_no_id")["records"]
    assert record["generation_s"] is None
    assert record["output_tokens_per_s"] is None


def test_generation_s_idless_line_keeps_anchor():
    rows = [loads(line) for line in (FIX / "generation_s_consumed_anchor.jsonl").read_bytes().splitlines()]
    rows[1]["message"].pop("id")
    first, second = parse.parse_file("k/sess/sess.jsonl", b"\n".join(map(dumps, rows)))["records"]
    assert first["generation_s"] is None
    assert second["generation_s"] == 5
    assert second["output_tokens_per_s"] == 2


@pytest.mark.parametrize("user_message", [{"content": "go"}, None])
def test_generation_s_user_line_without_role(user_message):
    rows = [loads(line) for line in (FIX / "generation_s_no_id.jsonl").read_bytes().splitlines()]
    rows[0]["message"] = user_message
    rows[-1]["message"]["id"] = "m1"
    record, = parse.parse_file("k/sess/sess.jsonl", b"\n".join(map(dumps, rows)))["records"]
    assert record["generation_s"] == 2
    assert record["output_tokens_per_s"] == 5
    assert record["reply_latency_s"] is None


@pytest.mark.parametrize("stamp,expected", [
    ("2026-06-23T11:00:00Z", 3600), ("2026-06-23T11:00:00.001Z", None),
    ("2026-06-23T10:00:00Z", None), ("2026-06-23T09:59:59Z", None),
    ("", None),
])
def test_generation_s_valid_duration(stamp, expected):
    rows = [loads(line) for line in (FIX / "generation_s_no_id.jsonl").read_bytes().splitlines()]
    rows[-1]["message"]["id"] = "m1"
    rows[-1]["timestamp"] = stamp
    record, = parse.parse_file("k/sess/sess.jsonl", b"\n".join(map(dumps, rows)))["records"]
    assert record["generation_s"] == expected
    assert record["output_tokens_per_s"] == (10 / 3600 if expected else None)


@pytest.mark.parametrize("name", ["generation_s_last_line", "non_object_nested_claude"])
def test_generation_speed_collection_off(name):
    enabled = _parse_fixture(name)
    disabled = _parse_fixture(name, collection=flags.resolve_config(
        {"CLAUDIT_COLLECT_GENERATION_SPEED": "0"}).collection)
    assert enabled["records"][0]["generation_s"] == 9
    for record in disabled["records"]:
        assert record["generation_s"] is None
        assert record["output_tokens_per_s"] is None
    for result in (enabled, disabled):
        for record in result["records"]:
            del record["generation_s"], record["output_tokens_per_s"]
    assert disabled == enabled


def test_generation_speed_conflicting_role():
    outputs = []
    for flag in (True, False):
        collection = flags.resolve_config({"CLAUDIT_COLLECT_GENERATION_SPEED": str(int(flag))}).collection
        result = _parse_fixture("generation_speed_conflicting_role", collection=collection)
        for record in result["records"]:
            del record["generation_s"], record["output_tokens_per_s"]
        outputs.append(result)
    assert outputs[0] == outputs[1]
    assert [(r["line_num"], r["reply_latency_s"], r["output_tokens"])
            for r in outputs[0]["records"]] == [(2, 2, 10), (3, None, 10)]


@pytest.mark.parametrize("value,expected", [
    (None, True), ("", False), ("0", False), ("false", False),
    ("no", False), ("FALSE", False), ("1", True), ("other", True),
])
def test_generation_speed_startup_switch(value, expected):
    env = {} if value is None else {"CLAUDIT_COLLECT_GENERATION_SPEED": value}
    assert flags.resolve_config(env).collection.enabled("generation_speed") is expected


@pytest.mark.parametrize("name", ["codex_min.jsonl", "kimi_code_min.jsonl", "kimi_legacy_min.jsonl"])
def test_generation_speed_lanes_null(name):
    records = parse.parse_file("sessions/p/s/wire.jsonl", (FIX / name).read_bytes())["records"]
    assert records
    for record in records:
        assert record["generation_s"] is None
        assert record["output_tokens_per_s"] is None


@pytest.mark.parametrize("list_text", [False, True])
def test_replayed_injected_latency(list_text):
    record, = _parse_fixture("replayed_injected_latency", list_text=list_text)["records"]
    assert record["reply_latency_s"] == 10
    assert record["generation_s"] == 10


@pytest.mark.parametrize("list_text", [False, True])
def test_whitespace_injected_latency(list_text):
    record, = _parse_fixture("whitespace_injected_latency", list_text=list_text)["records"]
    assert record["reply_latency_s"] == 10
    assert record["generation_s"] == 2


@pytest.mark.parametrize("list_text", [False, True])
def test_interrupt_injected_latency(list_text):
    record, = _parse_fixture("interrupt_injected_latency", list_text=list_text)["records"]
    assert record["reply_latency_s"] is None
    assert record["generation_s"] == 2


@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    yield from scratch_db.scratch_viz_database(monkeypatch, "timing")


def test_generation_speed_persisted(fresh_db):
    key = "claude/k/sess/sess.jsonl"
    blob = (FIX / "generation_s_last_line.jsonl").read_bytes()
    parsed = parse.parse_file(key, blob)
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    ingest._persist(  # pylint: disable=protected-access
        r2.R2Object(key, "timing-fixture", len(blob), now),
        {"project_id": "k", "display_name": "k", "first_seen_at": now, "last_seen_at": now},
        parsed, parser_version.PARSER_VERSION,
    )
    with db.viz_conn() as conn:
        rows = conn.execute(
            "SELECT generation_s, output_tokens_per_s FROM records r "
            "JOIN files f ON f.file_id = r.file_id "
            "WHERE f.file_key = %s ORDER BY r.line_num",
            (key,),
        ).fetchall()
    assert rows == [(9, 10), (5, 6)]
