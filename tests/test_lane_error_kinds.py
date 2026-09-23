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

from backend import parse
from backend.tool_errors import classify_lane_error

UTC_ISO = "2026-06-14T12:00:00.000Z"


def _tool_uses(blob: bytes) -> list[dict]:
    return parse.parse_file("sessions/p/s/wire.jsonl", blob)["tool_uses"]


# --------------------------------------------------------------------------
# The classifier itself
# --------------------------------------------------------------------------


def test_lane_classifier_defaults_a_ran_and_failed_call_to_tool_error():
    assert classify_lane_error("Script failed to start: exit 1") == (
        "tool_error"
    )
    assert classify_lane_error("collab spawn failed: no such agent") == (
        "tool_error"
    )


def test_lane_classifier_honours_the_harness_generic_markers():
    assert classify_lane_error(
        "The user doesn't want to proceed with this tool use"
    ) == "rejected"
    assert classify_lane_error("Exit code 1") == "tool_error"
    assert classify_lane_error("<tool_use_error>InputValidationError"
                               "</tool_use_error>") == "tool_error"


def test_lane_classifier_keeps_a_textless_failure_at_failed():
    assert classify_lane_error("") == "failed"
    assert classify_lane_error("   \n  ") == "failed"


# --------------------------------------------------------------------------
# Per-format, through parse_file -> to_claudit
# --------------------------------------------------------------------------


def _codex_blob(output_text: str | None) -> bytes:
    lines = [
        {"timestamp": UTC_ISO, "type": "session_meta",
         "payload": {"session_id":
                     "00000000-0000-4000-8000-0000000000e1"}},
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
    return b"".join(json.dumps(l).encode() + b"\n" for l in lines)


def test_codex_failed_script_classifies_tool_error():
    tus = _tool_uses(_codex_blob(
        "Script failed with exit code 2\nstderr tail"))
    assert len(tus) == 1
    tu = tus[0]
    assert tu["is_error"] is True
    assert tu["error_kind"] == "tool_error"
    assert tu["error_text"].startswith("Script failed with exit code 2")


def test_codex_successful_call_keeps_error_kind_null():
    tus = _tool_uses(_codex_blob("Script completed\nWall time 1.0 seconds"))
    assert len(tus) == 1
    tu = tus[0]
    assert tu["is_error"] is False
    assert tu["error_kind"] is None
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
    return b"".join(json.dumps(l).encode() + b"\n" for l in lines)


def test_kimi_code_errored_result_with_text_classifies_tool_error():
    tus = _tool_uses(_kimi_code_blob(
        {"isError": True,
         "output": [{"type": "text", "text": "command not found"}]}))
    assert len(tus) == 1
    assert tus[0]["is_error"] is True
    assert tus[0]["error_kind"] == "tool_error"
    assert tus[0]["error_text"] == "command not found"


def test_kimi_code_textless_errored_result_stays_failed():
    tus = _tool_uses(_kimi_code_blob({"isError": True}))
    assert len(tus) == 1
    assert tus[0]["is_error"] is True
    assert tus[0]["error_kind"] == "failed"
    assert tus[0]["error_text"] is None


def test_kimi_code_successful_result_keeps_error_kind_null():
    tus = _tool_uses(_kimi_code_blob(
        {"isError": False,
         "output": [{"type": "text", "text": "done"}]}))
    assert len(tus) == 1
    assert tus[0]["is_error"] is False
    assert tus[0]["error_kind"] is None


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
    return b"".join(json.dumps(l).encode() + b"\n" for l in lines)


def test_legacy_errored_result_classifies_from_its_output():
    tus = _tool_uses(_legacy_blob({"is_error": True, "output": "nope"}))
    assert len(tus) == 1
    assert tus[0]["is_error"] is True
    assert tus[0]["error_kind"] == "tool_error"
    assert tus[0]["error_text"] == "nope"


def test_legacy_successful_result_keeps_error_kind_null():
    tus = _tool_uses(_legacy_blob({"is_error": False, "output": "ok"}))
    assert len(tus) == 1
    assert tus[0]["is_error"] is False
    assert tus[0]["error_kind"] is None


def test_a_lane_row_without_a_result_stays_unsettled():
    """is_error NULL (no tool_result in the file at all): not a settled
    call, so no kind — the same contract as the Claude path."""
    tus = _tool_uses(_codex_blob(None))
    assert len(tus) == 1
    assert tus[0]["is_error"] is None
    assert tus[0]["error_kind"] is None


def test_error_text_is_truncated_to_the_kept_prefix():
    from backend.tool_errors import ERROR_TEXT_MAX

    blob = _codex_blob("Script failed\n" + "y" * (ERROR_TEXT_MAX * 2))
    tus = _tool_uses(blob)
    assert tus[0]["is_error"] is True
    assert tus[0]["error_kind"] == "tool_error"
    assert len(tus[0]["error_text"]) == ERROR_TEXT_MAX
