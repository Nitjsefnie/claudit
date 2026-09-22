"""Object key → (project, session, is_main): the one place that knows
where a transcript lives in the bucket.

Two layouts share this bucket:

Claude Code (claudit's own, unchanged — the exact derivation ingest
used to inline in _collect_todo and _persist):

  <project-slug>/<session>/<stem>.jsonl[.xz]
      project = segment 0, session = segment 1,
      is_main = stem == session

  Subagent sidecars are simply files whose stem differs from the
  session id (e.g. .../subagents/agent-x.jsonl.xz), so they fall out
  as is_main=False without a rule of their own.

Lane (codexmeter / kimimeter, ported from codexmeter's _scan_r2 and
_plan_work):

  sessions/<project>/<session>/wire.jsonl[.xz]
  sessions/<project>/<session>/subagents/<id>/wire.jsonl[.xz]
  sessions/<project>/project.json            (display-path marker)

The sessions/ subtree BELONGS to the lane layout: inside it only
wire.jsonl[.xz] and the depth-3 project.json marker are recognised, and
everything else — Kimi writes context.jsonl and state.json beside every
wire — classifies as None. Filing those under a project literally named
`sessions` is the mis-mapping this module exists to prevent, and no
real Claude project slug collides: slugs derive from absolute paths and
so always start with `-`. Outside sessions/ the Claude rule accepts any
.jsonl[.xz] key of three or more segments, codexmeter's depth floor for
its foreign (Claude-layout) transcripts.

classify() takes an OBJECT key with no bucket segment; a later task
qualifies stored keys with their bucket and strips it before calling
this.
"""
from __future__ import annotations

from typing import NamedTuple

_LANE_ROOT = "sessions"
_LANE_MARKER = "project.json"
# Basenames a lane transcript may carry; r2 inflates .xz transparently.
_LANE_WIRE = ("wire.jsonl", "wire.jsonl.xz")
_SUBAGENT_DIR = "subagents"
# Suffixes a Claude-layout transcript may carry, .xz first so a
# .jsonl.xz object is not mistaken for a bare .jsonl one.
_JSONL_SUFFIXES = (".jsonl.xz", ".jsonl")
# Claude-layout keys under this length hold no (project, session) pair.
_CLAUDE_MIN_SEGMENTS = 3
# A lane wire key is sessions/<project>/<session>/wire.jsonl at minimum.
_LANE_MIN_SEGMENTS = 4


class KeyInfo(NamedTuple):
    """The per-file identity the schema derives from an object key."""

    project_id: str
    session_id: str
    is_main: bool


def project_marker(key: str) -> str | None:
    """The project id when `key` is sessions/<project>/project.json.

    The marker carries the path the project's sessions were run from,
    which ingest uses as the project's display_name. It is not a
    transcript: classify() answers None for it.
    """
    parts = key.split("/")
    if (len(parts) == 3 and parts[0] == _LANE_ROOT
            and parts[2] == _LANE_MARKER):
        return parts[1]
    return None


def classify(key: str) -> KeyInfo | None:
    """Map an object key to its (project, session, is_main), or None
    when the key is not a transcript.

    The lane rule runs first: a key under sessions/ is lane territory
    whatever its basename, so a wire.jsonl at any depth maps to its
    project and session, and a non-wire file inside sessions/ is
    skipped rather than falling through to the Claude rule.
    """
    parts = key.split("/")
    if parts[0] == _LANE_ROOT:
        return _classify_lane(parts)
    return _classify_claude(parts)


def _classify_lane(parts: list[str]) -> KeyInfo | None:
    if len(parts) < _LANE_MIN_SEGMENTS or parts[-1] not in _LANE_WIRE:
        return None
    return KeyInfo(parts[1], parts[2], _SUBAGENT_DIR not in parts)


def _classify_claude(parts: list[str]) -> KeyInfo | None:
    if len(parts) < _CLAUDE_MIN_SEGMENTS:
        return None
    stem = _jsonl_stem(parts[-1])
    if stem is None:
        return None
    return KeyInfo(parts[0], parts[1], stem == parts[1])


def _jsonl_stem(basename: str) -> str | None:
    """The basename minus its .jsonl[.xz] suffix, or None when it has
    neither — ingest's _jsonl_suffix_len, turned inside out."""
    for suffix in _JSONL_SUFFIXES:
        if basename.endswith(suffix):
            return basename[:-len(suffix)]
    return None
