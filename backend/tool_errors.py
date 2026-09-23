"""Tool-result text handling and failure classification.

Split out of parse.py to keep it under the lint line budget; every name
here is re-exported by backend.parse, which remains the module callers
import. Three jobs, all operating on a tool_result's CONTENT:

- sizing: how many characters a result put into the transcript
  (``_result_size``, images included);
- text: flattening to plain text and stripping PostgreSQL-hostile NULs
  (``_flatten_result_text``, ``_pg_text``);
- classification: which HARNESS-generic failure kind a failed result is
  (``_classify_error`` and the ERROR_KIND_* constants).
"""
from __future__ import annotations

import re

from orjson import dumps

# Maximum characters of a failed tool_result kept in tool_uses.error_text.
# Enough to identify the failure by GROUP BY; short enough that the column
# stays small on the ~5% of rows that carry it.
ERROR_TEXT_MAX = 200


def _pg_text(s: str) -> str:
    """Strip NUL bytes from text bound for a PostgreSQL text column.

    Postgres text cannot hold 0x00, and psycopg raises DataError on the
    whole executemany rather than the one row -- so a single failed tool
    call that read binary content aborts the entire ingest transaction
    and leaves every rollup unbuilt. Transcripts carry it as the JSON
    escape \\u0000, which json.loads decodes to a real NUL.

    Stripping rather than rejecting: the readable part of the message is
    what error_kind grouping is drilled down by, and it survives intact.
    """
    return s.replace("\x00", "") if "\x00" in s else s


# Coarse, HARNESS-GENERIC failure classes. Deliberately not a taxonomy of
# any one operator's hooks: a PreToolUse denial carries that hook's own
# wording, which differs per deploy, so it lands in "failed" and is
# separated by grouping on error_text instead. Only markers Claude Code
# itself emits are classified.
ERROR_KIND_REJECTED = "rejected"
ERROR_KIND_TOOL_ERROR = "tool_error"
_EXIT_CODE_RE = re.compile(r"\s*Exit code \d+")
ERROR_KIND_FAILED = "failed"


def _flatten_result_text(content) -> str:
    """Flatten a tool_result content field to plain text.

    The field is either a string or a list of blocks; only the text
    blocks carry a message worth keeping.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(str(blk.get("text", "") or ""))
            elif isinstance(blk, str):
                parts.append(blk)
        return " ".join(parts)
    return ""


def _result_size(content) -> int:
    """Characters one tool_result put into the transcript.

    Deliberately NOT `len(_flatten_result_text(...))`: that keeps text
    blocks only, and an image block's base64 payload is the single
    largest thing a tool result can carry. Over the live corpus, image
    results are 92% of all duplicated read bytes — measuring only text
    would report the cheapest half of the intake and call it the total.
    """
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for blk in content:
        if isinstance(blk, str):
            total += len(blk)
        elif isinstance(blk, dict):
            if blk.get("type") == "text":
                total += len(str(blk.get("text", "") or ""))
            else:
                source = blk.get("source")
                data = (source or {}).get("data") if isinstance(
                    source, dict) else None
                total += (len(str(data)) if data is not None
                          else len(dumps(blk)))
    return total


def _classify_error(text: str) -> str:
    """Classify a failed tool_result by markers the HARNESS emits.

    Anything that is neither a user/permission rejection nor a
    harness-wrapped tool error is "failed" -- including hook denials,
    whose wording belongs to the deploy, not to Claude Code. A Bash
    `Exit code N` is harness wording for a call that RAN: a tool error.
    """
    if "<tool_use_error>" in text or _EXIT_CODE_RE.match(text):
        return ERROR_KIND_TOOL_ERROR
    low = text.lower()
    if ("tool use was rejected" in low
            or "doesn't want to proceed" in low
            or "does not want to proceed" in low):
        return ERROR_KIND_REJECTED
    return ERROR_KIND_FAILED


def classify_lane_error(text: str) -> str:
    """The harness-generic kind for a LANE tool call that errored.

    The lane wires carry no status field: an errored result is one the
    tool RAN and that reported failure -- Codex's "Script failed" head
    is that harness's wording for a non-zero exit -- so the default is
    tool_error. The harness-generic rejection wording demotes it to
    rejected (the call never ran), and an errored result with no
    readable text stays failed: there is no evidence it ran. SV-WHY-
    COLUMNS keeps the kinds harness-generic; no lane-specific kind is
    added.
    """
    if not text or not text.strip():
        return ERROR_KIND_FAILED
    generic = _classify_error(text)
    if generic != ERROR_KIND_FAILED:
        return generic
    return ERROR_KIND_TOOL_ERROR
