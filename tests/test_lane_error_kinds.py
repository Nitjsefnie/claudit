"""Lane errored tool calls classify into the harness-generic kinds.

SV-WHY-COLUMNS defines error_kind for every harness; before this the
lane paths stored NULL on every row, so lane failures counted in the
Tool Error Rate (keyed on is_error) but vanished from every error-kind
breakdown (rebuild_tool_error_rollup filters error_kind IS NOT NULL).
The lanes carry no status field — an errored result is what the wire
says — so the classification reads the failure text: a call that RAN
and reported failure is tool_error, the harness-generic rejection
wording is rejected, and an errored result with no readable text stays
failed. Non-errored calls keep NULL.
"""
from __future__ import annotations

import json

import pytest

from backend import parse
from backend.tool_errors import ERROR_TEXT_MAX, classify_lane_error

UTC_ISO = "2026-06-14T12:00:00.000Z"


def _tool_uses(blob: bytes) -> list[dict]:
    return parse.parse_file("sessions/p/s/wire.jsonl", blob)["tool_uses"]


# --------------------------------------------------------------------------
# The classifier itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text,kind", [
    ("The user doesn't want to proceed with this tool use", "rejected"),
    ("Exit code 1", "tool_error"),
    ("<tool_use_error>InputValidationError</tool_use_error>", "tool_error"),
    ("", "failed"),
    ("   \n  ", "failed"),
], ids=["rejected", "exit-code", "tool-use-error", "empty", "whitespace"])
def test_lane_classifier_honours_the_harness_generic_markers(text, kind):
    assert classify_lane_error(text) == kind


# --------------------------------------------------------------------------
# Per-format, through parse_file -> to_claudit
# --------------------------------------------------------------------------


def _codex_blob(output_text: str | None) -> bytes:
    lines = [
        {"timestamp": UTC_ISO, "type": "session_meta",
         "payload": {"session_id":
                     "00000000-0000-4000-8000-0000000000e1"}},
        {"timestamp": UTC_ISO, "type": "turn_context",
         "payload": {"model": "gpt-5.6-sol"}},
        {"timestamp": UTC_ISO, "type": "response_item",
         "payload": {"type": "custom_tool_call", "call_id": "call_e1",
                     "name": "exec",
                     "input": 'const r = await tools.exec_command('
                              '{"cmd": "false"})'}},
    ]
    if output_text is not None:
        lines.append(
            {"timestamp": UTC_ISO, "type": "response_item",
             "payload": {"type": "custom_tool_call_output",
                         "call_id": "call_e1",
                         "output": [{"type": "input_text",
                                     "text": output_text}]}},
        )
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


@pytest.mark.parametrize("output,is_error,kind,error_prefix", [
    ("Script failed with exit code 2\nstderr tail", True, "tool_error",
     "Script failed with exit code 2"),
    ("Script completed\nWall time 1.0 seconds", False, None, None),
    (None, None, None, None),
], ids=["failed-script", "successful-call", "unsettled-call"])
def test_codex_result_classification(output, is_error, kind, error_prefix):
    tus = _tool_uses(_codex_blob(output))
    assert len(tus) == 1
    tu = tus[0]
    assert tu["is_error"] is is_error
    if kind is None:
        assert tu["error_kind"] is None
    else:
        assert tu["error_kind"] == kind
    if error_prefix is not None:
        assert tu["error_text"].startswith(error_prefix)
    elif is_error is False:
        assert tu["error_text"] is None


def _kimi_code_blob(result: dict | None) -> bytes:
    lines = [
        {"type": "metadata", "protocol_version": "1.4",
         "created_at": 1783000000000},
        {"type": "context.append_loop_event", "time": 1783000006000,
         "event": {"type": "tool.call", "uuid": "tc-e1", "turnId": "t1",
                   "step": 1, "toolCallId": "call-1", "name": "Bash",
                   "args": {"command": "false"}}},
    ]
    if result is not None:
        lines.append(
            {"type": "context.append_loop_event", "time": 1783000007000,
             "event": {"type": "tool.result", "toolCallId": "call-1",
                       "result": result}},
        )
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


@pytest.mark.parametrize("is_error,text,kind", [
    (True, "command not found", "tool_error"),
    (False, "done", None),
], ids=["errored-result", "successful-result"])
def test_kimi_code_result_classification(is_error, text, kind):
    tus = _tool_uses(_kimi_code_blob(
        {"isError": is_error, "output": [{"type": "text", "text": text}]}))
    assert len(tus) == 1
    assert tus[0]["is_error"] is is_error
    if kind is None:
        assert tus[0]["error_kind"] is None
    else:
        assert tus[0]["error_kind"] == kind
    if is_error:
        assert tus[0]["error_text"] == text


def test_kimi_code_textless_errored_result_stays_failed():
    tus = _tool_uses(_kimi_code_blob({"isError": True}))
    assert len(tus) == 1
    assert tus[0]["is_error"] is True
    assert tus[0]["error_kind"] == "failed"
    assert tus[0]["error_text"] is None


def _legacy_blob(return_value: object) -> bytes:
    lines = [
        {"timestamp": "2026-06-14T12:00:00Z",
         "message": {"type": "ToolCall",
                     "payload": {"type": "function", "id": "call_err",
                                 "function": {"name": "Bash"}}}},
        {"timestamp": "2026-06-14T12:00:01Z",
         "message": {"type": "ToolResult",
                     "payload": {"tool_call_id": "call_err",
                                 "return_value": return_value}}},
    ]
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


@pytest.mark.parametrize("is_error,text,kind", [
    (True, "nope", "tool_error"), (False, "ok", None),
], ids=["errored-result", "successful-result"])
def test_legacy_result_classification(is_error, text, kind):
    tus = _tool_uses(_legacy_blob({"is_error": is_error, "output": text}))
    assert len(tus) == 1
    assert tus[0]["is_error"] is is_error
    if kind is None:
        assert tus[0]["error_kind"] is None
    else:
        assert tus[0]["error_kind"] == kind
    if is_error:
        assert tus[0]["error_text"] == text


def test_error_text_is_truncated_to_the_kept_prefix():
    blob = _codex_blob("Script failed\n" + "y" * (ERROR_TEXT_MAX * 2))
    tus = _tool_uses(blob)
    assert tus[0]["is_error"] is True
    assert tus[0]["error_kind"] == "tool_error"
    assert len(tus[0]["error_text"]) == ERROR_TEXT_MAX
