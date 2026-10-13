"""Kimi tool-call arguments, dispatch analytics and legacy result settling."""
from __future__ import annotations

import json
from datetime import datetime

from backend.bash_churn import bash_churn
from backend.json_shape import as_dict
from backend.parse_common import (_ParseState, _append_tool_use,
                                  _dispatch_prompt_shape, _line_count,
                                  _mark_assistant_event, _nonempty_str,
                                  _settle_lane_tool_result)
from backend.parse_collection import ALL_PARSE_COLLECTION, ParseCollection

_KIMI_DISPATCH_TOOLS = ("Agent",)


def _edit_churn(tool_name: str, args: dict) -> tuple[int, int]:
    """(lines_added, lines_deleted) for ONE file-mutating tool call.

    The wire's tool RESULT carries no diff — an Edit result is a prose
    string like "Replaced 1 occurrence in <path>" — so the counts are
    derived from the call's ARGS. Verified against the R2 corpus
    (2026-07, ~300 files sampled): the file-mutating tools come in
    exactly these shapes, two per wire format:

      kimi-code  Edit  {path, old_string, new_string}   (163/163 calls)
                 Write {path, content, mode?}
      legacy     StrReplaceFile {path, edit: {old, new} | [{old, new}, ...]}
                 WriteFile      {path, content, mode?}

    A Write's deleted count is unknowable — the args carry only the new
    content, not what was overwritten — so Writes contribute added lines
    only (append mode and fresh files are the common case anyway). Every
    other tool returns (0, 0).

    The counts describe the ATTEMPT: a call whose result is an error
    changed nothing, and _resolve_tool_errors zeroes its churn once the
    result's is_error is known.
    """
    if tool_name == "Edit":
        return (
            _line_count(args.get("new_string")),
            _line_count(args.get("old_string")),
        )
    if tool_name in ("Write", "WriteFile"):
        return (_line_count(args.get("content")), 0)
    if tool_name in ("Bash", "Shell"):
        return bash_churn(str(args.get("command") or ""))
    if tool_name == "StrReplaceFile":
        edit = args.get("edit")
        edits = edit if isinstance(edit, list) else [edit]
        added = deleted = 0
        for e in edits:
            if isinstance(e, dict):
                added += _line_count(e.get("new"))
                deleted += _line_count(e.get("old"))
        return (added, deleted)
    return (0, 0)


def _args_to_dict(args) -> dict:
    """Tool-call arguments as a dict, whatever the wire shape: kimi-code
    carries them already-parsed, legacy as a JSON string. Anything
    unreadable is {} — the churn of an unparsable edit is 0, not a
    dropped tool call."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _kimi_dispatch_args(name: str, args: dict, collect: ParseCollection = ALL_PARSE_COLLECTION) -> tuple | None:
    """(agent_type, agent_model, prompt_chars, brief_ref) an `Agent` call
    asked for, or None for any other tool.

    Same argument names as a Claude dispatch (`subagent_type`, `model`,
    `prompt`), and the prompt is plain text, so its shape is measured by
    the Claude path's own helper. A legacy Agent call names no
    subagent_type, so it carries a prompt shape and no type.
    """
    if name not in _KIMI_DISPATCH_TOOLS:
        return None
    chars, brief_ref = (
        _dispatch_prompt_shape(args)
        if collect.dispatch_analytics else (None, None))
    return (_nonempty_str(args.get("subagent_type")),
            (_nonempty_str(args.get("model")) if collect.dispatch_analytics else None), chars, brief_ref)


def _legacy_tool_call(st: _ParseState, line_num: int, ts: datetime | None,
                      payload: dict) -> None:
    func = as_dict(payload.get("function"))
    name = func.get("name", "")
    args = _args_to_dict(func.get("arguments"))
    _append_tool_use(st, line_num, ts, name, payload.get("id", ""),
                     _edit_churn(name, args), model=None,
                     dispatch=_kimi_dispatch_args(name, args, st.collect))
    _mark_assistant_event(st, ts)


def _legacy_tool_result(st: _ParseState, payload: dict) -> None:
    return_value = payload.get("return_value", {})
    tc_id = payload.get("tool_call_id", "")
    is_err = isinstance(return_value, dict) and bool(
        return_value.get("is_error", False)
    )
    if tc_id:
        text = (str(return_value.get("output") or "")
                if isinstance(return_value, dict) else "")
        _settle_lane_tool_result(st, tc_id, is_err, text if is_err else "")
