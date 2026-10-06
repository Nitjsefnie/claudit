"""The head of a Codex rollout: model declarations, fork detection, replay marking.

Two facts come off the file's head, before the parse loop runs: which
model the file FIRST declares (the model in force where a fork cut — the
inherited settings — which attributes the replayed prefix, issue #653),
and whether the file is a forked rollout whose leading lines replay its
parent's history (issue #687). The replayed rows are marked `is_replay`
so the canonical winner ranks a replayed copy below an original of the
same uuid, whatever the key order (SV-CANONICAL-FLAG): a child spawned
with its own model must not win its parent's history from it.

Entry point is backend.parse_codex.parse, which opens with head_scan()
and closes with mark_replay(). The browser mirror is the codex section
of src/parser-codex.js (SV-PARSER-SPEC).
"""
from __future__ import annotations

from orjson import JSONDecodeError, loads

from backend.json_shape import as_dict
from backend.parse_common import _nonempty_str


def _declared_model(obj: dict) -> str | None:
    """The model one turn_context / thread_settings_applied line declares.

    The two shapes _codex_event_msg and parse() read for the model in
    force; nothing else declares one.
    """
    payload = as_dict(obj.get("payload"))
    if obj.get("type") == "turn_context":
        name = payload.get("model")
    elif payload.get("type") == "thread_settings_applied":
        name = as_dict(payload.get("thread_settings")).get("model")
    else:
        return None
    return str(name) if name else None


def head_scan(blob: bytes) -> tuple[str | None, bool, int | None]:
    """The file's first declared model, whether it is a fork, and where
    the fork's own history begins.

    A forked rollout replays its parent's history before the new thread
    emits a turn_context, so the leading records have no model in front
    of them. The replay is the parent's history and the first declaration
    is the model the parent had in force at the fork point — the
    inherited settings — so it attributes the prefix (issue #653). None
    when the file declares no model at all: nothing derives the model in
    force, and the file is refused rather than stored as `unknown`.

    The fork flag and the boundary come off the same scan: the first
    session_meta carrying `forked_from_id` makes the file a fork, and the
    first declaration's line is the replay boundary — records on earlier
    lines are the parent's history journalled in this file (issue #687).

    The scan JSON-decodes only lines that mention a model-declaring
    record — on a 20MB rollout that is a few hundred lines out of tens
    of thousands — plus one session_meta line.
    """
    first_model: str | None = None
    declared_at: int | None = None
    is_fork = False
    for line_num, raw in enumerate(blob.splitlines(), 1):
        if not is_fork and b'"session_meta"' in raw:
            try:
                obj = loads(raw)
            except JSONDecodeError:
                continue
            if (isinstance(obj, dict) and obj.get("type") == "session_meta"
                    and _nonempty_str(
                        as_dict(obj.get("payload")).get("forked_from_id"))):
                is_fork = True
        if (declared_at is not None
                or (b'"turn_context"' not in raw
                    and b'"thread_settings_applied"' not in raw)):
            continue
        try:
            obj = loads(raw)
        except JSONDecodeError:
            continue
        if isinstance(obj, dict):
            name = _declared_model(obj)
            if name:
                first_model, declared_at = name, line_num
    return first_model, is_fork, declared_at


def mark_replay(parsed: dict, boundary: int | None) -> None:
    """Mark the fork's replayed prefix on its records and tool rows.

    `boundary` is head_scan's declared_at for a FORK — the caller passes
    None for a file that is not a fork. Rows before the boundary are the
    parent's requests journalled in the fork file; is_replay flags them
    for the canonical rank (ingest_rollup_state's winner rule). Every
    Codex row carries the flag — True in the replayed prefix, False
    elsewhere; NULL (via to_claudit's default on the other lanes, via
    this same False on unflagged Codex rows ranking identically) is an
    original.
    """
    for row in parsed["records"] + parsed["tool_uses"]:
        row["is_replay"] = bool(
            boundary is not None and row["line_num"] < boundary)
