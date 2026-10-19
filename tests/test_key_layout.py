"""Object-key layouts map to project, session and main-file identity or None.
Claude keys name project/session/stem, with a main stem equal to the session
id. Lane keys put wire.jsonl under sessions/project/session, subagents in a
subagents directory, and the project display path in project.json.
Use bucket-less keys; storage qualification is stripped before classify()."""
import pytest

from backend import key_layout
from backend.key_layout import (
    KeyInfo, canonical_project_id, classify, lane_project_id,
    project_marker, project_slug, sidecar_stem,
)


@pytest.mark.parametrize("key, expected", [
    ("-root-claudit/abc/abc.jsonl.xz", KeyInfo("-root-claudit", "abc", True)),
    ("projA/sess-A/sess-A.jsonl", KeyInfo("projA", "sess-A", True)),
    ("-root-proj/test/test.jsonl.xz", KeyInfo("-root-proj", "test", True)),
], ids=["compressed", "plain", "claude-test-session"])
def test_claude_layout(key, expected):
    assert classify(key) == expected


@pytest.mark.parametrize("key, expected", [
    ("-root-claudit/abc/subagents/agent-x.jsonl.xz",
     KeyInfo("-root-claudit", "abc", False)),
    ("projA/sess-A/sess-A.sidecar.jsonl", KeyInfo("projA", "sess-A", False)),
], ids=["subagent", "other-stem"])
def test_claude_layout_other_file_in_a_session_is_not_main(key, expected):
    info = classify(key)
    assert info is not None and info.is_main is False
    assert info == expected


@pytest.mark.parametrize("key, expected", [
    ("sessions/8805b8ac99ad/01a0-uuid/wire.jsonl.xz",
     KeyInfo("8805b8ac99ad", "01a0-uuid", True)),
    ("sessions/aa5d/019f-parent/subagents/019f-child/wire.jsonl.xz",
     KeyInfo("aa5d", "019f-parent", False)),
    ("sessions/aa5d/019f/extra/wire.jsonl", KeyInfo("aa5d", "019f", True)),
], ids=["main", "subagent", "extra-depth"])
def test_lane_wire_is_a_transcript(key, expected):
    assert classify(key) == expected


def test_lane_subtree_owns_its_non_wire_files():
    """Inside sessions/, only wire.jsonl is a transcript. Kimi writes
    context.jsonl and state.json beside it; ingesting those would file
    junk rows under a project literally named `sessions`, which is the
    mis-mapping this module exists to prevent."""
    assert classify("sessions/aa5d/019f/context.jsonl") is None
    assert classify("sessions/aa5d/019f/state.json") is None
    assert classify("sessions/aa5d/019f/subagents/x/context.jsonl") is None


def test_keys_shorter_than_three_segments_are_not_transcripts():
    """The depth floor codexmeter and claudit share: flat bucket-root
    objects (user-history/*.jsonl) are not session transcripts."""
    assert classify("") is None
    assert classify("top.jsonl") is None
    assert classify("user-history/2026.jsonl") is None


def test_project_marker_is_only_at_lane_depth():
    assert project_marker("sessions/aa5d/sess/project.json") is None
    assert project_marker("other/aa5d/project.json") is None
    assert project_marker("sessions/aa5d/project.json.bak") is None
    assert project_marker("sessions/aa5d/project.json") == "aa5d"


@pytest.mark.parametrize("path, expected", [
    ("/root/claudit", "-root-claudit"),
    ("/tmp/claude-0/-root-x/scratchpad", "-tmp-claude-0--root-x-scratchpad"),
    ("/home/me/my.repo/", "-home-me-my-repo-"),
], ids=["plain", "scratchpad", "dots-and-trailing-slash"])
def test_project_slug_matches_claudes_derivation(path, expected):
    assert project_slug(path) == expected


@pytest.mark.parametrize("marker_path, expected", [
    ("/x/repo", "-x-repo"),
    (None, "8805b8ac99ad"),
    ("", "8805b8ac99ad"),
], ids=["marker", "missing-marker", "empty-marker"])
def test_lane_project_id(marker_path, expected):
    assert lane_project_id("8805b8ac99ad", marker_path) == expected


@pytest.mark.parametrize("project_id, expected", [
    ("C--Users-A-x", "c--users-a-x"),
    ("c--users-a-x", "c--users-a-x"),
    ("-root-Claudit", "-root-Claudit"),
    ("-root-claudit", "-root-claudit"),
    ("8805b8ac99ad", "8805b8ac99ad"),
], ids=["windows-mixed", "windows-folded", "posix-mixed", "posix-lower", "hash"])
def test_canonical_project_id(project_id, expected):
    assert canonical_project_id(project_id) == expected


def test_classify_folds_the_windows_project_slug():
    """The fold is part of the Claude-layout identity, so every key of
    the same Windows directory lands on one project id."""
    upper = classify("C--Users-Z-Repo/sess/sess.jsonl")
    lower = classify("c--users-z-repo/sess/sess.jsonl")
    assert upper is not None and lower is not None
    assert upper.project_id == lower.project_id == "c--users-z-repo"


def test_marker_windows_path_and_claude_key_meet_on_one_id():
    """A lane marker naming 'C:\\Users\\Z\\Repo' and a Claude-layout key
    of the same directory resolve to the SAME project id."""
    marker = lane_project_id("8805b8ac99ad", "C:\\Users\\Z\\Repo")
    info = classify("c--users-z-repo/sess/sess.jsonl")
    assert info is not None
    assert marker == info.project_id == "c--users-z-repo"


@pytest.mark.parametrize("key", [
    "sessions",
    "sessions/x",
    "sessions/x/y",
    "sessions/x/y/subagents",
    "sessions/x/y/subagents/z",
    "sessions/x/test/subagents/z/wire.jsonl",
    "sessions/x/test/subagents/z/meta.json",
    "sessions/8805b8ac99ad/test/subagents/ac92a5a25/wire.jsonl.xz",
    "sessions/8805b8ac99ad/test/wire.jsonl",
    "sessions/8805b8ac99ad/test/subagents/ac92a5a25/meta.json",
], ids=["root", "project", "session", "subagents", "subagent",
        "test-wire", "test-meta", "test-compressed-wire", "test-main-wire",
        "test-hash-meta"])
def test_a_junk_lane_key_never_index_errors(key):
    assert key_layout.classify(key) is None
    assert key_layout.transcript_stem(key) is None
    assert key_layout.sidecar_stem(key) is None
    assert sidecar_stem("sessions/x/y/subagents/z/meta.json") == (
        "sessions/x/y/subagents/z")


def test_a_lane_test_session_shares_no_segments_with_the_test_rule():
    """Only the session segment decides: `test` as a project hash prefix
    or a subagent id is a transcript still."""
    assert classify(
        "sessions/test/01a0-uuid/wire.jsonl.xz") == KeyInfo(
        "test", "01a0-uuid", True)
    assert classify(
        "sessions/8805b8ac99ad/01a0-uuid/subagents/test/wire.jsonl.xz"
    ) == KeyInfo("8805b8ac99ad", "01a0-uuid", False)
