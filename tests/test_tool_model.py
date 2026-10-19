"""Store each tool call's emitting model directly.
Joining records by file_key and line_num loses Claude calls on later lines
of a merged request and lane calls without a matching record line."""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, db, ingest, parse, parse_kimi
from tests import dims_seed
# Importing the fixture functions registers them here under their
# @pytest.fixture(name=...) names.
from tests.test_ingest import (  # noqa: F401  # pylint: disable=unused-import
    _fresh_db_fixture, _mini_r2_env_fixture, _scalar,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures"
LATER_LINE = FIX / "parser" / "tool_later_line.jsonl"
HAIKU = "claude-haiku-4-5"


def _parse(path: Path, key: str = "sessions/p/s/wire.jsonl") -> dict:
    return parse.parse_file(key, path.read_bytes())


@pytest.mark.parametrize("fixture, expected_model", [
    (LATER_LINE, HAIKU),
    (FIX / "parser" / "message_id_merge.jsonl", "glm-5.3-flash"),
], ids=["request-id-merge", "message-id-merge"])
def test_claude_tool_use_on_a_later_line_carries_its_model(fixture, expected_model):
    """The tool_use sits on line 3; the merged record lives on line 2, so a
    (file_key, line_num) join finds nothing for it."""
    out = _parse(fixture, "p/s/s.jsonl")
    assert [r["line_num"] for r in out["records"]] == [2]
    [tu] = out["tool_uses"]
    assert tu["line_num"] == 3
    assert tu["model"] == expected_model


def test_codex_tool_call_keeps_the_model_in_force():
    out = _parse(FIX / "codex" / "rollout_shell_churn.jsonl")
    assert out["tool_uses"]
    assert {tu["model"] for tu in out["tool_uses"]} == {"gpt-5.6-sol"}
    assert {r["model"] for r in out["records"]} == {"gpt-5.6-sol"}


def test_kimi_code_tool_call_takes_its_step_record_model():
    """kimi-code writes a step's tool calls BEFORE the usage record that
    bills the step, and the session switches model between steps."""
    out = _parse(FIX / "parser" / "kimi_code_step_model.jsonl")
    assert [r["model"] for r in out["records"]] == ["kimi-k2-7-code",
                                                    "kimi-k3"]
    assert [tu["model"] for tu in out["tool_uses"]] == ["kimi-k2-7-code",
                                                        "kimi-k3"]


@pytest.mark.parametrize("name,after_record", [
    ("kimi_legacy_min.jsonl", False),
    ("kimi_code.jsonl", True),
], ids=["legacy-call-before-record", "code-call-after-last-record"])
def test_kimi_tool_call_takes_its_step_record_model(name, after_record):
    out = _parse(FIX / "parser" / name)
    [rec] = out["records"]
    [tu] = out["tool_uses"]
    assert (tu["line_num"] > rec["line_num"]) is after_record
    assert tu["model"] == rec["model"]


def _add_later_line_session(mirror_claude: Path) -> None:
    dest = mirror_claude / "projT" / "sess-T" / "sess-T.jsonl"
    dest.parent.mkdir(parents=True)
    shutil.copy(LATER_LINE, dest)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_ingest_stores_the_model_and_the_tool_rollups_carry_it(
        fresh_db, mini_r2_env):
    _add_later_line_session(mini_r2_env)
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        stored = c.execute(
            "SELECT dm.value FROM tool_uses t "
            "LEFT JOIN dim_model dm ON dm.id = t.model_id "
            "WHERE t.tool_use_id = 'toolu_later1'"
        ).fetchall()
        rolled = c.execute(
            "SELECT n_total, n_rated, n_error FROM tool_rollup tr "
            "JOIN dim_model dm ON dm.id = tr.model_id "
            "JOIN dim_tool_name dtn ON dtn.id = tr.tool_name_id "
            "WHERE dm.value = %s AND dtn.value = 'Bash'", (HAIKU,)
        ).fetchall()
        errors = _scalar(
            c, "SELECT COALESCE(SUM(n), 0) FROM tool_error_rollup ter "
               "JOIN dim_model dm ON dm.id = ter.model_id "
               "WHERE dm.value = %s", (HAIKU,))
    assert stored == [(HAIKU,)]
    assert rolled == [(1, 1, 1)]
    assert errors == 1


def _error_rate_rows(client: TestClient, rng: str) -> list:
    body = client.get(f"/api/tool-error-rate?range={rng}").json()
    return [b for b in body["buckets"] if b["model"] == HAIKU]


def test_tool_error_rate_counts_a_later_line_call(fresh_db, mini_r2_env):
    """Both paths: the rollup (hourly-or-coarser) and the live 24h one."""
    _add_later_line_session(mini_r2_env)
    ingest.run_ingest(trigger="manual")
    client = _client()
    rolled = _error_rate_rows(client, "3650d")
    assert [(b["tool"], b["n_total"], b["n_error"]) for b in rolled] == [
        ("Bash", 1, 1)]

    with db.viz_conn() as c:
        c.execute("UPDATE tool_uses SET ts = %s "
                  "WHERE tool_use_id = 'toolu_later1'",
                  (datetime.now(timezone.utc),))
        c.commit()
    live = _error_rate_rows(client, "1d")
    assert [(b["tool"], b["n_total"], b["n_error"]) for b in live] == [
        ("Bash", 1, 1)]


def test_tool_usage_model_filter_finds_a_later_line_call(fresh_db,
                                                         mini_r2_env):
    _add_later_line_session(mini_r2_env)
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        c.execute("UPDATE tool_uses SET ts = %s "
                  "WHERE tool_use_id = 'toolu_later1'",
                  (datetime.now(timezone.utc),))
        c.commit()
    ingest.rebuild_tool_rollup()
    client = _client()
    for rng in ("30d", "1d"):  # rollup path, then live path
        body = client.get(f"/api/tool-usage?range={rng}&model=haiku").json()
        assert sum(b["n"] for b in body["buckets"]) == 1, rng


def test_purge_removes_a_suppressed_models_later_line_call(fresh_db,
                                                           mini_r2_env):
    _add_later_line_session(mini_r2_env)
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        c.execute("INSERT INTO suppressed_models (pattern, note) "
                  "VALUES ('claude-haiku-%', 'test')")
        c.commit()
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        left = _scalar(c, "SELECT COUNT(*) FROM tool_uses "
                          "WHERE tool_use_id = 'toolu_later1'")
        rolled = _scalar(c, "SELECT COUNT(*) FROM tool_rollup tr "
                            "JOIN dim_model dm ON dm.id = tr.model_id "
                            "WHERE dm.value IN (%s, '')", (HAIKU,))
    assert left == 0
    assert rolled == 0


def test_kimi_tool_call_in_a_file_with_no_record_resolves_by_date():
    """No usage record to take a model from: the call resolves the way a
    record naming no model would (parse_kimi._model_for's date ladder)."""
    blob = b"\n".join((FIX / "parser" / "kimi_code_step_model.jsonl")
                      .read_bytes().splitlines()[:3])
    out = parse.parse_file("sessions/p/s/wire.jsonl", blob)
    assert out["records"] == []
    [tu] = out["tool_uses"]
    assert tu["model"] == parse_kimi._model_for(  # pylint: disable=protected-access
        None, tu["ts"])


def _with_a_call_after_each_turn_context(blob: bytes) -> bytes:
    """The ported switch fixture holds no tool calls; give every turn one,
    right after the turn_context that sets the model in force."""
    out = []
    for i, line in enumerate(blob.splitlines()):
        out.append(line)
        if b'"type":"turn_context"' in line:
            out.append(json.dumps({
                "timestamp": json.loads(line)["timestamp"],
                "type": "response_item",
                "payload": {"type": "function_call", "name": "exec_command",
                            "call_id": f"call_switch{i}", "arguments": "{}"},
            }).encode())
    return b"\n".join(out)


def test_codex_tool_calls_follow_a_model_switch():
    """Each call carries the model its own turn is billed at, so calls on
    both sides of the switch disagree with each other and agree with the
    record that follows them."""
    blob = (FIX / "codex" / "rollout_model_switch.jsonl").read_bytes()
    out = parse.parse_file("codex/switch.jsonl",
                           _with_a_call_after_each_turn_context(blob))
    records = out["records"]
    assert {tu["model"] for tu in out["tool_uses"]} == {"gpt-5.6-sol",
                                                        "gpt-5.6-terra"}
    for tu in out["tool_uses"]:
        following = next(r for r in records
                         if r["line_num"] > tu["line_num"])
        assert tu["model"] == following["model"], tu


def test_purge_leaves_every_other_models_tool_calls(fresh_db, mini_r2_env):
    """The tool half of the purge must delete only what matches."""
    _add_later_line_session(mini_r2_env)
    ingest.run_ingest(trigger="manual")
    survivors_sql = ("SELECT COUNT(*) FROM tool_uses t "
                     "LEFT JOIN dim_model dm ON dm.id = t.model_id "
                     "WHERE COALESCE(dm.value, '') "
                     "NOT ILIKE 'claude-haiku-%'")
    with db.viz_conn() as c:
        before = _scalar(c, survivors_sql)
        c.execute("INSERT INTO suppressed_models (pattern, note) "
                  "VALUES ('claude-haiku-%', 'test')")
        c.commit()
    assert before > 0, "fixture must carry other models' calls"
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        assert _scalar(c, survivors_sql) == before


def test_purge_same_line_fallback_spares_a_call_with_its_own_model(
        fresh_db, mini_r2_env):
    """The same-line delete exists for rows stored before tool_uses.model;
    a call that carries a model is judged by that model alone."""
    _add_later_line_session(mini_r2_env)
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT f.file_key, r.line_num FROM records r "
            "JOIN files f ON f.file_id = r.file_id "
            "JOIN dim_model dm ON dm.id = r.model_id "
            "WHERE dm.value = %s", (HAIKU,)).fetchone()
        assert row is not None
        bash = dims_seed.dim_id(c, "dim_tool_name", "Bash")
        sonnet = dims_seed.dim_id(c, "dim_model", "claude-sonnet-4-5")
        c.execute(
            "INSERT INTO tool_uses (file_id, line_num, idx, tool_name_id, "
            "model_id) VALUES ((SELECT file_id FROM files WHERE file_key = "
            "%s), %s, 7, %s, %s)", (*row, bash, sonnet))
        c.execute("INSERT INTO suppressed_models (pattern, note) "
                  "VALUES ('claude-haiku-%', 'test')")
        c.commit()
    ingest.purge_suppressed()
    with db.viz_conn() as c:
        assert _scalar(c, "SELECT COUNT(*) FROM tool_uses t "
                          "JOIN files f ON f.file_id = t.file_id "
                          "WHERE f.file_key = %s "
                          "AND t.line_num = %s AND t.idx = 7", row) == 1
