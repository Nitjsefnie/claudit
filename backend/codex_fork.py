"""The head of a Codex rollout: model declarations, fork detection, replay marking.

Three facts come off the file's head: which model the file FIRST declares
(the model in force where a fork cut — the inherited settings — which
attributes the replayed prefix, issue #653), whether the file is a forked
rollout whose leading lines replay its parent's history (issue #687), and
the line where that replay ends (the fork's own first model declaration).
The replayed rows are marked `is_replay` so the canonical winner ranks a
replayed copy below an original of the same uuid, whatever the key order
(SV-CANONICAL-FLAG): a child spawned with its own model must not win its
parent's history from it.

Entry point is backend.parse_codex.parse, which opens with head_scan()
and closes with mark_replay(). The browser mirror is the codex section
of src/parser-codex.js (SV-PARSER-SPEC).
"""
from __future__ import annotations

from orjson import JSONDecodeError, loads

from backend.json_shape import as_dict
from backend.parse_common import _nonempty_str


def _line_at(blob: bytes, pos: int) -> tuple[bytes, int, int]:
    """The line spanning byte `pos`, as (raw bytes, start, end-exclusive)."""
    start = blob.rfind(b"\n", 0, pos) + 1
    end = blob.find(b"\n", pos)
    if end < 0:
        end = len(blob)
    return blob[start:end], start, end


def head_scan(blob: bytes) -> tuple[str | None, bool, int | None]:
    """The file's first declared model, whether it is a fork, and where
    the fork's own history begins.

    The scan runs at byte-offset speed: a candidate line is found by
    `find()` on the model-declaring needles — whichever needle occurs
    first in the file — and verified by decoding it, so the common file
    costs a handful of C searches and one decode instead of a per-line
    Python pass. The reparse bench gates this module's work per file
    (SV-CI-RATCHETS), which is why the scan pays the byte-offset shape
    rather than the plainer line iteration. A line that mentions a
    needle without declaring advances both finds past its own end.

    A forked rollout replays its parent's history before the new thread
    emits a turn_context, so the leading records have no model in front
    of them. The replay is the parent's history and the first declaration
    is the model the parent had in force at the fork point — the
    inherited settings — so it attributes the prefix (issue #653). None
    when the file declares no model at all: nothing derives the model in
    force, and the file is refused rather than stored as `unknown`.

    The fork flag comes off the first session_meta (first one wins, the
    same head rule the parse loop applies to the thread id), and the
    first declaration's line is the replay boundary — records on earlier
    lines are the parent's history journalled in this file (issue #687).
    """
    is_fork = False
    if b'"forked_from_id"' in blob:
        j = blob.find(b'"session_meta"')
        while j >= 0:
            raw, _s, end = _line_at(blob, j)
            j = blob.find(b'"session_meta"', end)
            try:
                obj = loads(raw)
            except JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "session_meta":
                continue  # a needle mention advances; the first META decides
            is_fork = bool(_nonempty_str(
                as_dict(obj.get("payload")).get("forked_from_id")))
            break

    first_model: str | None = None
    declared_at: int | None = None
    ti = blob.find(b'"turn_context"')
    si = blob.find(b'"thread_settings_applied"')
    while ti >= 0 or si >= 0:
        if ti >= 0 and (si < 0 or ti < si):
            raw, start, end = _line_at(blob, ti)
            ti = blob.find(b'"turn_context"', end)
        else:
            raw, start, end = _line_at(blob, si)
            si = blob.find(b'"thread_settings_applied"', end)
        try:
            obj = loads(raw)
        except JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        payload = as_dict(obj.get("payload"))
        if obj.get("type") == "turn_context":
            name = payload.get("model")
        elif payload.get("type") == "thread_settings_applied":
            name = as_dict(payload.get("thread_settings")).get("model")
        else:
            continue
        if name:
            # The boundary counts lines the way the parse loop's
            # splitlines() does, so a lone \r inside an earlier line
            # cannot push a replayed row past the boundary.
            first_model, declared_at = str(name), len(blob[:start].splitlines()) + 1
            break
    return first_model, is_fork, declared_at


def mark_replay(parsed: dict, boundary: int | None) -> None:
    """Mark the fork's replayed prefix on its records and tool rows.

    `boundary` is head_scan's declared_at for a FORK; a file that is not
    a fork passes None and is not even walked — its rows keep no key and
    to_claudit defaults them to NULL ranks-as-original semantics. On a
    fork, rows before the boundary are the parent's requests journalled
    in the fork file; is_replay flags them for the canonical rank
    (ingest_rollup_state's winner rule), and every row of a marked file
    carries the flag — True in the replayed prefix, False after it.
    """
    if boundary is None:
        return
    for row in parsed["records"] + parsed["tool_uses"]:
        row["is_replay"] = bool(row["line_num"] < boundary)
