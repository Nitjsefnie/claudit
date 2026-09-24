"""Codex and Kimi transcripts through claudit's parse_file (D2, D3, D7)."""
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend import constants, db, ingest, parse
from backend.r2 import R2Object

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"
CLAUDIT_RECORD_KEYS = {
    "file_key", "line_num", "uuid", "request_id", "ts", "model",
    "fresh_tokens", "cache_creation_tokens", "cache_read_tokens",
    "output_tokens", "text_chars", "reply_latency_s", "stop_reason",
    "effort", "thinking_tokens", "cli_version", "turn_flags",
    "turn_tool_results", "eph5_tokens", "eph1h_tokens", "cost_usd",
}


@pytest.mark.parametrize("name,fmt", [
    ("codex_min.jsonl", "codex"),
    ("kimi_code_min.jsonl", "kimi-code"),
    ("kimi_legacy_min.jsonl", "legacy"),
])
def test_each_lane_format_is_sniffed(name, fmt):
    assert parse.sniff_format((FIX / name).read_bytes()) == fmt


def test_a_claude_transcript_still_sniffs_as_claude():
    assert parse.sniff_format((FIX / "unsplit_cache.jsonl").read_bytes()) == "claude"


@pytest.mark.parametrize("name", [
    "codex_min.jsonl", "kimi_code_min.jsonl", "kimi_legacy_min.jsonl"])
def test_lane_records_carry_every_column_claudit_persists(name):
    out = parse.parse_file(f"sessions/p/s/{name}", (FIX / name).read_bytes())
    assert out["records"], "fixture must hold at least one usage record"
    for r in out["records"]:
        assert CLAUDIT_RECORD_KEYS <= set(r), CLAUDIT_RECORD_KEYS - set(r)
        assert r["eph5_tokens"] == r["eph1h_tokens"] == 0
        assert r["turn_flags"] == []
    assert {"prompt_count", "models", "agent_type"} <= set(out)


def test_codex_reasoning_lands_in_thinking_tokens():
    out = parse.parse_file("sessions/p/s/wire.jsonl",
                           (FIX / "codex_min.jsonl").read_bytes())
    r = out["records"][0]
    assert 0 < r["thinking_tokens"] <= r["output_tokens"]
    assert "reasoning_output_tokens" not in r


@pytest.mark.parametrize("raw,label", [
    ("gpt-6-sol", "gpt-6-sol"), ("GPT-6-Sol", "gpt-6-sol"),
    ("gpt-6-luna", "gpt-6-luna"), ("gpt-5.6-sol", "gpt-5.6-sol"),
    ("gpt-5.6-luna", "gpt-5.6-luna"), ("gpt-5.6-terra", "gpt-5.6-terra"),
    ("gpt-6-astra", "gpt-6-astra"),
])
def test_codex_model_map_keeps_the_generations_apart(raw, label):
    from backend import parse_codex  # pylint: disable=import-outside-toplevel
    assert parse_codex._codex_model(raw) == label  # pylint: disable=protected-access


def test_lane_tool_uses_carry_claudit_columns():
    out = parse.parse_file("sessions/p/s/wire.jsonl",
                           (FIX / "codex_min.jsonl").read_bytes())
    tu = out["tool_uses"][0]
    for key in ("tool_use_id", "error_kind", "error_text", "agent_type",
                "agent_model", "dispatch_prompt_chars", "dispatch_brief_ref",
                "result_chars", "read_kind", "read_targets", "write_targets",
                "is_reread"):
        assert key in tu, key
    assert "tool_call_id" not in tu


def test_a_claude_line_with_a_string_message_sniffs_claude():
    """A non-dict "message" must not crash the legacy rung (it used to
    raise AttributeError on .get); the file keeps sniffing claude."""
    blob = b'{"sessionId":"x","type":"system","message":"hi"}\n'
    assert parse.sniff_format(blob) == "claude"


def test_a_kimi_code_llm_error_line_with_a_string_message_does_not_crash():
    """kimi-code's llm.error carries a string message. A fragment leading
    with one falls through the rungs to the claude catch-all instead of
    raising -- an unidentifiable fragment parses empty either way, which
    is the pre-dispatch behaviour."""
    blob = (b'{"type":"llm.error","time":1784213155000,'
            b'"kind":"quota_exhausted","message":"out of quota"}\n')
    assert parse.sniff_format(blob) == "claude"
    assert parse.parse_file("s/f/llm.jsonl", blob)["records"] == []


@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    """Per-test schema reset on a separate DB (tests/test_ingest.py's)."""
    test_db = "claudit_test"
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")
    os.system(f"createdb {test_db} 2>/dev/null")
    os.system(f"psql {test_db} -f {Path(__file__).resolve().parents[1] / 'backend/schema.sql'} >/dev/null")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    db.reset_viz_pool()
    yield
    db.reset_viz_pool()
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")


@pytest.mark.parametrize("name,tool_use_id,prompt_count,models", [
    ("codex_min.jsonl", "call_synthetic01", 1, ["gpt-6-astra"]),
    ("kimi_code_min.jsonl", "tool_Synthetic01Example", 2, ["kimi-k2-7-code"]),
    # Legacy ids are per-session sequences ("tc1" repeats across unrelated
    # sessions in the kimi bucket), so the adapter namespaces them with the
    # file key before ingest groups tool_uses by the id ACROSS files.
    ("kimi_legacy_min.jsonl", "sessions/p/s/wire.jsonl:tc1", 1, ["kimi-k3"]),
])
def test_a_lane_transcript_persists_through_ingest(
        fresh_db, name, tool_use_id, prompt_count, models):
    """Round trip: parse_file -> ingest._persist must land rows in files,
    records and tool_uses. Guards the schema's NOT NULLs -- request_id is
    NOT NULL DEFAULT '' and files.agent_type is NOT NULL -- which None
    values from the adapter used to violate for every lane format.

    The key is the real lane shape (wire.jsonl under sessions/): since
    key_layout owns the mapping, a non-wire basename inside sessions/ is
    no longer a transcript at all."""
    key = "sessions/p/s/wire.jsonl"
    out = parse.parse_file(key, (FIX / name).read_bytes())
    assert out["records"] and out["tool_uses"]
    when = datetime(2026, 9, 22, tzinfo=timezone.utc)
    ingest._persist(  # pylint: disable=protected-access
        R2Object(key=key, etag="etag", size=1024, last_modified=when),
        {"project_id": "p", "display_name": "p",
         "first_seen_at": when, "last_seen_at": when},
        out, constants.PARSER_VERSION,
    )
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT agent_type, prompt_count, models FROM files "
            "WHERE file_key = %s", (key,)).fetchone()
        assert row is not None, "no files row"
        assert row[0] == "general-purpose"
        assert row[1] == prompt_count
        assert row[2] == models
        n_records = c.execute(
            "SELECT COUNT(*) FROM records WHERE file_key = %s", (key,)
        ).fetchone()
        assert n_records is not None and n_records[0] == len(out["records"])
        empty_request_ids = c.execute(
            "SELECT COUNT(*) FROM records WHERE file_key = %s "
            "AND request_id <> ''", (key,)).fetchone()
        assert empty_request_ids is not None and empty_request_ids[0] == 0
        ids = [r[0] for r in c.execute(
            "SELECT tool_use_id FROM tool_uses WHERE file_key = %s "
            "ORDER BY idx", (key,)).fetchall()]
    assert ids == [tu["tool_use_id"] for tu in out["tool_uses"]]
    assert tool_use_id in ids


# ---- agent role: what a lane transcript ran as, what a dispatch asked for --

def _lane(name: str) -> dict:
    return parse.parse_file("sessions/p/s/wire.jsonl", (FIX / name).read_bytes())


def _codex_meta(*payloads: str) -> bytes:
    """A Codex rollout of session_meta lines, one per payload body."""
    return b"".join(
        b'{"timestamp":"2026-09-10T08:44:15Z","type":"session_meta",'
        b'"payload":{"session_id":"s"%s}}\n' % p.encode() for p in payloads)


def test_codex_subagent_file_records_its_agent_role():
    """The role rides the second session_meta; the first, without it,
    must not shadow it -- the non-empty value wins."""
    assert _lane("codex_agent_role.jsonl")["agent_type"] == "explorer"


def test_codex_main_file_without_a_role_is_the_default_agent_type():
    assert _lane("codex_min.jsonl")["agent_type"] == constants.DEFAULT_AGENT_TYPE


@pytest.mark.parametrize("payload", [
    ',"agent_role":"default"',   # Codex's own name for "no role profile"
    ',"agent_role":""',
    ',"agent_role":null',
])
def test_codex_default_or_empty_role_is_the_default_agent_type(payload):
    out = parse.parse_file("sessions/p/s/wire.jsonl", _codex_meta(payload))
    assert out["agent_type"] == constants.DEFAULT_AGENT_TYPE


def test_codex_first_non_empty_role_wins():
    out = parse.parse_file("sessions/p/s/wire.jsonl", _codex_meta(
        "", ',"agent_role":"task-reviewer"', ',"agent_role":"explorer"'))
    assert out["agent_type"] == "task-reviewer"


def test_codex_role_falls_back_to_the_thread_spawn_mirror():
    out = parse.parse_file("sessions/p/s/wire.jsonl", _codex_meta(
        ',"source":{"subagent":{"thread_spawn":{"agent_role":"adversary"}}}'))
    assert out["agent_type"] == "adversary"


@pytest.mark.parametrize("bogus", ['{"name":"x"}', '["x"]', "7", "true"])
def test_codex_non_string_role_falls_back_to_the_thread_spawn_mirror(bogus):
    """A truthy agent_role that is not a string is no role at all: it must
    not shadow the thread_spawn mirror."""
    out = parse.parse_file("sessions/p/s/wire.jsonl", _codex_meta(
        ',"agent_role":%s,"source":{"subagent":{"thread_spawn":'
        '{"agent_role":"adversary"}}}' % bogus))
    assert out["agent_type"] == "adversary"


@pytest.mark.parametrize("bogus", [{"name": "x"}, ["x"], 7, True, ""])
def test_codex_session_role_is_a_string_or_none(bogus):
    """The thread_spawn mirror goes through the same guard as agent_role:
    whatever it holds, the helper hands back a non-empty str or None."""
    from backend import parse_codex  # pylint: disable=import-outside-toplevel
    payload = {"source": {"subagent": {"thread_spawn": {"agent_role": bogus}}}}
    assert parse_codex._codex_session_role(payload) is None  # pylint: disable=protected-access


def test_codex_replayed_parent_session_meta_does_not_lend_its_role():
    """A forked subagent rollout opens with its OWN session_meta (no role)
    and then replays its parent's, which names one. The parent's role is
    not this thread's: the file stays unattributed. (Shape of a real
    rollout: thread `k` forked from `f`, both under root session `s`.)"""
    out = parse.parse_file("sessions/p/s/wire.jsonl", _codex_meta(
        ',"id":"k","agent_role":null,"source":{"subagent":{"thread_spawn":'
        '{"parent_thread_id":"f","agent_role":null}}}',
        ',"id":"f","agent_role":"code-reviewer","source":{"subagent":'
        '{"thread_spawn":{"parent_thread_id":"s",'
        '"agent_role":"code-reviewer"}}}'))
    assert out["agent_type"] == constants.DEFAULT_AGENT_TYPE


def test_codex_later_session_meta_of_the_same_thread_still_names_the_role():
    """Restricting the role to the file's own thread keeps first-non-empty
    within it: a later session_meta with the same id may declare it."""
    out = parse.parse_file("sessions/p/s/wire.jsonl", _codex_meta(
        ',"id":"k"', ',"id":"k","agent_role":"explorer"'))
    assert out["agent_type"] == "explorer"


def _dispatch_cols(tu: dict) -> tuple:
    return (tu["agent_type"], tu["agent_model"],
            tu["dispatch_prompt_chars"], tu["dispatch_brief_ref"])


def test_codex_spawn_agent_calls_record_what_they_asked_for():
    """Both spellings of the dispatch tool; an empty model is None,
    unparseable arguments yield nothing, and the encrypted `message`
    gives no prompt shape. A non-dispatch call stays unattributed even
    when its arguments happen to carry an agent_type."""
    by_id = {tu["tool_use_id"]: tu
             for tu in _lane("codex_spawn_agent.jsonl")["tool_uses"]}
    assert _dispatch_cols(by_id["call_spawn01"]) == (
        "implementer", "gpt-6-astra", None, None)
    assert _dispatch_cols(by_id["call_spawn02"]) == (
        "explorer", None, None, None)
    assert _dispatch_cols(by_id["call_spawn03"]) == (None, None, None, None)
    assert _dispatch_cols(by_id["call_wait01"]) == (None, None, None, None)


def test_kimi_code_first_profile_name_is_the_agent_type():
    assert _lane("kimi_code_agent_dispatch.jsonl")["agent_type"] == "coder"


def _kc_profile(profile: str) -> bytes:
    return (b'{"type":"metadata","protocol_version":"1.4",'
            b'"created_at":1782740973430}\n'
            b'{"type":"config.update","profileName":%s,'
            b'"time":1782740973431}\n' % profile.encode())


@pytest.mark.parametrize("profile", ['"agent"', '""', "null"])
def test_kimi_code_default_or_empty_profile_is_the_default_agent_type(profile):
    """`agent` is kimi-code's name for the default profile."""
    out = parse.parse_file("sessions/p/s/wire.jsonl", _kc_profile(profile))
    assert out["agent_type"] == constants.DEFAULT_AGENT_TYPE


def test_kimi_code_empty_profile_does_not_shadow_a_later_one():
    blob = _kc_profile('""') + (b'{"type":"config.update",'
                                b'"profileName":"implementer"}\n')
    assert parse.parse_file("sessions/p/s/wire.jsonl", blob)[
        "agent_type"] == "implementer"


def test_kimi_code_without_a_profile_is_the_default_agent_type():
    assert _lane("kimi_code_min.jsonl")["agent_type"] == constants.DEFAULT_AGENT_TYPE


def test_kimi_code_agent_call_records_subagent_type_and_prompt_shape():
    """The prompt is plain text on this lane, so its shape is measured
    the same way the Claude path measures it. AgentSwarm stays
    unattributed."""
    by_id = {tu["tool_use_id"]: tu
             for tu in _lane("kimi_code_agent_dispatch.jsonl")["tool_uses"]}
    prompt = "Read /tmp/briefs/task-1.md IN FULL and execute it."
    assert _dispatch_cols(by_id["tool_Agent01"]) == (
        "explore", None, len(prompt), True)
    assert _dispatch_cols(by_id["tool_Swarm01"]) == (None, None, None, None)


def test_kimi_code_message_tool_call_agent_carries_a_model():
    """The v1.0/v1.1 toolCalls shape, arguments as a JSON string."""
    blob = (b'{"type":"metadata","protocol_version":"1.4",'
            b'"created_at":1782740973430}\n'
            b'{"type":"context.append_message","message":{"role":"assistant",'
            b'"content":[],"toolCalls":[{"type":"function","id":"tool_A2",'
            b'"function":{"name":"Agent","arguments":"{\\"subagent_type\\":'
            b'\\"coder\\",\\"model\\":\\"k3\\",\\"prompt\\":\\"do it\\"}"}}]},'
            b'"time":1782740973431}\n')
    tu = parse.parse_file("sessions/p/s/wire.jsonl", blob)["tool_uses"][0]
    assert _dispatch_cols(tu) == ("coder", "k3", 5, False)


def test_legacy_has_no_role_and_its_agent_call_is_shaped():
    """Legacy kimi-cli carries no role signal, so the file is DEFAULT;
    its Agent calls name no subagent_type but carry a prompt."""
    out = _lane("kimi_legacy_agent.jsonl")
    assert out["agent_type"] == constants.DEFAULT_AGENT_TYPE
    prompt = "Implement the wiring inline."
    assert _dispatch_cols(out["tool_uses"][0]) == (
        None, None, len(prompt), False)


def test_lane_parse_output_does_not_leak_the_raw_role():
    assert "agent_role" not in _lane("codex_agent_role.jsonl")


def test_lane_roles_and_dispatches_persist_through_ingest(fresh_db):
    """files.agent_type carries the role, tool_uses the dispatch, and
    dispatch_rollup counts the lane dispatch (it filters on agent_type,
    not on the Claude tool names)."""
    key = "sessions/p/s/wire.jsonl"
    out = parse.parse_file(key, (FIX / "codex_agent_role.jsonl").read_bytes()
                           + (FIX / "codex_spawn_agent.jsonl").read_bytes())
    when = datetime(2026, 9, 22, tzinfo=timezone.utc)
    ingest._persist(  # pylint: disable=protected-access
        R2Object(key=key, etag="etag", size=1024, last_modified=when),
        {"project_id": "p", "display_name": "p",
         "first_seen_at": when, "last_seen_at": when},
        out, constants.PARSER_VERSION,
    )
    ingest.rebuild_dispatch_rollup()
    with db.viz_conn() as c:
        role = c.execute("SELECT agent_type FROM files WHERE file_key = %s",
                         (key,)).fetchone()
        calls = c.execute(
            "SELECT agent_type, agent_model FROM tool_uses "
            "WHERE file_key = %s AND agent_type IS NOT NULL ORDER BY idx",
            (key,)).fetchall()
        rolled = c.execute(
            "SELECT agent_type, agent_model, n FROM dispatch_rollup "
            "ORDER BY agent_type").fetchall()
    assert role is not None and role[0] == "explorer"
    assert calls == [("implementer", "gpt-6-astra"), ("explorer", None)]
    assert rolled == [("explorer", "", 1), ("implementer", "gpt-6-astra", 1)]
