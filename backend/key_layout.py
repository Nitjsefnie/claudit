"""Object key to (project, session, is_main) and transcript/sidecar pairing.

Claude: <project-slug>/<session>/<stem>.jsonl[.xz], at least three segments;
is_main iff stem == session. Windows drive-- slugs case-fold; POSIX slugs do not.
Lane: sessions/<project>/<session>/wire.jsonl[.xz], with subagent wires at
subagents/<id>/wire.jsonl[.xz] and project.json markers at depth three.

The sessions/ subtree belongs exclusively to lanes: non-wire files never fall
through to Claude classification. A lane session named test is scratch data,
including its subagents, and classifies as neither transcript nor sidecar. POSIX slugs start with -, drive slugs with a letter plus --;
neither equals sessions.

Claude agent-<id>.meta.json[.xz] pairs with the adjacent agent transcript;
lane subagents/<id>/meta.json[.xz] pairs with that directory's wire.
classify takes object keys only; r2.split_key strips the stored bucket prefix.
"""
from __future__ import annotations

import re
from typing import NamedTuple

LANE_ROOT = "sessions"
_LANE_MARKER = "project.json"
# Basenames a lane transcript may carry; r2 inflates .xz transparently.
_LANE_WIRE = ("wire.jsonl", "wire.jsonl.xz")
# A lane subagent's sidecar, beside its wire.jsonl in subagents/<id>/.
_LANE_AGENT_META = ("meta.json", "meta.json.xz")
# A Claude subagent's sidecar, beside it: agent-<id>.meta.json[.xz].
_AGENT_META_SUFFIXES = (".meta.json.xz", ".meta.json")
_SUBAGENT_DIR = "subagents"
# Suffixes a Claude-layout transcript may carry, .xz first so a
# .jsonl.xz object is not mistaken for a bare .jsonl one.
_JSONL_SUFFIXES = (".jsonl.xz", ".jsonl")
# Claude-layout keys under this length hold no (project, session) pair.
_CLAUDE_MIN_SEGMENTS = 3
# A lane wire key is sessions/<project>/<session>/wire.jsonl at minimum.
_LANE_MIN_SEGMENTS = 4
# A lane session named exactly this is a test-suite scratch session, not
# a real one: the kimi-code test suite archives its runs
# under a session named `test` inside a hash-named project, and those
# transcripts carry no usage -- classify() answers None for the whole
# subtree, so the ingest walk never lists them and the orphan sweep
# deletes rows already stored under such keys.
_LANE_TEST_SESSION = "test"


class KeyInfo(NamedTuple):
    project_id: str
    session_id: str
    is_main: bool


def project_marker(key: str) -> str | None:
    """The project id when `key` is sessions/<project>/project.json.

    The marker carries the path the project's sessions were run from,
    which ingest uses as the project's display_name and, keyed by its
    Claude slug, as the project id itself (lane_project_id). It is not a
    transcript: classify() answers None for it.
    """
    parts = key.split("/")
    if (len(parts) == 3 and parts[0] == LANE_ROOT
            and parts[2] == _LANE_MARKER):
        return parts[1]
    return None


def project_slug(path: str) -> str:
    """The Claude project slug of a directory path: every
    non-alphanumeric character replaced by '-' — the derivation Claude
    Code applies to the working directory when it names the project tree
    under ~/.claude/projects, and therefore the project segment of every
    Claude-layout object key (/root/claudit -> -root-claudit;
    /tmp/claude-0/-root-x/scratchpad -> -tmp-claude-0--root-x-scratchpad).

    The lane trees key their projects by a content hash instead; this is
    the bridge that makes one directory ONE project across buckets
    (lane_project_id)."""
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


# A slug of a WINDOWS path: a drive letter followed by '--', which is
# what 'C:\\' and 'C:/' slug to ('C:\\Users\\x' -> C--Users-x). A POSIX
# slug always starts with '-' — slugs derive from absolute paths.
_WINDOWS_SLUG = re.compile(r"^[A-Za-z]--")


def canonical_project_id(project_id: str) -> str:
    """The canonical project id: a Windows slug is case-folded to
    lowercase, everything else is returned unchanged.

    Windows paths are case-insensitive, and Claude Code takes the path's
    case from however the shell reported it, so one Windows directory
    arrives under two Claude-layout project ids (C--Users-x and
    c--users-x) that are ONE project. POSIX paths are case-sensitive and
    their slugs must NOT be folded. Lane content hashes match neither
    shape and pass through untouched.
    """
    if _WINDOWS_SLUG.match(project_id):
        return project_id.lower()
    return project_id


def lane_project_id(project_id: str, marker_path: str | None) -> str:
    """The id a lane project goes by: the CANONICAL id of the marker
    path — its Claude slug, case-folded when it names a Windows
    directory — so one directory is ONE project across buckets in a
    multi-bucket deploy. Without a marker path — legacy Kimi has none; a
    marker missing, malformed, or not read this run — the hash stays the
    id."""
    if not marker_path:
        return project_id
    return canonical_project_id(project_slug(marker_path))


def in_lane_tree(key: str) -> bool:
    return key.split("/", 1)[0] == LANE_ROOT


def classify(key: str) -> KeyInfo | None:
    """Map an object key to its (project, session, is_main), or None
    when the key is not a transcript.

    The lane rule runs first: a key under sessions/ is lane territory
    whatever its basename, so a wire.jsonl at any depth maps to its
    project and session, and a non-wire file inside sessions/ is
    skipped rather than falling through to the Claude rule.
    """
    parts = key.split("/")
    if parts[0] == LANE_ROOT:
        return _classify_lane(parts)
    return _classify_claude(parts)


def _classify_lane(parts: list[str]) -> KeyInfo | None:
    if len(parts) < _LANE_MIN_SEGMENTS or parts[-1] not in _LANE_WIRE:
        return None
    if parts[2] == _LANE_TEST_SESSION:
        return None
    return KeyInfo(parts[1], parts[2], _SUBAGENT_DIR not in parts)


def _classify_claude(parts: list[str]) -> KeyInfo | None:
    if len(parts) < _CLAUDE_MIN_SEGMENTS:
        return None
    stem = _jsonl_stem(parts[-1])
    if stem is None:
        return None
    # The project id is canonical: a Windows slug folds to lowercase so
    # one directory is ONE project however each session's shell cased
    # the path (canonical_project_id).
    return KeyInfo(canonical_project_id(parts[0]), parts[1], stem == parts[1])


def _jsonl_stem(basename: str) -> str | None:
    for suffix in _JSONL_SUFFIXES:
        if basename.endswith(suffix):
            return basename[:-len(suffix)]
    return None


def transcript_stem(key: str) -> str | None:
    """The key a transcript shares with its meta.json sidecar, or None
    when `key` is not a transcript.

    A Claude transcript's is its key minus the .jsonl[.xz] suffix
    (<dir>/agent-<id>); a lane wire's is its directory. sidecar_stem()
    answers the same string for the sidecar that describes it.
    """
    parts = key.split("/")
    if classify(key) is None:
        return None
    if parts[0] == LANE_ROOT:
        return "/".join(parts[:-1])
    stem = _jsonl_stem(parts[-1])
    return "/".join(parts[:-1] + [stem]) if stem is not None else None


def sidecar_stem(key: str) -> str | None:
    """The transcript_stem() of the transcript a subagent's meta.json
    sidecar describes, or None when `key` is not such a sidecar.

    Lane: sessions/<project>/<session>/subagents/<id>/meta.json[.xz],
    beside that subagent's wire. Claude: <dir>/agent-<id>.meta.json[.xz],
    beside agent-<id>.jsonl[.xz] — whether <dir> is subagents/ or a
    workflow's directory under it. Neither is a transcript: classify()
    answers None for both.
    """
    parts = key.split("/")
    if parts[0] == LANE_ROOT:
        if len(parts) > 2 and parts[2] == _LANE_TEST_SESSION:
            return None
        if (len(parts) > _LANE_MIN_SEGMENTS + 1
                and parts[-3] == _SUBAGENT_DIR
                and parts[-1] in _LANE_AGENT_META):
            return "/".join(parts[:-1])
        return None
    if len(parts) < _CLAUDE_MIN_SEGMENTS:
        return None
    for suffix in _AGENT_META_SUFFIXES:
        if parts[-1].endswith(suffix):
            return key[:-len(suffix)]
    return None
