"""Tests moved from test_parse_kimi.py to keep test modules under 700 lines."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend import parse, parse_kimi
from backend.parse_kimi import _line_count

from tests.test_parse_kimi import (
    VALID_MODELS,
    _KC_CHURN_BLOB,
    _LEGACY_CHURN_BLOB,
    _churn_by_tool,
    _kc_blob,
    _kc_llm_error_blob,
    _read,
)


def test_legacy_records_split_across_the_model_cutoff():
    """Legacy transcripts have no model string, so dates are all we have —
    but applied per record, so a session straddling MODEL_CUTOFF splits
    instead of taking one label from its first event.
    """
    before = parse_kimi.MODEL_CUTOFF_EPOCH - 60
    after = parse_kimi.MODEL_CUTOFF_EPOCH + 60
    blob = b""
    for secs in (before, after):
        ts = datetime.fromtimestamp(secs, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        blob += (
            b'{"timestamp": "%s", "message": {"type": "StatusUpdate", "payload": '
            b'{"message_id": "m%d", "token_usage": {"input_other": 10, '
            b'"input_cache_creation": 0, "input_cache_read": 0, "output": 5}}}}\n'
            % (ts.encode(), secs)
        )
    out = parse.parse_file("sessions/projA/sess-split/wire.jsonl", blob)
    assert [r["model"] for r in out["records"]] == ["kimi-k2-6", "kimi-k2-7-code"]


def test_legacy_after_k3_cutoff_still_labels_k3():
    """No regression on the real legacy files that sit after the K3 cutoff:
    with no wire model, the date ladder's k3 rung still applies.
    """
    secs = parse_kimi.K3_CUTOFF_EPOCH + 3600
    ts = datetime.fromtimestamp(secs, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    blob = (
        b'{"timestamp": "%s", "message": {"type": "StatusUpdate", "payload": '
        b'{"message_id": "m1", "token_usage": {"input_other": 10, '
        b'"input_cache_creation": 0, "input_cache_read": 0, "output": 5}}}}\n'
        % ts.encode()
    )
    out = parse.parse_file("sessions/projA/sess-late/wire.jsonl", blob)
    assert out["records"][0]["model"] == "kimi-k3"


def test_parser_only_ever_emits_the_three_canonical_models():
    """There are exactly three real models. Anything else means a raw provider
    id leaked past canonicalisation — which is precisely what would bill at
    DEFAULT_RATES without any label looking obviously wrong.
    """
    base = parse_kimi.K3_CUTOFF_EPOCH - 3600
    blob = _kc_blob([
        (base, "kimi-code/k3"),
        (base + 1, "kimi-code/kimi-for-coding"),
        (base + 2, "kimi-code/k4-not-yet-invented"),
        (base + 3, None),
        (parse_kimi.MODEL_CUTOFF_EPOCH - 60, "kimi-code/k3"),
    ])
    out = parse.parse_file("sessions/projKC/sess-all/wire.jsonl", blob)
    assert out["records"], "fixture must produce records"
    for r in out["records"]:
        assert r["model"] in VALID_MODELS, r["model"]


def test_quota_exhausted_llm_error_is_recorded_as_a_rate_limit_hit():
    """The one event worth recording: a hard quota stop."""
    out = parse.parse_file(
        "sessions/projKC/sess-rl/wire.jsonl",
        _kc_llm_error_blob("quota_exhausted"),
    )
    assert len(out["rate_limit_hits"]) == 1
    hit = out["rate_limit_hits"][0]
    assert hit["line"] == 2
    assert hit["content"] == "You are out of quota."
    assert hit["ts"].startswith(
        datetime.fromtimestamp(
            parse_kimi.K3_CUTOFF_EPOCH + 1, tz=timezone.utc
        ).isoformat()[:19]
    )


def test_transient_rate_limit_llm_error_is_not_recorded():
    """kind="rate_limit" is the provider shaping traffic per minute, not the
    wall the user hits. claudit excludes the same case by text-matching "out
    of extra usage"; here the classification is a field, so the two dashboards
    count the same thing.
    """
    out = parse.parse_file(
        "sessions/projKC/sess-rl/wire.jsonl",
        _kc_llm_error_blob("rate_limit"),
    )
    assert out["rate_limit_hits"] == []


def test_llm_error_content_is_capped_at_500_chars():
    """Provider messages are unbounded free text. The producer truncates at
    500 (LLM_ERROR_MESSAGE_MAX_LENGTH) but the journal is not ours to trust.
    """
    out = parse.parse_file(
        "sessions/projKC/sess-rl/wire.jsonl",
        _kc_llm_error_blob("quota_exhausted", "x" * 900),
    )
    assert len(out["rate_limit_hits"][0]["content"]) == 500


def test_kimi_code_edit_write_churn_from_call_args():
    """The wire's tool RESULT carries no diff ("Replaced 1 occurrence in
    <path>"), so added/deleted line counts come from the call's args:
    Edit -> lines(new_string) / lines(old_string), Write -> lines(content).
    """
    out = parse.parse_file(
        "sessions/projLC/sess-lc/wire.jsonl", _KC_CHURN_BLOB)
    assert _churn_by_tool(out) == [
        ("Edit", False, 4, 3),     # "a\nb\nc" -> "a\nB\nc\nd"
        ("Write", False, 2, 0),    # content "x\ny\n"; overwrite size unknowable
        ("Edit", True, 0, 0),      # is_error -> the rejected edit changed nothing
        ("Bash", False, 0, 0),     # not a file-mutating tool
    ]


def test_legacy_str_replace_and_write_churn():
    """Legacy StrReplaceFile takes {edit: {old, new}} OR {edit: [edits]};
    WriteFile contributes added lines only."""
    out = parse.parse_file(
        "sessions/projLC/sess-lcl/wire.jsonl", _LEGACY_CHURN_BLOB
    )
    assert _churn_by_tool(out) == [
        ("StrReplaceFile", False, 1, 2),  # single edit object
        ("StrReplaceFile", False, 4, 3),  # list of edits, summed
        ("WriteFile", False, 2, 0),
        ("WriteFile", True, 0, 0),        # is_error -> zeroed
    ]


def test_no_edit_tools_means_zero_churn_everywhere():
    """The empty/no-churn case: a file with no file-mutating calls parses
    with explicit zeros, not missing keys, so ingest can insert blindly."""
    out = parse.parse_file(
        "sessions/projA/sess-A/wire.jsonl", _read("kimi_single_turn.jsonl")
    )
    assert out["tool_uses"] == []
    out = parse.parse_file(
        "sessions/projErr/sess-err/wire.jsonl", _read("kimi_tool_error.jsonl")
    )
    assert len(out["tool_uses"]) == 1
    tu = out["tool_uses"][0]
    assert tu["lines_added"] == 0
    assert tu["lines_deleted"] == 0


def test_line_count_conventions():
    """Trailing newline terminates, a final partial line still counts."""
    assert _line_count("") == 0
    assert _line_count(None) == 0
    assert _line_count("a") == 1
    assert _line_count("a\n") == 1
    assert _line_count("a\nb") == 2
    assert _line_count("a\nb\n") == 2


def test_claude_detection_survives_a_leading_unrecognized_line():
    """Detection scans until a line identifies the format, so a sidecar
    record Claude Code adds in some future version cannot make the file
    fall through to a lane parser and come back empty."""
    blob = (b'{"type":"some-future-record","payload":{}}\n'
            b'{"sessionId":"s1","type":"user","message":{"role":"user",'
            b'"content":"hi"}}\n')
    assert parse.sniff_format(blob) == "claude"


def test_file_history_lines_are_claude_even_without_a_session_id():
    """The two file-history record types carry no sessionId; their names
    carry the identification instead. The line is followed by a Codex
    session_meta record so the rung is load-bearing: without it the blob
    would sniff codex (the claude catch-all answers "claude" either
    way), not claude."""
    blob = (b'{"type":"file-history-delta","messageId":"m1","delta":{}}\n'
            b'{"timestamp":"2026-09-10T08:44:15Z","type":"session_meta",'
            b'"payload":{"session_id":"00000000-0000-4000-8000-0000000000c0"}}\n')
    assert parse.sniff_format(blob) == "claude"


@pytest.mark.parametrize("name", [
    "kimi_single_turn.jsonl", "kimi_tool_error.jsonl", "kimi_tool_success.jsonl",
])
def test_native_fixtures_are_not_mistaken_for_claude(name):
    """Parsing succeeds on the lane formats' own fixtures."""
    out = parse.parse_file("sessions/p/s/wire.jsonl", _read(name))
    assert isinstance(out["records"], list)
