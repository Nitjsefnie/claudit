"""Codex and Kimi transcripts through claudit's parse_file (D2, D3, D7)."""
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend import db, ingest, parse
from backend.r2 import R2Object

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"
CLAUDIT_RECORD_KEYS = {
    "file_key", "line_num", "uuid", "request_id", "ts", "model",
    "fresh_tokens", "cache_creation_tokens", "cache_read_tokens",
    "output_tokens", "text_chars", "reply_latency_s", "stop_reason",
    "effort", "thinking_tokens", "cli_version", "turn_flags",
    "turn_tool_results", "eph5_tokens", "eph1h_tokens", "cost_usd",
}


@pytest.mark.parametrize("name,fmt", [
    ("codex_min.jsonl", "codex"),
    ("kimi_code_min.jsonl", "kimi-code"),
    ("kimi_legacy_min.jsonl", "legacy"),
])
def test_each_lane_format_is_sniffed(name, fmt):
    assert parse.sniff_format((FIX / name).read_bytes()) == fmt


def test_a_claude_transcript_still_sniffs_as_claude():
    assert parse.sniff_format((FIX / "unsplit_cache.jsonl").read_bytes()) == "claude"


@pytest.mark.parametrize("name", [
    "codex_min.jsonl", "kimi_code_min.jsonl", "kimi_legacy_min.jsonl"])
def test_lane_records_carry_every_column_claudit_persists(name):
    out = parse.parse_file(f"sessions/p/s/{name}", (FIX / name).read_bytes())
    assert out["records"], "fixture must hold at least one usage record"
    for r in out["records"]:
        assert CLAUDIT_RECORD_KEYS <= set(r), CLAUDIT_RECORD_KEYS - set(r)
        assert r["eph5_tokens"] == r["eph1h_tokens"] == 0
        assert r["turn_flags"] == []
    assert {"prompt_count", "models", "agent_type"} <= set(out)


def test_codex_reasoning_lands_in_thinking_tokens():
    out = parse.parse_file("sessions/p/s/wire.jsonl",
                           (FIX / "codex_min.jsonl").read_bytes())
    r = out["records"][0]
    assert 0 < r["thinking_tokens"] <= r["output_tokens"]
    assert "reasoning_output_tokens" not in r


@pytest.mark.parametrize("raw,label", [
    ("gpt-6-sol", "gpt-6-sol"), ("GPT-6-Sol", "gpt-6-sol"),
    ("gpt-6-luna", "gpt-6-luna"), ("gpt-5.6-sol", "gpt-5.6-sol"),
    ("gpt-5.6-luna", "gpt-5.6-luna"), ("gpt-5.6-terra", "gpt-5.6-terra"),
    ("gpt-6-astra", "gpt-6-astra"),
])
def test_codex_model_map_keeps_the_generations_apart(raw, label):
    from backend import parse_codex  # pylint: disable=import-outside-toplevel
    assert parse_codex._codex_model(raw) == label  # pylint: disable=protected-access


def test_lane_tool_uses_carry_claudit_columns():
    out = parse.parse_file("sessions/p/s/wire.jsonl",
                           (FIX / "codex_min.jsonl").read_bytes())
    tu = out["tool_uses"][0]
    for key in ("tool_use_id", "error_kind", "error_text", "agent_type",
                "agent_model", "dispatch_prompt_chars", "dispatch_brief_ref",
                "result_chars", "read_kind", "read_targets", "write_targets",
                "is_reread"):
        assert key in tu, key
    assert "tool_call_id" not in tu


def test_a_claude_line_with_a_string_message_sniffs_claude():
    """A non-dict "message" must not crash the legacy rung (it used to
    raise AttributeError on .get); the file keeps sniffing claude."""
    blob = b'{"sessionId":"x","type":"system","message":"hi"}\n'
    assert parse.sniff_format(blob) == "claude"


def test_a_kimi_code_llm_error_line_with_a_string_message_does_not_crash():
    """kimi-code's llm.error carries a string message. A fragment leading
    with one falls through the rungs to the claude catch-all instead of
    raising -- an unidentifiable fragment parses empty either way, which
    is the pre-dispatch behaviour."""
    blob = (b'{"type":"llm.error","time":1784213155000,'
            b'"kind":"quota_exhausted","message":"out of quota"}\n')
    assert parse.sniff_format(blob) == "claude"
    assert parse.parse_file("s/f/llm.jsonl", blob)["records"] == []


@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    """Per-test schema reset on a separate DB (tests/test_ingest.py's)."""
    test_db = "claudit_test"
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")
    os.system(f"createdb {test_db} 2>/dev/null")
    os.system(f"psql {test_db} -f {Path(__file__).resolve().parents[1] / 'backend/schema.sql'} >/dev/null")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    db.reset_viz_pool()
    yield
    db.reset_viz_pool()
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")


@pytest.mark.parametrize("name,tool_use_id", [
    ("codex_min.jsonl", "call_synthetic01"),
    ("kimi_code_min.jsonl", "tool_Synthetic01Example"),
    # Legacy ids are per-session sequences ("tc1" repeats across unrelated
    # sessions in the kimi bucket), so the adapter namespaces them with the
    # file key before ingest groups tool_uses by the id ACROSS files.
    ("kimi_legacy_min.jsonl", "sessions/p/s/wire.jsonl:tc1"),
])
def test_a_lane_transcript_persists_through_ingest(fresh_db, name, tool_use_id):
    """Round trip: parse_file -> ingest._persist must land rows in files,
    records and tool_uses. Guards the schema's NOT NULLs -- request_id is
    NOT NULL DEFAULT '' and files.agent_type is NOT NULL -- which None
    values from the adapter used to violate for every lane format.

    The key is the real lane shape (wire.jsonl under sessions/): since
    key_layout owns the mapping, a non-wire basename inside sessions/ is
    no longer a transcript at all."""
    key = "sessions/p/s/wire.jsonl"
    out = parse.parse_file(key, (FIX / name).read_bytes())
    assert out["records"] and out["tool_uses"]
    when = datetime(2026, 9, 22, tzinfo=timezone.utc)
    ingest._persist(  # pylint: disable=protected-access
        R2Object(key=key, etag="etag", size=1024, last_modified=when),
        {"project_id": "p", "display_name": "p",
         "first_seen_at": when, "last_seen_at": when},
        out, "50",
    )
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT agent_type, prompt_count, models FROM files "
            "WHERE file_key = %s", (key,)).fetchone()
        assert row is not None, "no files row"
        assert row[0] == "general-purpose"
        n_records = c.execute(
            "SELECT COUNT(*) FROM records WHERE file_key = %s", (key,)
        ).fetchone()
        assert n_records is not None and n_records[0] == len(out["records"])
        empty_request_ids = c.execute(
            "SELECT COUNT(*) FROM records WHERE file_key = %s "
            "AND request_id <> ''", (key,)).fetchone()
        assert empty_request_ids is not None and empty_request_ids[0] == 0
        ids = [r[0] for r in c.execute(
            "SELECT tool_use_id FROM tool_uses WHERE file_key = %s "
            "ORDER BY idx", (key,)).fetchall()]
    assert ids == [tu["tool_use_id"] for tu in out["tool_uses"]]
    assert tool_use_id in ids
