"""Object-key layout rules: key → (project, session, is_main), or None.

claude audit ingests two bucket layouts through one classify():

- Claude Code's own, which ingest used to derive inline (project =
  segment 0, session = segment 1, main when the stem equals the
  session id) — unchanged, byte for byte.
- the lane layout shared by codexmeter and kimimeter, whose
  transcripts are wire.jsonl files under sessions/<project>/<session>/,
  with subagent wires under a further subagents/ directory and a
  project.json marker carrying the project's display path.

Tests use bucket-less keys throughout: a later task qualifies stored
keys with their bucket and strips it before calling classify().
"""
from backend.key_layout import (
    KeyInfo, classify, lane_project_id, project_marker, project_slug,
)


def test_claude_layout():
    assert classify("-root-claudit/abc/abc.jsonl.xz") == KeyInfo("-root-claudit", "abc", True)


def test_claude_subagent_sidecar_is_not_main():
    info = classify("-root-claudit/abc/subagents/agent-x.jsonl.xz")
    assert info is not None and info.is_main is False


def test_lane_main_session():
    assert classify("sessions/8805b8ac99ad/01a0-uuid/wire.jsonl.xz") == KeyInfo(
        "8805b8ac99ad", "01a0-uuid", True)


def test_lane_subagent():
    assert classify("sessions/aa5d/019f-parent/subagents/019f-child/wire.jsonl.xz") == KeyInfo(
        "aa5d", "019f-parent", False)


def test_non_transcripts_are_skipped():
    assert classify("user-history/2026.jsonl") is None
    assert classify("sessions/aa5d/project.json") is None


def test_project_marker():
    assert project_marker("sessions/aa5d/project.json") == "aa5d"
    assert project_marker("sessions/aa5d/x/wire.jsonl") is None


def test_claude_layout_plain_jsonl_matches_ingest_today():
    """The uncompressed shape of the r2_mini fixtures: stem == session
    is what makes a Claude file main, whatever the suffix."""
    assert classify("projA/sess-A/sess-A.jsonl") == KeyInfo("projA", "sess-A", True)


def test_claude_layout_other_file_in_a_session_is_not_main():
    """Any Claude-layout file whose stem differs from the session id is
    not main — ingest derived exactly this from the key before."""
    assert classify("projA/sess-A/sess-A.sidecar.jsonl") == KeyInfo(
        "projA", "sess-A", False)


def test_lane_wire_at_any_depth_is_a_transcript():
    """codexmeter's scan only fixes the first three segments and the
    basename; depth between them is not load-bearing."""
    assert classify("sessions/aa5d/019f/extra/wire.jsonl") == KeyInfo(
        "aa5d", "019f", True)


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


def test_project_slug_matches_claudes_derivation():
    """The two load-bearing examples: one segment per path character
    run, every non-alphanumeric byte a '-'."""
    assert project_slug("/root/claudit") == "-root-claudit"
    assert project_slug(
        "/tmp/claude-0/-root-x/scratchpad"
    ) == "-tmp-claude-0--root-x-scratchpad"


def test_project_slug_dots_and_trailing_slash():
    assert project_slug("/home/me/my.repo/") == "-home-me-my-repo-"


def test_lane_project_id_is_the_marker_path_slug():
    assert lane_project_id("8805b8ac99ad", "/x/repo") == "-x-repo"


def test_lane_project_id_keeps_the_hash_without_a_marker_path():
    """No marker (legacy Kimi has none; missing, malformed, or not read
    this run) — the hash stays the id."""
    assert lane_project_id("8805b8ac99ad", None) == "8805b8ac99ad"
    assert lane_project_id("8805b8ac99ad", "") == "8805b8ac99ad"
