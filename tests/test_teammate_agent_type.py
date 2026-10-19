"""A named teammate's agent type is its role, never its teammate name.

Claude Code writes a teammate's meta.json sidecar with ``agentType`` set
to the NAME the lead gave it (``{"agentType": "migrate-rest", "name":
"migrate-rest", "taskKind": "in_process_teammate", ...}``), and the
teammate transcript carries no ``attributionAgent``. The role lives only
in the lead's dispatching ``Agent`` call (``subagent_type`` beside
``name``), so ingest joins the two within the session; with no joinable
dispatch the teammate is DEFAULT_AGENT_TYPE.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
# The ingest suite's fresh-DB fixture, registered here under its own
# name ("fresh_db") by having the function object in this module.
from test_ingest import _fresh_db_fixture

from backend import agent_sidecar, constants, db, ingest, parse

__all__ = ["_fresh_db_fixture"]

_PARSER_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "parser"
DEFAULT = constants.DEFAULT_AGENT_TYPE

_TEAMMATE_SIDECAR = json.dumps({
    "agentType": "migrate-rest", "description": "Migrate the rest",
    "name": "migrate-rest", "taskKind": "in_process_teammate",
    "teamName": "session-sess-t", "model": "opus"}).encode()

# An older release's teammate sidecar: ONLY the agentType, which is the
# NAME the lead gave it -- no ``name``, no ``taskKind``, no ``toolUseId``.
_CANDIDATE_SIDECAR = b'{"agentType":"worker-1"}'

_MEMBER_KEY = "-root-x/sess-t/subagents/agent-amigrate-rest-0123456789abcdef.jsonl"
_LEAD_KEY = "-root-x/sess-t/sess-t.jsonl"


def _fixture(name: str) -> bytes:
    return (_PARSER_FIX / name).read_bytes()


def _member() -> dict:
    return parse.parse_file(_MEMBER_KEY, _fixture("teammate_member.jsonl"))


# ---- parse: a teammate sidecar names a teammate, not a role ----------------

def test_a_teammate_sidecar_names_no_role():
    out = agent_sidecar.apply_agent_sidecar(_member(), _TEAMMATE_SIDECAR, _MEMBER_KEY)
    assert out["agent_type"] == DEFAULT
    assert out["teammate_name"] == "migrate-rest"
    assert agent_sidecar.sidecar_agent_role(_TEAMMATE_SIDECAR) is None


@pytest.mark.parametrize("sidecar,expected_role,expected_name", [
    (b'{"agentType":"scout-2","name":"scout-2","description":"d"}',
     DEFAULT, "scout-2"),
    (b'{"agentType":"implementer","name":"migrate-rest",'
     b'"taskKind":"in_process_teammate"}', "implementer", "migrate-rest"),
    (_CANDIDATE_SIDECAR, "worker-1", "worker-1"),
    (b'{"agentType":"code-reviewer","description":"d","spawnDepth":1}',
     "code-reviewer", "code-reviewer"),
], ids=["name-equals-role", "marked-with-fallback", "agent-type-only",
        "candidate-with-extra-keys"])
def test_teammate_sidecars_keep_their_role_and_name(
        sidecar, expected_role, expected_name):
    out = agent_sidecar.apply_agent_sidecar(_member(), sidecar, _MEMBER_KEY)
    assert out["agent_type"] == expected_role
    assert out["teammate_name"] == expected_name


@pytest.mark.parametrize("sidecar,expected_role", [
    (b'{"agentType":"implementer","name":"implementer",'
     b'"toolUseId":"toolu_p1"}', "implementer"),
    (b'{"agentType":"implementer","name":"impl-task1",'
     b'"description":"d","toolUseId":"toolu_p1"}', "implementer"),
    (b'{"agentType":"lsp-probe","toolUseId":"toolu_p1"}', "lsp-probe"),
    (b'{"isFork": true, "agentType":"explorer"}', "Explore"),
    (b'{"taskKind":"fork","agentType":"scout"}', "scout"),
], ids=["tool-id-name-equals-role", "tool-id-distinct-name", "tool-id-only",
        "fork-flag", "fork-task-kind"])
def test_definite_subagent_sidecars_keep_a_role_without_a_teammate_name(
        sidecar, expected_role):
    out = agent_sidecar.apply_agent_sidecar(_member(), sidecar, _MEMBER_KEY)
    assert out["agent_type"] == expected_role
    assert out.get("teammate_name") is None


def test_a_teammate_with_an_in_band_role_keeps_it():
    parsed = parse.parse_file(_MEMBER_KEY, _fixture("agent_attribution.jsonl"))
    out = agent_sidecar.apply_agent_sidecar(parsed, _TEAMMATE_SIDECAR, _MEMBER_KEY)
    assert out["agent_type"] == "implementer"
    assert out.get("teammate_name") is None


def test_a_dispatch_records_the_name_it_gave_its_agent():
    lead = parse.parse_file(_LEAD_KEY, _fixture("teammate_lead.jsonl"))
    (call,) = lead["tool_uses"]
    assert call["agent_type"] == "implementer"
    assert call["dispatch_name"] == "migrate-rest"
    plain = parse.parse_file("k/sess-d/sess-d.jsonl",
                             _fixture("agent_dispatch.jsonl"))
    assert [tu["dispatch_name"] for tu in plain["tool_uses"]] == [None] * 3


def test_a_non_dispatch_call_with_a_name_argument_records_no_dispatch_name():
    line = {"type": "assistant", "timestamp": "2026-09-01T12:00:01Z",
            "uuid": "a1", "requestId": "req-1",
            "message": {"role": "assistant", "model": "claude-opus-5",
                        "content": [{"type": "tool_use", "id": "toolu_n1",
                                     "name": "SendMessage",
                                     "input": {"name": "migrate-rest",
                                               "message": "hi"}}],
                        "usage": {"input_tokens": 1, "output_tokens": 1}}}
    out = parse.parse_file(_LEAD_KEY, json.dumps(line).encode() + b"\n")
    assert [tu["dispatch_name"] for tu in out["tool_uses"]] == [None]


# ---- ingest: the role is joined from the lead's dispatch -------------------

@pytest.fixture(name="mirror")
def _mirror_fixture(monkeypatch, tmp_path):
    bucket = tmp_path / "r2" / "claude"
    bucket.mkdir(parents=True)
    monkeypatch.setenv("CLAUDIT_R2_ENDPOINT", f"file://{tmp_path}/r2/")
    monkeypatch.delenv("CLAUDIT_R2_BUCKET", raising=False)
    return bucket


def _put(bucket: Path, key: str, data: bytes) -> None:
    path = bucket / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _put_member(bucket: Path) -> None:
    _put(bucket, _MEMBER_KEY, _fixture("teammate_member.jsonl"))
    _put(bucket, _MEMBER_KEY.replace(".jsonl", ".meta.json"),
         _TEAMMATE_SIDECAR)


def _put_candidate_member(bucket: Path) -> None:
    _put(bucket, _MEMBER_KEY, _fixture("teammate_member.jsonl"))
    _put(bucket, _MEMBER_KEY.replace(".jsonl", ".meta.json"),
         _CANDIDATE_SIDECAR)


def _member_type() -> str:
    with db.viz_conn() as c:
        row = c.execute("SELECT agent_type FROM files WHERE file_key = %s",
                        (f"claude/{_MEMBER_KEY}",)).fetchone()
    assert row is not None
    return row[0]


def _rollup_types() -> set[str]:
    with db.viz_conn() as c:
        return {r[0] for r in c.execute(
            "SELECT DISTINCT dat.value FROM agent_rollup ar "
            "JOIN dim_agent_type dat ON dat.id = ar.agent_type_id"
        ).fetchall()}


@pytest.mark.parametrize("dispatch_name,expected_role", [
    (b'"migrate-rest"', "implementer"),
    (b'"someone-else"', DEFAULT),
], ids=["matching-dispatch", "unmatched-dispatch"])
def test_ingest_resolves_a_teammates_role_from_a_matching_dispatch(
        fresh_db, mirror, dispatch_name, expected_role):
    _put(mirror, _LEAD_KEY,
         _fixture("teammate_lead.jsonl").replace(b'"migrate-rest"', dispatch_name))
    _put_member(mirror)
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert _member_type() == expected_role
    assert "migrate-rest" not in _rollup_types()
    if expected_role == "implementer":
        assert "implementer" in _rollup_types()


@pytest.mark.parametrize("sidecar,initial_role", [
    (b'{"agentType":"code-reviewer","name":"migrate-rest",'
     b'"taskKind":"in_process_teammate"}', "code-reviewer"),
    (_TEAMMATE_SIDECAR, DEFAULT),
], ids=["sidecar-role-fallback", "default-role-fallback"])
def test_a_late_lead_resolves_the_teammates_standing_role(
        fresh_db, mirror, sidecar, initial_role):
    _put(mirror, _MEMBER_KEY, _fixture("teammate_member.jsonl"))
    _put(mirror, _MEMBER_KEY.replace(".jsonl", ".meta.json"), sidecar)
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert _member_type() == initial_role

    _put(mirror, _LEAD_KEY, _fixture("teammate_lead.jsonl"))
    result = ingest.run_ingest(trigger="manual")
    assert result["inserted"] == 1 and result["reparsed"] == 0
    assert _member_type() == "implementer", "a joined dispatch wins"


def _agent_type_only_line(ts: str, call_id: str) -> dict:
    """The issue's lead: an Agent dispatch that named its teammate but no
    subagent_type, so the dispatch call's own agent_type is NULL."""
    return {"type": "assistant", "timestamp": ts, "uuid": f"u-{call_id}",
            "requestId": f"req-{call_id}", "sessionId": "sess-t",
            "message": {"role": "assistant", "model": "claude-opus-5",
                        "content": [{"type": "tool_use", "id": call_id,
                                     "name": "Agent",
                                     "input": {"description": "Do the work",
                                               "name": "worker-1",
                                               "run_in_background": True,
                                               "prompt": "go"}}],
                        "usage": {"input_tokens": 1, "output_tokens": 1}}}


def test_an_agent_type_only_teammate_resolves_from_its_leads_dispatch(
        fresh_db, mirror):
    _put(mirror, _LEAD_KEY, json.dumps(_agent_type_only_line(
        "2026-09-01T12:00:00Z", "toolu_w1")).encode() + b"\n")
    _put_candidate_member(mirror)
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert _member_type() == DEFAULT
    with db.viz_conn() as c:
        row = c.execute("SELECT teammate_name FROM files WHERE file_key = %s",
                        (f"claude/{_MEMBER_KEY}",)).fetchone()
    assert row == ("worker-1",)


@pytest.mark.parametrize("sidecar,lead,expected_role", [
    (_TEAMMATE_SIDECAR, _fixture("teammate_lead.jsonl"), DEFAULT),
    (_CANDIDATE_SIDECAR, json.dumps(_agent_type_only_line(
        "2026-09-01T12:00:00Z", "toolu_w1")).encode() + b"\n", "worker-1"),
], ids=["marked-teammate", "agent-type-only-candidate"])
def test_a_dispatch_in_another_session_does_not_join(
        fresh_db, mirror, sidecar, lead, expected_role):
    _put(mirror, "-root-x/sess-u/sess-u.jsonl", lead)
    _put(mirror, _MEMBER_KEY, _fixture("teammate_member.jsonl"))
    _put(mirror, _MEMBER_KEY.replace(".jsonl", ".meta.json"), sidecar)
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert _member_type() == expected_role


def _dispatch_line(ts: str, call_id: str, role: str) -> dict:
    return {"type": "assistant", "timestamp": ts, "uuid": f"u-{call_id}",
            "requestId": f"req-{call_id}", "sessionId": "sess-t",
            "message": {"role": "assistant", "model": "claude-opus-5",
                        "content": [{"type": "tool_use", "id": call_id,
                                     "name": "Agent",
                                     "input": {"subagent_type": role,
                                               "name": "migrate-rest",
                                               "prompt": "go"}}],
                        "usage": {"input_tokens": 1, "output_tokens": 1}}}


def _result_line(ts: str, call_id: str, is_error: bool) -> dict:
    return {"type": "user", "timestamp": ts, "uuid": f"r-{call_id}",
            "sessionId": "sess-t",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": call_id,
                 "is_error": is_error, "content": "spawned"}]}}


def test_a_respawned_name_takes_the_dispatch_that_started_this_teammate(
        fresh_db, mirror):
    """The member's first record is at 12:00:03. The dispatch it came
    from is the latest successful one at or before then: not the older
    same-named one, not the failed one, not a later respawn."""
    lines = [
        _dispatch_line("2026-09-01T11:00:00Z", "toolu_old", "Explore"),
        _result_line("2026-09-01T11:00:00Z", "toolu_old", False),
        _dispatch_line("2026-09-01T12:00:00Z", "toolu_ok", "implementer"),
        _result_line("2026-09-01T12:00:00Z", "toolu_ok", False),
        _dispatch_line("2026-09-01T12:00:01Z", "toolu_bad", "code-reviewer"),
        _result_line("2026-09-01T12:00:01Z", "toolu_bad", True),
        _dispatch_line("2026-09-01T13:00:00Z", "toolu_new", "spec-reviewer"),
    ]
    _put(mirror, _LEAD_KEY,
         b"".join(json.dumps(line).encode() + b"\n" for line in lines))
    _put_member(mirror)
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert _member_type() == "implementer"


def test_a_dispatch_at_the_teammates_first_record_joins(fresh_db, mirror):
    """The member's first record is at 12:00:03; a dispatch logged in that
    same instant still started it."""
    _put(mirror, _LEAD_KEY, json.dumps(_dispatch_line(
        "2026-09-01T12:00:03Z", "toolu_eq", "implementer")).encode() + b"\n")
    _put_member(mirror)
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert _member_type() == "implementer"


def test_a_teammate_with_no_records_yet_still_joins(fresh_db, mirror):
    _put(mirror, _LEAD_KEY, _fixture("teammate_lead.jsonl"))
    _put(mirror, _MEMBER_KEY,
         _fixture("teammate_member.jsonl").splitlines(keepends=True)[0])
    _put(mirror, _MEMBER_KEY.replace(".jsonl", ".meta.json"),
         _TEAMMATE_SIDECAR)
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert _member_type() == "implementer"


def test_a_plain_subagent_sidecar_is_unchanged_by_the_teammate_pass(
        fresh_db, mirror):
    key = "-root-x/sess-t/subagents/agent-a0123456789abcdef.jsonl"
    _put(mirror, _LEAD_KEY, _fixture("teammate_lead.jsonl"))
    _put(mirror, key, _fixture("cross_file_agent.jsonl"))
    _put(mirror, key.replace(".jsonl", ".meta.json"),
         b'{"agentType":"code-reviewer","name":"migrate-rest",'
         b'"toolUseId":"toolu_p1"}')
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    with db.viz_conn() as c:
        row = c.execute("SELECT agent_type FROM files WHERE file_key = %s",
                        (f"claude/{key}",)).fetchone()
    assert row == ("code-reviewer",)


# ---- the compaction replay must never outrank the canonical dispatch ------

_COMPACTION_KEY = "-root-x/sess-t/subagents/agent-acompact-1.jsonl"


def _replayed_dispatch_lines() -> list[dict]:
    """A compaction sidecar's replay of the lead's dispatch: same uuid,
    requestId and tool_use_id at the same ts, but a DIFFERENT
    subagent_type, and sitting at a higher line_num than the original's
    (the leading user-line copies push it to line 3; the original is
    line 2 of the lead file)."""
    user = {"type": "user", "timestamp": "2026-09-01T12:00:00Z",
            "uuid": "tl-u1", "sessionId": "sess-t",
            "message": {"role": "user", "content": "split the work"}}
    replay = _dispatch_line("2026-09-01T12:00:01Z", "toolu_tl1",
                            "code-reviewer")
    replay["uuid"] = "tl-a1"
    replay["requestId"] = "req-tl1"
    return [user, user, replay]


def test_a_compaction_replay_of_the_dispatch_never_wins_the_join(
        fresh_db, mirror):
    _put(mirror, _LEAD_KEY, _fixture("teammate_lead.jsonl"))
    _put(mirror, _COMPACTION_KEY, b"".join(
        json.dumps(line).encode() + b"\n"
        for line in _replayed_dispatch_lines()))
    _put_member(mirror)
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert _member_type() == "implementer"
