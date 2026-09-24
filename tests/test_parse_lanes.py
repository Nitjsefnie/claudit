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


# ---- reply latency: anchor to the model's FIRST assistant output ----------
#
# The Claude path ends the window at the first assistant line (a thinking
# line included); the lanes must end it at the first assistant event of the
# turn, not at the turn's first billing record, which can land minutes or
# hours later when the cumulative counter stays flat.

_CODEX_TOKENS = (
    b'"info":{"total_token_usage":{"input_tokens":%d,"cached_input_tokens":0,'
    b'"cache_write_input_tokens":0,"output_tokens":5,'
    b'"reasoning_output_tokens":0,"total_tokens":%d},"last_token_usage":'
    b'{"input_tokens":%d,"cached_input_tokens":0,"cache_write_input_tokens":0,'
    b'"output_tokens":5,"reasoning_output_tokens":0,"total_tokens":%d}}')


def _codex_line(sec: int, rtype: str, body: str) -> bytes:
    """One rollout line `sec` seconds after 2026-09-10T08:00:00Z."""
    ts = datetime.fromtimestamp(1_789_027_200 + sec, tz=timezone.utc)
    stamp = ts.strftime("%Y-%m-%dT%H:%M:%SZ").encode()
    return (b'{"timestamp":"%s","type":"%s","payload":{%s}}\n'
            % (stamp, rtype.encode(), body.encode()))


def _codex_tokens(sec: int, total_in: int) -> bytes:
    info = _CODEX_TOKENS % (total_in, total_in + 5, total_in, total_in + 5)
    return _codex_line(sec, "event_msg",
                       '"type":"token_count",' + info.decode())


def _codex_turn_rollout(*middle: bytes) -> bytes:
    return (_codex_line(0, "session_meta", '"session_id":"s"')
            + _codex_line(10, "event_msg", '"type":"task_started"')
            + b"".join(middle))


_CODEX_ASSISTANT_ITEMS = {
    "item_completed Reasoning": ("event_msg",
                                 '"type":"item_completed","item":'
                                 '{"type":"Reasoning"}'),
    "response_item reasoning": ("response_item", '"type":"reasoning"'),
    "event_msg agent_reasoning": ("event_msg",
                                  '"type":"agent_reasoning","text":"t"'),
    "item_completed AgentMessage": ("event_msg",
                                    '"type":"item_completed","item":'
                                    '{"type":"AgentMessage","content":'
                                    '[{"type":"Text","text":"hi"}]}'),
    "event_msg agent_message": ("event_msg",
                                '"type":"agent_message","message":"hi"'),
    "function_call": ("response_item",
                      '"type":"function_call","name":"f","call_id":"c1",'
                      '"arguments":"{}"'),
    "web_search_call": ("response_item",
                        '"type":"web_search_call","status":"completed",'
                        '"action":{"type":"search","query":"q"}'),
    "local_shell_call": ("response_item",
                         '"type":"local_shell_call","call_id":"c2",'
                         '"status":"completed","action":{"type":"exec",'
                         '"command":["true"]}'),
}


@pytest.mark.parametrize("item", sorted(_CODEX_ASSISTANT_ITEMS))
def test_codex_latency_ends_at_the_first_assistant_item(item):
    """task_started at +10 s, the first assistant item at +16 s, a second
    one at +18 s, and the cumulative counter only moves at +4934 s: the
    reply began after 6 s, not 4924 s."""
    rtype, body = _CODEX_ASSISTANT_ITEMS[item]
    out = parse.parse_file("sessions/p/s/wire.jsonl", _codex_turn_rollout(
        _codex_line(16, rtype, body),
        _codex_line(18, "event_msg", '"type":"agent_message","message":"x"'),
        _codex_tokens(4934, 100)))
    assert [r["reply_latency_s"] for r in out["records"]] == [6.0]


def test_codex_latency_without_an_assistant_item_still_ends_at_the_record():
    out = parse.parse_file("sessions/p/s/wire.jsonl", _codex_turn_rollout(
        _codex_tokens(25, 100)))
    assert [r["reply_latency_s"] for r in out["records"]] == [15.0]


def test_codex_first_assistant_item_does_not_leak_into_the_next_turn():
    """Turn 1's assistant item, and a stray one between the turns, must
    not time turn 2, which has no assistant item before its record."""
    out = parse.parse_file("sessions/p/s/wire.jsonl", _codex_turn_rollout(
        _codex_line(12, "event_msg", '"type":"agent_message","message":"a"'),
        _codex_tokens(20, 100),
        _codex_line(21, "event_msg", '"type":"task_complete"'),
        _codex_line(30, "event_msg", '"type":"agent_message","message":"b"'),
        _codex_line(100, "event_msg", '"type":"task_started"'),
        _codex_tokens(110, 300)))
    assert [r["reply_latency_s"] for r in out["records"]] == [2.0, 10.0]


def test_codex_assistant_item_after_the_record_does_not_retime_it():
    """One latency per turn: an item after the first record is not a
    second measurement for a later record in the same turn."""
    out = parse.parse_file("sessions/p/s/wire.jsonl", _codex_turn_rollout(
        _codex_tokens(40, 100),
        _codex_line(41, "event_msg", '"type":"agent_message","message":"a"'),
        _codex_tokens(50, 300)))
    assert [r["reply_latency_s"] for r in out["records"]] == [30.0, None]


def _kc_line(ms: int, body: str) -> bytes:
    return b'{%s,"time":%d}\n' % (body.encode(), 1_783_000_000_000 + ms)


_KC_USAGE = ('"type":"usage.record","model":"kimi-code/kimi-for-coding",'
             '"usage":{"inputOther":10,"output":2,"inputCacheRead":0,'
             '"inputCacheCreation":0}')

_KC_ASSISTANT_EVENTS = {
    "content.part think": ('"type":"context.append_loop_event","event":'
                           '{"type":"content.part","turnId":"t1","part":'
                           '{"type":"think","think":"hm"}}'),
    "tool.call": ('"type":"context.append_loop_event","event":'
                  '{"type":"tool.call","turnId":"t1","toolCallId":"c1",'
                  '"name":"Bash","args":{"command":"true"}}'),
    "assistant message": ('"type":"context.append_message","message":'
                          '{"role":"assistant","content":'
                          '[{"type":"text","text":"hi"}]}'),
}


@pytest.mark.parametrize("event", sorted(_KC_ASSISTANT_EVENTS))
def test_kimi_code_latency_ends_at_the_first_assistant_event(event):
    blob = (_kc_line(0, '"type":"metadata","protocol_version":"1.4",'
                        '"created_at":1783000000000')
            + _kc_line(1000, '"type":"turn.prompt","input":[]')
            + _kc_line(3500, _KC_ASSISTANT_EVENTS[event])
            + _kc_line(4000, _KC_ASSISTANT_EVENTS["assistant message"])
            + _kc_line(900_000, _KC_USAGE))
    out = parse.parse_file("sessions/p/s/wire.jsonl", blob)
    assert [r["reply_latency_s"] for r in out["records"]] == [2.5]


def test_kimi_code_latency_without_an_assistant_event_ends_at_the_record():
    blob = (_kc_line(0, '"type":"metadata","protocol_version":"1.4",'
                        '"created_at":1783000000000')
            + _kc_line(1000, '"type":"turn.prompt","input":[]')
            + _kc_line(8000, _KC_USAGE))
    out = parse.parse_file("sessions/p/s/wire.jsonl", blob)
    assert [r["reply_latency_s"] for r in out["records"]] == [7.0]


_KC_META = _kc_line(0, '"type":"metadata","protocol_version":"1.4",'
                       '"created_at":1783000000000')


def _kc_event(ms: int, event: str) -> bytes:
    return _kc_line(ms, '"type":"context.append_loop_event","event":{%s}'
                    % event)


def _kc_user(ms: int, text: str) -> bytes:
    """The user message kimi-code appends when an input enters context."""
    return _kc_line(ms, '"type":"context.append_message","message":'
                        '{"role":"user","content":[{"type":"text","text":'
                        '"%s"}],"toolCalls":[]}' % text)


def _kc_input(ms: int, typ: str, text: str) -> bytes:
    return _kc_line(ms, '"type":"%s","input":[{"type":"text","text":"%s"}]'
                    % (typ, text))


def test_kimi_code_new_turn_id_keeps_the_seen_assistant_event():
    """A step.begin that opens a new turnId while the anchor is armed
    closes the bookkeeping turn but must not forget the output already
    seen: the reply began at the content.part, 2.5 s in."""
    blob = (_KC_META
            + _kc_line(1000, '"type":"turn.prompt","input":[]')
            + _kc_event(3500, '"type":"content.part","turnId":"t1","part":'
                              '{"type":"text","text":"hi"}')
            + _kc_event(4000, '"type":"step.begin","turnId":"t2"')
            + _kc_line(900_000, _KC_USAGE))
    out = parse.parse_file("sessions/p/s/wire.jsonl", blob)
    assert [r["reply_latency_s"] for r in out["records"]] == [2.5]


def test_kimi_code_assistant_event_before_the_anchor_yields_no_latency():
    """An assistant event stamped before the anchor (clock skew) ends the
    window before it opened: a negative gap is dropped, not replaced by
    the record's own ts."""
    blob = (_KC_META
            + _kc_line(5000, '"type":"turn.prompt","input":[]')
            + _kc_line(3000, _KC_ASSISTANT_EVENTS["content.part think"])
            + _kc_line(8000, _KC_USAGE))
    out = parse.parse_file("sessions/p/s/wire.jsonl", blob)
    assert [r["reply_latency_s"] for r in out["records"]] == [None]


def _kc_step(begin_ms: int, turn_id: str) -> bytes:
    return (_kc_event(begin_ms, f'"type":"step.begin","turnId":"{turn_id}"')
            + _kc_line(begin_ms + 1, '"type":"llm.request","kind":"loop"'))


def _kc_step_output(ms: int, turn_id: str) -> bytes:
    return _kc_event(ms, '"type":"content.part","turnId":"%s","part":'
                         '{"type":"text","text":"x"}' % turn_id)


def _kc_step_end(ms: int, turn_id: str) -> bytes:
    return (_kc_event(ms, f'"type":"step.end","turnId":"{turn_id}"')
            + _kc_line(ms, _KC_USAGE))


def _kc_prompted_turn() -> bytes:
    """A prompt at +1 s whose first step replies at +3 s and bills at +4 s,
    then a second step that is in flight (a tool call at +5 s)."""
    return (_KC_META
            + _kc_input(1000, "turn.prompt", "go")
            + _kc_user(1000, "go")
            + _kc_step(1001, "t1")
            + _kc_step_output(3000, "t1")
            + _kc_step_end(4000, "t1")
            + _kc_step(4001, "t1")
            + _kc_event(5000, '"type":"tool.call","turnId":"t1",'
                              '"toolCallId":"c1","name":"AskUserQuestion",'
                              '"args":{}'))


def test_kimi_code_steer_mid_step_anchors_at_its_delivery():
    """A steer arriving while a step is in flight (blocked on a question
    the user answers ~11.5 min later) enters context only after that step
    ends. The in-flight step's billing record is not the steer's reply;
    the steer's latency runs from its delivery to the next step's first
    output, 8 s."""
    blob = (_kc_prompted_turn()
            + _kc_input(10_000, "turn.steer", "also this")
            + _kc_event(700_000, '"type":"tool.result","toolCallId":"c1",'
                                 '"result":{"output":"ok"}')
            + _kc_step_end(700_000, "t1")
            + _kc_user(700_001, "also this")
            + _kc_step(700_010, "t1")
            + _kc_step_output(708_001, "t1")
            + _kc_step_end(709_000, "t1"))
    out = parse.parse_file("sessions/p/s/wire.jsonl", blob)
    assert [r["reply_latency_s"] for r in out["records"]] == [2.0, None, 8.0]


def test_kimi_code_steer_ignores_other_user_messages_until_delivered():
    """A user message that is not the steer's own (a Skill tool injecting
    instructions mid-step) is not its delivery, so it must not arm the
    anchor for the in-flight step's record to consume."""
    blob = (_kc_prompted_turn()
            + _kc_input(10_000, "turn.steer", "also this")
            + _kc_user(20_000, "Skill tool loaded instructions")
            + _kc_step_end(20_001, "t1")
            + _kc_user(20_002, "also this")
            + _kc_step(20_003, "t1")
            + _kc_step_output(26_002, "t1")
            + _kc_step_end(27_000, "t1"))
    out = parse.parse_file("sessions/p/s/wire.jsonl", blob)
    assert [r["reply_latency_s"] for r in out["records"]] == [2.0, None, 6.0]


def test_kimi_code_queued_steers_are_delivered_in_order():
    """Two steers queued behind one step are delivered first-in first-out;
    the window runs from the LAST delivery, when the next request can
    start, to the next step's first output."""
    blob = (_kc_prompted_turn()
            + _kc_input(10_000, "turn.steer", "first")
            + _kc_input(11_000, "turn.steer", "second")
            + _kc_step_end(30_000, "t1")
            + _kc_user(30_001, "first")
            + _kc_user(30_002, "second")
            + _kc_step(30_003, "t1")
            + _kc_step_output(33_002, "t1")
            + _kc_step_end(34_000, "t1"))
    out = parse.parse_file("sessions/p/s/wire.jsonl", blob)
    assert [r["reply_latency_s"] for r in out["records"]] == [2.0, None, 3.0]


def test_kimi_code_idle_steer_is_delivered_at_once():
    """A steer arriving between turns enters context immediately and opens
    the next turn: its latency runs from that delivery."""
    blob = (_kc_prompted_turn()
            + _kc_step_end(6000, "t1")
            + _kc_input(50_000, "turn.steer", "next")
            + _kc_user(50_000, "next")
            + _kc_step(50_007, "t2")
            + _kc_step_output(53_000, "t2")
            + _kc_step_end(54_000, "t2"))
    out = parse.parse_file("sessions/p/s/wire.jsonl", blob)
    assert [r["reply_latency_s"] for r in out["records"]] == [2.0, None, 3.0]


def _legacy_line(sec: float, msg_type: str, payload: str) -> bytes:
    return (b'{"timestamp": %.1f, "message": {"type": "%s", "payload": {%s}}}\n'
            % (1_784_226_000 + sec, msg_type.encode(), payload.encode()))


_LEGACY_STATUS = ('"message_id": "m1", "token_usage": {"input_other": 10, '
                  '"output": 2, "input_cache_read": 0, '
                  '"input_cache_creation": 0}')

_LEGACY_ASSISTANT_EVENTS = {
    "ContentPart think": ("ContentPart", '"type": "think", "think": "hm"'),
    "ContentPart text": ("ContentPart", '"type": "text", "text": "hi"'),
    "ToolCall": ("ToolCall", '"type": "function", "id": "tc1", "function": '
                             '{"name": "Shell", "arguments": "{}"}'),
}


@pytest.mark.parametrize("event", sorted(_LEGACY_ASSISTANT_EVENTS))
def test_legacy_kimi_latency_ends_at_the_first_assistant_event(event):
    msg_type, payload = _LEGACY_ASSISTANT_EVENTS[event]
    blob = (b'{"type": "metadata", "protocol_version": "1.10"}\n'
            + _legacy_line(0, "TurnBegin", '"user_input": "hi"')
            + _legacy_line(1.5, msg_type, payload)
            + _legacy_line(3, "ContentPart", '"type": "text", "text": "x"')
            + _legacy_line(900, "StatusUpdate", _LEGACY_STATUS))
    out = parse.parse_file("sessions/p/s/wire.jsonl", blob)
    assert [r["reply_latency_s"] for r in out["records"]] == [1.5]


def test_legacy_kimi_latency_without_an_assistant_event_ends_at_the_record():
    blob = (b'{"type": "metadata", "protocol_version": "1.10"}\n'
            + _legacy_line(0, "TurnBegin", '"user_input": "hi"')
            + _legacy_line(4, "StatusUpdate", _LEGACY_STATUS))
    out = parse.parse_file("sessions/p/s/wire.jsonl", blob)
    assert [r["reply_latency_s"] for r in out["records"]] == [4.0]
