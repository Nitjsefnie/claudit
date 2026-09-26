# Range prompts/turns count per own timestamp (issue #214). One module
# because the feature's three pins (Claude parse, lane parse, dashboard
# totals) share one subject and this keeps the baselined test_parse.py /
# test_api.py modules within their committed size-baseline entries
# (SV-CI-RATCHETS: growth is fixed by moving code into a new module).
"""files.prompt_ts — per-prompt timestamps, in-range dashboard counting."""
import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, db, ingest, parse
from tests import scratch_db

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"


def test_prompt_ts_collected_per_prompt():
    """parse_file returns prompt_ts carrying each counted prompt's own
    timestamp, index-aligned with prompt_count: an excluded line's ts is
    excluded by the same gate, so instrumentation never appears in it
    (issue #214)."""
    blob = (
        b'{"type":"user","timestamp":"2026-05-07T10:00:00Z","uuid":"u1",'
        b'"message":{"role":"user","content":"real prompt"}}\n'
        b'{"type":"user","timestamp":"2026-05-07T10:00:01Z","uuid":"u2",'
        b'"message":{"role":"user","content":"<command-name>foo</command-name>"}}\n'
        b'{"type":"user","timestamp":"2026-05-07T10:00:04Z","uuid":"u5",'
        b'"message":{"role":"user","content":"another real prompt"}}\n'
    )
    out = parse.parse_file("k/sess-pts/sess-pts.jsonl", blob)
    assert out["prompt_ts"] == [
        "2026-05-07T10:00:00+00:00",
        "2026-05-07T10:00:04+00:00",
    ]
    assert len(out["prompt_ts"]) == out["prompt_count"] == 2


@pytest.mark.parametrize("name", [
    "codex_min.jsonl", "kimi_code_min.jsonl", "kimi_legacy_min.jsonl"])
def test_lane_prompt_ts_matches_prompt_count(name):
    """The lane parsers emit prompt_ts with one entry per counted prompt
    (== the source of files.prompt_count), a real isoformat timestamp or
    None (issue #214)."""
    out = parse.parse_file(f"sessions/p/s/{name}", (FIX / name).read_bytes())
    assert len(out["prompt_ts"]) == out["prompt_count"]
    for ts in out["prompt_ts"]:
        assert ts is None or datetime.fromisoformat(ts.replace("Z", "+00:00"))


@pytest.fixture(name="app_with_prompt_range_data")
def _app_with_prompt_range_data_fixture(monkeypatch):
    """Fresh DB + a tiny R2 mirror built inline: two main files whose
    mtimes are current, one of them carrying a prompt and a turn dated
    BEFORE a 30d range's `since` plus one prompt with no timestamp.

    This reproduces issue #214: a file modified inside the range still
    carried prompts sent before it, and the range's totals used to count
    those whole-file numbers."""
    test_db = scratch_db.create_database("api_ptr")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    tmp = tempfile.mkdtemp(prefix="sv-api-ptr-")
    r2root = Path(tmp) / "r2"
    monkeypatch.setenv("R2_ENDPOINT", f"file://{r2root}/")

    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_ts = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _user(uid, ts, text):
        line = {"type": "user", "uuid": uid,
                "message": {"role": "user", "content": text}}
        if ts is not None:
            line["timestamp"] = ts
        return json.dumps(line)

    def _reply(uid, req, ts):
        return json.dumps({
            "type": "assistant", "timestamp": ts, "uuid": uid,
            "requestId": req,
            "message": {"role": "assistant", "model": "claude-sonnet-4-5",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 10, "output_tokens": 20,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}}})

    def _write(key, lines):
        path = r2root / key  # e.g. claude/projA/sess-RA/sess-RA.jsonl
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")

    # sess-RA: a prompt and a turn 45 days old (before a 30d `since`) in
    # a file modified now, a fresh prompt, and one prompt with no
    # timestamp at all (ruling P1: unstamped cannot prove out of range).
    _write("claude/projA/sess-RA/sess-RA.jsonl", [
        _user("ra-u1", old_ts, "old prompt"),
        _reply("ra-a1", "ra-r1", old_ts),
        _user("ra-u2", new_ts, "new prompt"),
        _reply("ra-a2", "ra-r2", new_ts),
        _user("ra-u3", None, "unstamped prompt"),
    ])
    # sess-RB: entirely inside the range.
    _write("claude/projA/sess-RB/sess-RB.jsonl", [
        _user("rb-u1", new_ts, "b prompt"),
        _reply("rb-a1", "rb-r1", new_ts),
    ])

    db.reset_viz_pool()

    ingest.run_ingest(trigger="manual")

    a = FastAPI()
    a.include_router(api.router)

    yield TestClient(a)

    db.reset_viz_pool()
    shutil.rmtree(tmp)
    scratch_db.drop_database(test_db)


def test_dashboard_counts_prompts_and_turns_by_own_timestamp(
        app_with_prompt_range_data):
    """Issue #214 regression: the range's prompts/turns count per own
    timestamp, not whole files' totals. sess-RA was modified now but
    carries a prompt and a turn dated 45 days ago; a 30d view must not
    count those, while its unstamped prompt still counts (ruling P1: an
    unstamped ts cannot prove out of range). Over the all-range the
    totals stay exactly the old whole-file SUMs."""
    body_30d = app_with_prompt_range_data.get(
        "/api/dashboard?range=30d&fresh=1").json()
    assert body_30d["total_prompts"] == 3  # RA: new + unstamped; RB: 1
    assert body_30d["total_turns"] == 2    # RA: 1 in-range; RB: 1

    body_all = app_with_prompt_range_data.get(
        "/api/dashboard?range=3650d&fresh=1").json()
    assert body_all["total_prompts"] == 4  # == the old SUM(prompt_count)
    assert body_all["total_turns"] == 3    # == the old SUM(turn_count)
