"""A subagent transcript's agent type from its sibling meta.json sidecar.

Legacy kimi-cli subagent wires carry no role in-band, and pre-2.1.126
Claude subagent transcripts carry no ``attributionAgent``; both archive a
``meta.json`` sidecar beside the transcript that names the role the
dispatch asked for:

- lane:   sessions/<p>/<s>/subagents/<id>/meta.json[.xz] beside wire.jsonl
          ({"subagent_type": ..., "launch_spec": {"subagent_type": ...}})
- Claude: <dir>/agent-<id>.meta.json[.xz] beside agent-<id>.jsonl[.xz]
          ({"agentType": ...})

Precedence: an in-band role > the sidecar's role > DEFAULT_AGENT_TYPE.
A missing or unreadable sidecar is today's behaviour, never a failure.
"""
from __future__ import annotations

import json
import lzma
import os
from pathlib import Path

import pytest
# The ingest suite's fresh-DB fixture, registered here under its own
# name ("fresh_db") by having the function object in this module.
from test_ingest import _fresh_db_fixture

from backend import agent_sidecar, constants, db, ingest, parse
from backend.key_layout import sidecar_stem, transcript_stem

__all__ = ["_fresh_db_fixture"]

_PARSER_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "parser"
DEFAULT = constants.DEFAULT_AGENT_TYPE


# ---- pairing: which sidecar belongs to which transcript -------------------

@pytest.mark.parametrize("wire, meta", [
    ("-root-x/s1/subagents/agent-a1.jsonl.xz",
     "-root-x/s1/subagents/agent-a1.meta.json.xz"),
    ("-root-x/s1/subagents/agent-a1.jsonl",
     "-root-x/s1/subagents/agent-a1.meta.json"),
    ("-root-x/s1/subagents/workflows/wf_1/agent-a2.jsonl.xz",
     "-root-x/s1/subagents/workflows/wf_1/agent-a2.meta.json.xz"),
    ("sessions/p1/s1/subagents/a3/wire.jsonl.xz",
     "sessions/p1/s1/subagents/a3/meta.json.xz"),
    ("sessions/p1/s1/subagents/a3/wire.jsonl",
     "sessions/p1/s1/subagents/a3/meta.json"),
])
def test_a_sidecar_pairs_with_the_transcript_beside_it(wire, meta):
    assert transcript_stem(wire) is not None
    assert sidecar_stem(meta) == transcript_stem(wire)


@pytest.mark.parametrize("key", [
    "-root-x/s1/subagents/agent-a1.jsonl.xz",   # a transcript
    "-root-x/s1/data/tool-results/x.txt",
    "sessions/p1/project.json",                 # the project marker
    "sessions/p1/s1/meta.json.xz",              # not a subagent dir
    "sessions/p1/s1/subagents/a3/state.json",
    "x.meta.json",                              # too shallow to be a transcript's
])
def test_non_sidecar_keys_have_no_sidecar_stem(key):
    assert sidecar_stem(key) is None


@pytest.mark.parametrize("key", [
    "-root-x/s1/subagents/agent-a1.meta.json.xz",
    "sessions/p1/s1/subagents/a3/meta.json.xz",
    "sessions/p1/project.json",
])
def test_non_transcript_keys_have_no_transcript_stem(key):
    assert transcript_stem(key) is None


def test_a_different_agent_does_not_pair():
    assert (sidecar_stem("-root-x/s1/subagents/agent-a1.meta.json.xz")
            != transcript_stem("-root-x/s1/subagents/agent-a2.jsonl.xz"))


def test_a_lane_main_wire_never_pairs_with_a_subagent_sidecar():
    assert (transcript_stem("sessions/p1/s1/wire.jsonl.xz")
            != sidecar_stem("sessions/p1/s1/subagents/a3/meta.json.xz"))


# ---- resolution: in-band > sidecar > DEFAULT -------------------------------

def _parsed(fixture: str, key: str) -> dict:
    return parse.parse_file(key, (_PARSER_FIX / fixture).read_bytes())


_LANE_KEY = "sessions/p/s/subagents/a1/wire.jsonl"
_CLAUDE_KEY = "-root-x/s/subagents/agent-a1.jsonl"


def test_legacy_wire_takes_the_sidecar_subagent_type():
    out = agent_sidecar.apply_agent_sidecar(
        _parsed("kimi_legacy_min.jsonl", _LANE_KEY),
        json.dumps({"subagent_type": "coder",
                    "launch_spec": {"subagent_type": "explore"}}).encode(),
        _LANE_KEY)
    assert out["agent_type"] == "coder"


def test_legacy_wire_falls_back_to_the_launch_spec_subagent_type():
    out = agent_sidecar.apply_agent_sidecar(
        _parsed("kimi_legacy_min.jsonl", _LANE_KEY),
        json.dumps({"subagent_type": None,
                    "launch_spec": {"subagent_type": "explore"}}).encode(),
        _LANE_KEY)
    assert out["agent_type"] == "explore"


def test_a_sidecar_role_goes_through_the_lanes_normalisation():
    """kimi-code's own name for its default profile is DEFAULT, whichever
    of the wire and the sidecar says it."""
    blob = (b'{"type":"metadata","protocol_version":"1.4",'
            b'"created_at":1782740973430}\n'
            b'{"type":"turn.prompt","input":[{"type":"text","text":"go"}],'
            b'"origin":{"kind":"user"},"time":1782740973442}\n')
    parsed = parse.parse_file(_LANE_KEY, blob)
    assert parse.sniff_format(blob) == "kimi-code"
    assert parsed["agent_type_in_band"] is False
    out = agent_sidecar.apply_agent_sidecar(
        parsed, b'{"subagent_type":"agent"}', _LANE_KEY)
    assert out["agent_type"] == DEFAULT


def test_lane_normalisation_follows_the_key_layout_not_the_sniff():
    """An empty lane wire sniffs as claude (the catch-all), but its
    sidecar is still a lane sidecar: Kimi's default-profile name maps to
    DEFAULT. The same value beside a Claude-layout transcript is kept."""
    assert parse.sniff_format(b"") == "claude"
    lane = agent_sidecar.apply_agent_sidecar(
        parse.parse_file(_LANE_KEY, b""), b'{"subagent_type":"agent"}',
        _LANE_KEY)
    assert lane["agent_type"] == DEFAULT
    claude = agent_sidecar.apply_agent_sidecar(
        parse.parse_file(_CLAUDE_KEY, b""), b'{"agentType":"agent"}',
        _CLAUDE_KEY)
    assert claude["agent_type"] == "agent"


def test_in_band_kimi_code_profile_wins_over_the_sidecar():
    out = agent_sidecar.apply_agent_sidecar(
        _parsed("kimi_code_agent_dispatch.jsonl", _LANE_KEY),
        b'{"subagent_type":"implementer"}', _LANE_KEY)
    assert out["agent_type"] == "coder"


def test_claude_subagent_without_attribution_takes_the_sidecar_agent_type():
    out = agent_sidecar.apply_agent_sidecar(
        _parsed("cross_file_agent.jsonl", _CLAUDE_KEY),
        b'{"agentType":"code-reviewer","description":"d","spawnDepth":1}',
        _CLAUDE_KEY)
    assert out["agent_type"] == "code-reviewer"


def test_in_band_attribution_agent_wins_over_the_sidecar():
    out = agent_sidecar.apply_agent_sidecar(
        _parsed("agent_attribution.jsonl", _CLAUDE_KEY),
        b'{"agentType":"code-reviewer"}', _CLAUDE_KEY)
    assert out["agent_type"] == "implementer"


@pytest.mark.parametrize("sidecar", [
    b"", b"not json", b"[1, 2]", b'"coder"', b"{}",
    b'{"agentType": ""}', b'{"agentType": 7}',
    b'{"subagent_type": null, "launch_spec": "coder"}',
    b"\xff\xfe",
])
def test_an_unusable_sidecar_leaves_the_default(sidecar):
    out = agent_sidecar.apply_agent_sidecar(
        _parsed("kimi_legacy_min.jsonl", _LANE_KEY), sidecar, _LANE_KEY)
    assert out["agent_type"] == DEFAULT


# ---- ingest: the sidecar is found in the listing and read per file ---------

@pytest.fixture(name="lane_mirror")
def _lane_mirror_fixture(monkeypatch, tmp_path):
    """An empty file:// mirror of the default `claude` bucket."""
    bucket = tmp_path / "r2" / "claude"
    bucket.mkdir(parents=True)
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")
    monkeypatch.delenv("R2_BUCKET", raising=False)
    return bucket


def _put(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(lzma.compress(data) if path.suffix == ".xz" else data)


def _fixture(name: str) -> bytes:
    return (_PARSER_FIX / name).read_bytes()


def _agent_types() -> dict[str, str]:
    with db.viz_conn() as c:
        return dict(c.execute(
            "SELECT file_key, agent_type FROM files").fetchall())


_SUB = "sessions/p1/s1/subagents"


def test_ingest_attributes_subagents_from_their_sidecars(fresh_db, lane_mirror):
    # Legacy wire + xz sidecar -> the sidecar's role.
    _put(lane_mirror / _SUB / "a1" / "wire.jsonl.xz",
         _fixture("kimi_legacy_min.jsonl"))
    _put(lane_mirror / _SUB / "a1" / "meta.json.xz", json.dumps(
        {"agent_id": "a1", "subagent_type": "coder", "status": "killed",
         "launch_spec": {"subagent_type": "coder"}}).encode())
    # Legacy wire, no sidecar -> DEFAULT.
    _put(lane_mirror / _SUB / "a2" / "wire.jsonl",
         _fixture("kimi_legacy_min.jsonl"))
    # kimi-code wire naming its profile in-band -> the in-band profile.
    _put(lane_mirror / _SUB / "a3" / "wire.jsonl",
         _fixture("kimi_code_agent_dispatch.jsonl"))
    _put(lane_mirror / _SUB / "a3" / "meta.json",
         b'{"subagent_type":"implementer"}')
    # Claude subagent without attributionAgent + sidecar -> agentType.
    _put(lane_mirror / "-root-x" / "s2" / "subagents" / "agent-b1.jsonl.xz",
         _fixture("cross_file_agent.jsonl"))
    _put(lane_mirror / "-root-x" / "s2" / "subagents" / "agent-b1.meta.json.xz",
         b'{"agentType":"code-reviewer","spawnDepth":1}')
    # Claude subagent WITH attributionAgent + a disagreeing sidecar.
    _put(lane_mirror / "-root-x" / "s2" / "subagents" / "agent-b2.jsonl",
         _fixture("agent_attribution.jsonl"))
    _put(lane_mirror / "-root-x" / "s2" / "subagents" / "agent-b2.meta.json",
         b'{"agentType":"code-reviewer"}')

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["r2_listed"] == 5, "a sidecar is not a transcript"
    assert _agent_types() == {
        f"claude/{_SUB}/a1/wire.jsonl.xz": "coder",
        f"claude/{_SUB}/a2/wire.jsonl": DEFAULT,
        f"claude/{_SUB}/a3/wire.jsonl": "coder",
        "claude/-root-x/s2/subagents/agent-b1.jsonl.xz": "code-reviewer",
        "claude/-root-x/s2/subagents/agent-b2.jsonl": "implementer",
    }


def test_an_unreadable_sidecar_is_the_default_not_a_failure(
        fresh_db, lane_mirror):
    _put(lane_mirror / _SUB / "a1" / "wire.jsonl",
         _fixture("kimi_legacy_min.jsonl"))
    # Named .xz but not xz: inflating it raises inside the fetch.
    (lane_mirror / _SUB / "a1" / "meta.json.xz").write_bytes(b"not xz")
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["inserted"] == 1
    assert _agent_types() == {f"claude/{_SUB}/a1/wire.jsonl": DEFAULT}


def test_a_sidecar_that_vanishes_before_its_fetch_is_the_default(
        fresh_db, lane_mirror, monkeypatch):
    _put(lane_mirror / _SUB / "a1" / "wire.jsonl",
         _fixture("kimi_legacy_min.jsonl"))
    meta = lane_mirror / _SUB / "a1" / "meta.json"
    _put(meta, b'{"subagent_type":"coder"}')
    real_fetch = ingest._fetch_with_retry  # pylint: disable=protected-access

    def fetch(key: str) -> bytes:
        if key.endswith("meta.json") and meta.exists():
            os.unlink(meta)
        return real_fetch(key)

    monkeypatch.setattr(ingest, "_fetch_with_retry", fetch)
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert _agent_types() == {f"claude/{_SUB}/a1/wire.jsonl": DEFAULT}


def test_a_sidecar_is_not_fetched_when_the_role_is_in_band(
        fresh_db, lane_mirror, monkeypatch):
    _put(lane_mirror / _SUB / "a3" / "wire.jsonl",
         _fixture("kimi_code_agent_dispatch.jsonl"))
    _put(lane_mirror / _SUB / "a3" / "meta.json", b'{"subagent_type":"x"}')
    fetched: list[str] = []
    real_fetch = ingest._fetch_with_retry  # pylint: disable=protected-access

    def fetch(key: str) -> bytes:
        fetched.append(key)
        return real_fetch(key)

    monkeypatch.setattr(ingest, "_fetch_with_retry", fetch)
    ingest.run_ingest(trigger="manual")
    assert fetched == [f"claude/{_SUB}/a3/wire.jsonl"]


def test_a_sidecar_written_after_its_wire_triggers_a_reparse(
        fresh_db, lane_mirror):
    """The sidecar's etag joins the wire's in the reparse decision, so a
    sidecar that lands (or changes) after the wire was ingested is picked
    up on the next run without a PARSER_VERSION bump."""
    _put(lane_mirror / _SUB / "a1" / "wire.jsonl",
         _fixture("kimi_legacy_min.jsonl"))
    ingest.run_ingest(trigger="manual")
    assert _agent_types() == {f"claude/{_SUB}/a1/wire.jsonl": DEFAULT}

    meta = lane_mirror / _SUB / "a1" / "meta.json"
    _put(meta, b'{"subagent_type":"coder"}')
    result = ingest.run_ingest(trigger="manual")
    assert result["reparsed"] == 1
    assert _agent_types() == {f"claude/{_SUB}/a1/wire.jsonl": "coder"}

    _put(meta, b'{"subagent_type":"explore"}')
    result = ingest.run_ingest(trigger="manual")
    assert result["reparsed"] == 1
    assert _agent_types() == {f"claude/{_SUB}/a1/wire.jsonl": "explore"}

    result = ingest.run_ingest(trigger="manual")
    assert result["reparsed"] == 0, "an unchanged pair is not reparsed"

    meta.unlink()
    result = ingest.run_ingest(trigger="manual")
    assert result["reparsed"] == 1
    assert _agent_types() == {f"claude/{_SUB}/a1/wire.jsonl": DEFAULT}


def _fail_sidecar_gets(monkeypatch, exc: Exception) -> list[str]:
    """Make every GET of a meta.json raise `exc`; sleep is a no-op."""
    calls: list[str] = []
    real_get = ingest.r2.get_object

    def get_object(key: str) -> bytes:
        if "meta.json" in key:
            calls.append(key)
            raise exc
        return real_get(key)

    monkeypatch.setattr(ingest.r2, "get_object", get_object)
    monkeypatch.setattr(ingest.time, "sleep", lambda s: None)
    return calls


def test_a_transient_sidecar_failure_fails_the_file_and_is_retried(
        fresh_db, lane_mirror, monkeypatch):
    """A sidecar GET that keeps failing transiently must NOT persist the
    file: persisting it would store the pair etag with the default role,
    and the next healthy run would see nothing changed and never retry."""
    _put(lane_mirror / _SUB / "a1" / "wire.jsonl",
         _fixture("kimi_legacy_min.jsonl"))
    _put(lane_mirror / _SUB / "a1" / "meta.json", b'{"subagent_type":"coder"}')
    healthy_get = ingest.r2.get_object
    calls = _fail_sidecar_gets(monkeypatch, ConnectionResetError("reset"))
    result = ingest.run_ingest(trigger="manual")
    assert len(calls) == ingest.FETCH_ATTEMPTS, "the sidecar GET is retried"
    assert result["failed"] == 1
    assert not _agent_types(), "the file is a failure, not persisted"

    monkeypatch.setattr(ingest.r2, "get_object", healthy_get)
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["inserted"] == 1
    assert _agent_types() == {f"claude/{_SUB}/a1/wire.jsonl": "coder"}


def test_a_fatal_sidecar_fetch_error_escapes_to_the_run(
        fresh_db, lane_mirror, monkeypatch):
    """A bug in the fetch is FatalFetchError on the sidecar path too: it
    reaches the run-level handler instead of being absorbed per file."""
    _put(lane_mirror / _SUB / "a1" / "wire.jsonl",
         _fixture("kimi_legacy_min.jsonl"))
    _put(lane_mirror / _SUB / "a1" / "meta.json", b'{"subagent_type":"coder"}')
    calls = _fail_sidecar_gets(monkeypatch, TypeError("bug"))
    result = ingest.run_ingest(trigger="manual")
    assert len(calls) == 1, "a bug is not retried"
    assert result["error"].startswith("FatalFetchError:"), result["error"]
    assert not _agent_types()
