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

from backend import constants, db, ingest, parse

__all__ = ["_fresh_db_fixture"]

_PARSER_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "parser"
DEFAULT = constants.DEFAULT_AGENT_TYPE

_TEAMMATE_SIDECAR = json.dumps({
    "agentType": "migrate-rest", "description": "Migrate the rest",
    "name": "migrate-rest", "taskKind": "in_process_teammate",
    "teamName": "session-sess-t", "model": "opus"}).encode()

_MEMBER_KEY = "-root-x/sess-t/subagents/agent-amigrate-rest-0123456789abcdef.jsonl"
_LEAD_KEY = "-root-x/sess-t/sess-t.jsonl"


def _fixture(name: str) -> bytes:
    return (_PARSER_FIX / name).read_bytes()


def _member() -> dict:
    return parse.parse_file(_MEMBER_KEY, _fixture("teammate_member.jsonl"))


# ---- parse: a teammate sidecar names a teammate, not a role ----------------

def test_a_teammate_sidecar_names_no_role():
    out = parse.apply_agent_sidecar(_member(), _TEAMMATE_SIDECAR, _MEMBER_KEY)
    assert out["agent_type"] == DEFAULT
    assert out["teammate_name"] == "migrate-rest"
    assert parse.sidecar_agent_role(_TEAMMATE_SIDECAR) is None


def test_a_sidecar_whose_agent_type_is_its_name_is_a_teammate():
    """No taskKind: agentType equal to name still cannot be told from the
    teammate name, so the lead's dispatch decides."""
    sidecar = b'{"agentType":"scout-2","name":"scout-2","description":"d"}'
    out = parse.apply_agent_sidecar(_member(), sidecar, _MEMBER_KEY)
    assert out["agent_type"] == DEFAULT
    assert out["teammate_name"] == "scout-2"


def test_a_named_plain_subagent_keeps_its_sidecar_role():
    """An Agent call with a `name` that is not a teammate: the sidecar's
    agentType is the real role and differs from the name."""
    sidecar = (b'{"agentType":"implementer","name":"impl-task1",'
               b'"description":"d","toolUseId":"toolu_p1"}')
    out = parse.apply_agent_sidecar(_member(), sidecar, _MEMBER_KEY)
    assert out["agent_type"] == "implementer"
    assert out.get("teammate_name") is None


def test_a_teammate_with_an_in_band_role_keeps_it():
    parsed = parse.parse_file(_MEMBER_KEY, _fixture("agent_attribution.jsonl"))
    out = parse.apply_agent_sidecar(parsed, _TEAMMATE_SIDECAR, _MEMBER_KEY)
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


# ---- ingest: the role is joined from the lead's dispatch -------------------

@pytest.fixture(name="mirror")
def _mirror_fixture(monkeypatch, tmp_path):
    """An empty file:// mirror of the default `claude` bucket."""
    bucket = tmp_path / "r2" / "claude"
    bucket.mkdir(parents=True)
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")
    monkeypatch.delenv("R2_BUCKET", raising=False)
    return bucket


def _put(bucket: Path, key: str, data: bytes) -> None:
    path = bucket / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _put_member(bucket: Path) -> None:
    _put(bucket, _MEMBER_KEY, _fixture("teammate_member.jsonl"))
    _put(bucket, _MEMBER_KEY.replace(".jsonl", ".meta.json"),
         _TEAMMATE_SIDECAR)


def _member_type() -> str:
    with db.viz_conn() as c:
        row = c.execute("SELECT agent_type FROM files WHERE file_key = %s",
                        (f"claude/{_MEMBER_KEY}",)).fetchone()
    assert row is not None
    return row[0]


def _rollup_types() -> set[str]:
    with db.viz_conn() as c:
        return {r[0] for r in c.execute(
            "SELECT DISTINCT agent_type FROM agent_rollup").fetchall()}


def test_ingest_takes_a_teammates_role_from_its_leads_dispatch(
        fresh_db, mirror):
    _put(mirror, _LEAD_KEY, _fixture("teammate_lead.jsonl"))
    _put_member(mirror)
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert _member_type() == "implementer"
    assert "migrate-rest" not in _rollup_types()
    assert "implementer" in _rollup_types()


def test_a_teammate_with_no_matching_dispatch_is_the_default(
        fresh_db, mirror):
    _put(mirror, _LEAD_KEY,
         _fixture("teammate_lead.jsonl").replace(b'"migrate-rest"',
                                                 b'"someone-else"'))
    _put_member(mirror)
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert _member_type() == DEFAULT
    assert "migrate-rest" not in _rollup_types()


def test_a_same_named_dispatch_in_another_session_does_not_join(
        fresh_db, mirror):
    _put(mirror, "-root-x/sess-u/sess-u.jsonl",
         _fixture("teammate_lead.jsonl"))
    _put_member(mirror)
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert _member_type() == DEFAULT


def test_a_lead_archived_after_its_teammate_resolves_it_on_that_run(
        fresh_db, mirror):
    _put_member(mirror)
    ingest.run_ingest(trigger="manual")
    assert _member_type() == DEFAULT

    _put(mirror, _LEAD_KEY, _fixture("teammate_lead.jsonl"))
    result = ingest.run_ingest(trigger="manual")
    assert result["inserted"] == 1 and result["reparsed"] == 0
    assert _member_type() == "implementer"


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
