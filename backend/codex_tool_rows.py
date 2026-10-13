"""Codex tool rows, dispatch arguments and patch-result attribution."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING

from backend.parse_common import _append_tool_use, _nonempty_str, _settle_lane_tool_result
from backend.parse_codex_churn import _codex_program_churn
from backend.json_shape import as_dict
from backend.tool_errors import ERROR_KIND_FAILED
from backend.parse_collection import ALL_PARSE_COLLECTION, ParseCollection

if TYPE_CHECKING:
    from backend.parse_codex import _CodexState

# The single custom tool is `exec`, whose input is a JS program calling
# tools.<api>({...}). The api is the useful tool name — `exec` alone would
# collapse every shell command, patch and plan update into one bucket.
_CODEX_API_RE = re.compile(r"tools\.([A-Za-z_][A-Za-z_0-9]*)\s*\(")

# A record keeps the model id the transcript names (issue #471): the parser
# relabels nothing and falls back to nothing. An id pricing.json has no row
# for prices through the fallback and surfaces as estimated_rate
# (SV-RATE-ESTIMATES) — a visible estimate beats an invented attribution.
# That failure mode is why: the old substring map RELABELLED any unmapped id
# (gpt-6.1-sol read as gpt-5.6-sol at twice its price; Astra shipped as Sol
# for weeks until it got a row), hiding every new model from the dashboard
# until a code change listed it.
#
# The only rewrites are spelling: case-folding and the corpus's missing
# separator (gpt5.6-sol -> gpt-5.6-sol, a real turn_context spelling), which
# the stored id should spell the way the corpus canonically does. The digit
# class is spelled [0-9], never \d: Python's \d matches Unicode Nd digits
# where the JS mirror's \d matches ASCII only, so \d would make the two
# runtimes disagree on a non-ASCII spelling (SV-PARSER-SPEC).
_CODEX_SEPARATED = re.compile(r"^gpt(?=[0-9.])")

# A tool result whose first line starts with one of these is a failure. The
# payload carries no status field of its own.
_CODEX_FAILURE_HEADS = ("Script failed", "collab spawn failed")

# function_call names that dispatch a subagent. Both spellings are in the
# corpus: the prefixed one in older rollouts.
_CODEX_DISPATCH_TOOLS = ("spawn_agent", "multi_agent_v1__spawn_agent")


def _codex_model(raw: str | None) -> str | None:
    """Model string in force -> the stored model id.

    Spelling normalisation only (issue #471); never a relabelling. None
    when the string in force names nothing: the file is then refused
    (issue #653) rather than stored under an `unknown` placeholder.
    """
    lowered = (raw or "").strip().lower()
    if not lowered:
        return None
    return _CODEX_SEPARATED.sub("gpt-", lowered)


def _codex_output_text(payload: dict) -> str:
    """Flatten a *_call_output payload into one string."""
    out = payload.get("output")
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        parts = []
        for chunk in out:
            if isinstance(chunk, dict) and chunk.get("text"):
                parts.append(str(chunk["text"]))
            elif isinstance(chunk, str):
                parts.append(chunk)
        return "\n".join(parts)
    return ""


def _diff_churn(diff: object) -> tuple[int, int]:
    """(lines_added, lines_deleted) for one unified diff.

    Unlike the Kimi formats, whose tool results carry no diff and whose churn
    has to be inferred from the CALL's arguments, Codex journals the applied
    diff — so this counts what was actually written, not what was requested.
    """
    added = deleted = 0
    for line in str(diff or "").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            deleted += 1
    return added, deleted


def _change_churn(change: dict) -> tuple[int, int]:
    """(lines_added, lines_deleted) for one entry of a patch's `changes`.

    Only an `update` carries a unified_diff. An `add` and a `delete` carry
    the whole file body under `content` instead, and reading unified_diff
    alone books them at zero — 1,355 of the 3,084 changes on the reference
    corpus (2026-08-20..25) are one of those two, so it is not an edge case.
    A pure rename is an `update` whose unified_diff is absent: no line moved.
    """
    if change.get("unified_diff") is not None:
        return _diff_churn(change.get("unified_diff"))
    lines = len(str(change.get("content") or "").splitlines())
    if change.get("type") == "add":
        return lines, 0
    if change.get("type") == "delete":
        return 0, lines
    return 0, 0


def _codex_dispatch_args(payload: dict, collect: ParseCollection = ALL_PARSE_COLLECTION) -> tuple | None:
    """(agent_type, agent_model, prompt_chars, brief_ref) a spawn_agent
    function_call asked for, or None for any other call.

    `arguments` is a JSON string. Its `message` -- the brief -- is
    ENCRYPTED, so neither its length nor a brief reference inside it
    means anything: both prompt-shape fields stay None on this lane.
    Arguments that do not parse to an object attribute nothing.
    """
    if payload.get("type") != "function_call" or (
            payload.get("name") not in _CODEX_DISPATCH_TOOLS):
        return None
    args = payload.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args) if args else None
        except json.JSONDecodeError:
            args = None
    if not isinstance(args, dict):
        return None, None, None, None
    return (_nonempty_str(args.get("agent_type")),
            (_nonempty_str(args.get("model")) if collect.dispatch_analytics else None), None, None)


def _codex_tool_call(st: _CodexState, line_num: int, ts: datetime | None,
                     payload: dict) -> None:
    """Record one tool call.

    A custom_tool_call is always `exec`, whose real identity is the first
    tools.<api> its JS program calls — exec_command, apply_patch, write_stdin,
    update_plan. A function_call names its function directly (spawn_agent,
    wait_agent, send_message, ...).
    """
    churn = (0, 0)
    if payload.get("type") == "custom_tool_call":
        program = str(payload.get("input") or "")
        apis = _CODEX_API_RE.findall(program)
        name = apis[0] if apis else str(payload.get("name") or "")
        churn = _codex_program_churn(program)
    else:
        apis = [str(payload.get("name") or "")]
        name = apis[0]
    _append_tool_use(
        st, line_num, ts, name, str(payload.get("call_id") or ""), churn,
        model=_codex_model(st.model),
        dispatch=_codex_dispatch_args(payload, st.collect),
    )
    if "apply_patch" in apis:
        metadata = as_dict(payload.get("internal_chat_message_metadata_passthrough"))
        turn_id = metadata.get("turn_id")
        st.patch_target = (len(st.tool_uses) - 1, turn_id)


def _codex_tool_result(st: _CodexState, payload: dict) -> None:
    """Settle one tool call's is_error.

    The payload carries no status field; failure is announced in the first
    line of the output text. A backgrounded script ("Script running with cell
    ID ...") has not failed — it has not finished.
    """
    call_id = payload.get("call_id")
    if not call_id:
        return
    text = _codex_output_text(payload)
    head = text.split("\n", 1)[0] if text else ""
    is_err = head.startswith(_CODEX_FAILURE_HEADS)
    _settle_lane_tool_result(st, call_id, is_err, text if is_err else "")


def _codex_patch(st: _CodexState, line_num: int, ts: datetime | None,
                 ok: bool, changes: object, turn_id: object) -> None:
    """Attribute one applied patch's line churn.

    Called for both spellings of the same event — the `patch_apply_end`
    event_msg, and the `FileChange` item of the `item_completed` event_msg
    that replaced it (see _codex_item_completed). The caller resolves the
    success flag and the changes map; everything below is common to both.

    A patch does NOT share a call_id namespace with the tool calls (0 of
    1,727 patch call_ids matched one in the old spelling, 0 of 2,623
    FileChange item ids in the new — both carry "exec-<uuid>"), so the churn
    is carried back onto the most recent apply_patch call of the SAME turn.
    One exec can apply several patches; they sum onto that call.

    Where no such call exists in this file the patch came from a subagent,
    whose tool calls the parent rollout does not record (522 of the 524
    unattributable patches in the local corpus sit in files with
    sub_agent_activity). Those get a row of their own rather than being
    dropped: the work happened, and this file is the only record of it.

    A failed patch changed nothing, so it contributes no churn — the same
    rule _resolve_tool_errors applies to a rejected edit.
    """
    added = deleted = 0
    if ok and isinstance(changes, dict):
        for change in changes.values():
            if isinstance(change, dict):
                a, d = _change_churn(change)
                added += a
                deleted += d
    target = st.patch_target
    if target is not None and target[1] == turn_id:
        tool_use = st.tool_uses[target[0]]
        tool_use["lines_added"] += added
        tool_use["lines_deleted"] += deleted
        return
    # No tool_call_id: _resolve_tool_errors must leave this row's is_error,
    # which is settled here and nowhere else, alone. A failed patch
    # carried no output text to classify, so its kind is `failed` — the
    # patch attempt is the thing that never produced a change.
    _append_tool_use(
        st, line_num, ts, "apply_patch", "", (added, deleted),
        model=_codex_model(st.model),
    )
    st.tool_uses[-1]["is_error"] = not ok
    st.tool_uses[-1]["error_kind"] = None if ok else ERROR_KIND_FAILED
