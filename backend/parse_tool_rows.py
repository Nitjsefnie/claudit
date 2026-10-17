"""Claude tool-call rows, optional analytics and retained failure/churn settling."""
from __future__ import annotations

from backend import bash_churn_errors, bash_reads
from backend.agent_types import canonical_agent_type
from backend.bash_churn import BashCommand, replace_churn
from backend.json_shape import as_dict
from backend.parse_common import _dispatch_prompt_shape
from backend.parse_collection import ALL_PARSE_COLLECTION, ParseCollection
from backend.tool_errors import (ERROR_KIND_TOOL_ERROR, ERROR_TEXT_MAX,
                                 _classify_error, _flatten_result_text,
                                 _pg_text, _result_size)

WRITE_TOOLS = ("Edit", "Write", "NotebookEdit")
READ_TOOLS = ("Read",)
DISPATCH_TOOLS = ("Agent", "Task")


def _line_count(s: str) -> int:
    """Lines in a payload string, git-style: "a\nb\n" and "a\nb" are both
    2 lines; "" is 0."""
    if not s:
        return 0
    return s.count("\n") + (0 if s.endswith("\n") else 1)


def _tool_churn(name: str, tool_input: dict) -> tuple[int, int]:
    """Attempted edit/write churn from call arguments, not result prose.

    Arguments carry old/new content reliably; structured result diffs are
    optional. Added and deleted counts are separate positive series.
    Edit/Write use this path; Bash uses BashCommand.churn scanners for
    literal heredocs, patches and replacements, returning zero whenever
    execution would be required to know the count.

    replace_churn excludes repeated context. replace_all counts one
    occurrence because the call does not say how many exist. A Write's
    prior contents are absent, so overwrites record zero deletions."""
    if name == "Edit":
        return replace_churn(
            str(tool_input.get("old_string", "") or ""),
            str(tool_input.get("new_string", "") or ""),
        )
    if name == "Write":
        return (
            _line_count(str(tool_input.get("content", "") or "")),
            0,
        )
    return 0, 0


def _tool_access(name: str, tool_input: dict) -> tuple[str | None, list[str], list[str]]:
    """(read_kind, read targets, write targets) for one tool CALL.

    Two shapes reach this. `Read` names its file outright. `Edit`,
    `Write` and `NotebookEdit` name a file they change. A Bash call
    never does: its caller hands the command to `bash_reads.scan_command`
    with the command text it already parsed, so every other name is a
    tool this module takes no read or write target from.

    A `Read` with an offset or limit is a SLICE, exactly as `sed -n`
    is: it put part of the file in context, not the file.
    """
    if name in WRITE_TOOLS:
        path = str(tool_input.get("file_path", "") or "")
        return None, [], [path] if path else []
    if name in READ_TOOLS:
        path = str(tool_input.get("file_path", "") or "")
        if not path:
            return None, [], []
        sliced = bool(tool_input.get("offset") or tool_input.get("limit"))
        return ("slice" if sliced else "whole"), [path], []
    return None, [], []


def _content_metrics(msg: dict, cwd: str = "", collect: ParseCollection = ALL_PARSE_COLLECTION) -> tuple[int, list]:
    """(text_chars, tool_use blocks) for one assistant message.

    Visible-response size: sum character lengths of `text` blocks
    in the assistant message. Thinking tokens roll into output_tokens
    undifferentiated, so token-based response-size metrics conflate
    "size" with "how much the model thought". Character count of text
    content blocks is the clean measure of visible response size.
    Also extract every `tool_use` block — the `name` field is what
    the per-tool panels group by. Stored later as one
    row per tool call in the `tool_uses` table for the per-tool
    ratio panel.
    """
    msg_content = msg.get("content")
    text_chars = 0
    msg_tool_uses: list[dict] = []
    if isinstance(msg_content, list):
        for idx, blk in enumerate(map(as_dict, msg_content)):
            btype = blk.get("type")
            if btype == "text":
                text_chars += len(str(blk.get("text", "")))
            elif btype in ("tool_use", "server_tool_use"):
                # server_tool_use = model invoked an Anthropic-hosted
                # tool (e.g. WebSearch). Same shape as tool_use; treat
                # both as tool calls in the panel.
                row = _tool_use_row(idx, blk, cwd, collect)
                if row is not None:
                    msg_tool_uses.append(row)
    elif isinstance(msg_content, str):
        text_chars = len(msg_content)
    return text_chars, msg_tool_uses


def _tool_use_row(idx: int, blk: dict, cwd: str, collect: ParseCollection = ALL_PARSE_COLLECTION) -> dict | None:
    """Build one pending tool row, or None for a nameless tool_use block.

    Keep per-call column derivation separate from the content-list walk
    so new columns do not inflate that walk's local-variable count."""
    name = str(blk.get("name", "") or "")
    if not name:
        return None
    args = as_dict(blk.get("input") or {})
    if name == "Bash":
        command = BashCommand(str(args.get("command", "") or ""))
        added, deleted = command.churn()
        r_kind, r_targets, w_targets = (bash_reads.scan_command(command, cwd)
                                        if collect.context_intake else (None, [], []))
    else:
        added, deleted = _tool_churn(name, args)
        r_kind, r_targets, w_targets = (_tool_access(name, args)
                                        if collect.context_intake else (None, [], []))
    dispatch = _dispatch_args(name, args, collect)
    return {
        "idx": idx,
        "tool_name": name,
        "tool_use_id": str(blk.get("id", "") or ""),
        "command": str(args.get("command", "") or "") if added else "",
        "lines_added": added,
        "lines_deleted": deleted,
        "agent_type": dispatch[0],
        "agent_model": dispatch[1],
        "dispatch_prompt_chars": dispatch[2],
        "dispatch_brief_ref": dispatch[3],
        "dispatch_name": _dispatch_name(name, args),
        "read_kind": r_kind,
        "read_targets": r_targets,
        "write_targets": w_targets,
    }


def _dispatch_args(name: str, args: dict, collect: ParseCollection = ALL_PARSE_COLLECTION) -> tuple:
    """(agent_type, agent_model, prompt_chars, brief_ref) for a
    subagent-dispatching CALL.

    Read off the call arguments, so a dispatch is attributable even
    when the subagent writes no JSONL of its own. `files.agent_type`
    answers "what ran"; this answers "what was ASKED for, on which
    model" -- the two differ exactly when a dispatch fails or the
    request omits the field. Non-dispatch tools get all-None.
    """
    if name not in DISPATCH_TOOLS or not isinstance(args, dict):
        return None, None, None, None
    a_type = args.get("subagent_type")
    a_model = args.get("model") if collect.dispatch_analytics else None
    chars, brief_ref = (
        _dispatch_prompt_shape(args)
        if collect.dispatch_analytics else (None, None))
    return (canonical_agent_type(a_type) if isinstance(a_type, str) and a_type else None,
            str(a_model) if isinstance(a_model, str) and a_model else None,
            chars, brief_ref)


def _dispatch_name(name: str, args: dict) -> str | None:
    """The `name` a dispatching call gave its agent: a teammate's link to its role."""
    value = args.get("name") if name in DISPATCH_TOOLS and isinstance(args, dict) else None
    return value if isinstance(value, str) and value else None


def _resolve_tool_errors(tool_uses: list, tool_result_is_error: dict,
                         tool_result_text: dict | None = None,
                         tool_result_chars: dict | None = None, *,
                         error_text: bool = True) -> None:
    """Resolve tool_result.is_error onto each tool_uses entry by
    tool_use_id. Unmatched entries keep is_error=None and are
    excluded from rate denominators at query time. An errored call
    changed nothing on disk, so its churn is zeroed (an unmatched
    call keeps its churn: the file may simply end before the result
    record) — unless `bash_churn_errors.churn_survives_error` says otherwise."""
    for tu in tool_uses:
        # Kept on the row: ingest dedups compaction-sidecar replays on it.
        tu["tool_use_id"] = tu_id = tu.get("tool_use_id") or None
        command = tu.pop("command", "")
        if tu_id:
            tu["result_chars"] = (tool_result_chars or {}).get(tu_id)
        if tu_id and tu_id in tool_result_is_error:
            tu["is_error"] = tool_result_is_error[tu_id]
            if tu["is_error"]:
                text = (tool_result_text or {}).get(tu_id, "")
                tu["error_kind"] = _classify_error(text)
                tu["error_text"] = (text[:ERROR_TEXT_MAX] or None) if error_text else None
                ran = tu["error_kind"] == ERROR_KIND_TOOL_ERROR
                if not (ran and bash_churn_errors.churn_survives_error(command, text)):
                    tu["lines_added"], tu["lines_deleted"] = 0, 0
                if not ran:
                    tu["write_targets"] = []  # wrote nothing


def note_tool_result(blk: dict, outcomes: dict, texts: dict, sizes: dict,
                     context_intake: bool) -> None:
    """Keep full failures for survival decisions, size results only when collected."""
    tu_id = blk.get("tool_use_id")
    if not tu_id:
        return
    is_err = bool(blk.get("is_error", False))
    outcomes[str(tu_id)] = is_err
    if context_intake:
        sizes[str(tu_id)] = _result_size(blk.get("content"))
    if is_err:
        texts[str(tu_id)] = _pg_text(_flatten_result_text(blk.get("content")).strip())
