# Range prompts/turns count per own timestamp (issue #214). One module
# because the feature's three pins (Claude parse, lane parse, dashboard
# totals) share one subject and this keeps the baselined test_parse.py /
# test_api.py modules within their committed size-baseline entries
# (SV-CI-RATCHETS: growth is fixed by moving code into a new module).
"""files.prompt_ts — per-prompt timestamps, in-range dashboard counting."""
import json
import os
import shutil
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, api_common, api_ctx_growth, api_dashboard, cache, db, ingest, parse, r2
from tests import dims_seed, scratch_db
from tests.test_ingest import _fresh_db_fixture  # pylint: disable=unused-import

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
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    tmp = tempfile.mkdtemp(prefix="sv-api-ptr-")
    r2root = Path(tmp) / "r2"
    monkeypatch.setenv("CLAUDIT_R2_ENDPOINT", f"file://{r2root}/")

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


def test_dashboard_counts_null_prompt_ts_as_unprovable(
        app_with_prompt_range_data):
    """A NULL timestamp, including migrated junk, counts in every range;
    a genuinely older stamped element in the same array stays out."""
    now = datetime.now(timezone.utc)
    prompt_ts = [
        None,
        now.isoformat(),                                # in range
        (now - timedelta(days=45)).isoformat(),         # provably out
    ]
    with closing(psycopg.connect(os.environ["CLAUDIT_DATABASE_URL_VIZ"])) as conn, \
            conn.cursor() as cur:
        cur.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, "
            "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
            "parser_version, prompt_count, prompt_ts) VALUES "
            "('claude/projA/sess-RJ/sess-RJ.jsonl', 'projA', 'sess-RJ', "
            "TRUE, 'junk-etag', 10, now(), now(), 'test', 3, %s::timestamptz[])",
            (prompt_ts,),
        )
        conn.commit()

    body = app_with_prompt_range_data.get(
        "/api/dashboard?range=30d&fresh=1").json()
    # RA 2 (in-range + unstamped) + RB 1 + RJ 2 (NULL + in-range; the
    # 45-day-old element is the only provably-out-of-range one).
    assert body["total_prompts"] == 5
    assert body["total_turns"] == 2  # RJ carries no context turns


_TIMELINE_NOW = datetime(2026, 6, 23, 12, tzinfo=timezone.utc)
_TIMELINE_TURNS = [
    {"idx": 1, "ts": "", "line": 2, "input": 100, "output": 5, "delta": 100},
    {"idx": 2, "ts": "2026-05-01T10:00:00+00:00", "line": 4,
     "input": 250, "output": 9, "delta": 150},
    {"idx": 3, "ts": "2026-06-23T10:00:00+00:00", "line": 6,
     "input": 80, "output": 7, "delta": -170},
]
_TIMELINE_PATHS = (
    "/api/context-growth/traces?range=30d",
    "/api/context-growth/agg?range=30d",
    "/api/context-growth/session/timeline",
    "/api/context-growth/session/empty",
    "/api/sessions/timeline",
    "/api/dashboard?range=30d&fresh=1",
)


def _timeline_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return _TIMELINE_NOW.astimezone(tz or timezone.utc)

    original_viz_conn = db.viz_conn

    @contextmanager
    def utc_conn():
        with original_viz_conn() as conn:
            conn.execute("SET TimeZone = 'UTC'")
            yield conn

    monkeypatch.setattr(db, "viz_conn", utc_conn)
    for module in (api_common, api_ctx_growth, api_dashboard):
        monkeypatch.setattr(module, "datetime", Clock)


def _seed_old_timelines(conn):
    # Install the old timeline columns even when the base DDL is typed.
    conn.execute("ALTER TABLE files DROP CONSTRAINT IF EXISTS files_ctx_cardinality")
    for column in ("ctx_ts", "ctx_line", "ctx_input", "ctx_output"):
        conn.execute(f"ALTER TABLE files DROP COLUMN IF EXISTS {column}")
    conn.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS "
                 "ctx_turns JSONB NOT NULL DEFAULT '[]'")
    conn.execute("ALTER TABLE files ALTER COLUMN prompt_ts DROP DEFAULT, "
                 "ALTER COLUMN prompt_ts TYPE JSONB USING '[]'::jsonb, "
                 "ALTER COLUMN prompt_ts SET DEFAULT '[]'::jsonb")
    dims_seed.seed_project(conn, "timeline-project")
    for sid, turns, prompts in (
            ("timeline", _TIMELINE_TURNS,
             [None, "junk", "2026-05-01T10:00:00+00:00",
              "2026-06-23T10:00:00+00:00"]),
            ("empty", [], [])):
        conn.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, "
            "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, parser_version, "
            "ctx_turns, prompt_ts, turn_count, prompt_count) "
            "VALUES (%s, 'timeline-project', %s, TRUE, 'e', 1, %s, %s, "
            "'test', %s, %s, %s, %s)",
            (f"claude/timeline-project/{sid}/{sid}.jsonl", sid,
             _TIMELINE_NOW, _TIMELINE_NOW, dims_seed.Jsonb(turns),
             dims_seed.Jsonb(prompts), len(turns), len(prompts)))
    dims_seed.seed_usage_rollup(conn, "timeline-project", "timeline",
                                _TIMELINE_NOW, model="timeline-model")
    conn.commit()


def _seed_distinguishable_timelines(conn):
    for sid, stem, main, inputs in (
            ("zulu", "zulu", False, [901, 800]),
            ("alpha", "alpha", True, [303, 707]),
            ("timeline", "z-last", True, [909, 4000])):
        turns, previous = [], 0
        for idx, value in enumerate(inputs, 1):
            turns.append({"idx": idx, "ts": _TIMELINE_NOW.isoformat(), "line": idx,
                          "input": value, "output": idx, "delta": value - previous})
            previous = value
        conn.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, "
            "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, parser_version, "
            "ctx_turns, prompt_ts, turn_count, prompt_count) "
            "VALUES (%s, 'timeline-project', %s, %s, 'e', 1, %s, %s, 'test', %s, %s, %s, 1)",
            (f"claude/timeline-project/{sid}/{stem}.jsonl", sid, main,
             _TIMELINE_NOW, _TIMELINE_NOW, dims_seed.Jsonb(turns),
             dims_seed.Jsonb([None]), len(turns)))
    mid = dims_seed.dim_id(conn, "dim_model", "synthetic-wire-model")
    fid = dims_seed.file_id(conn, "claude/timeline-project/timeline/timeline.jsonl")
    for idx in (1, 2):
        conn.execute(
            "INSERT INTO records (file_id, line_num, fallback, ts, model_id, fresh_tokens, "
            "output_tokens, eph5_tokens, eph1h_tokens, cache_read_tokens) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (fid, idx, f"parity-{idx}", _TIMELINE_NOW + timedelta(seconds=idx * 30),
             mid, 11 * idx, 13 * idx, 17 * idx, 19 * idx, 23 * idx))
        conn.execute("INSERT INTO records_price (file_id, line_num) VALUES (%s, %s)",
                     (fid, idx))
    conn.commit()


def _timeline_responses():
    app = FastAPI()
    app.include_router(api.router)
    cache.response_cache.clear()
    with TestClient(app) as client:
        responses = [client.get(path) for path in _TIMELINE_PATHS]
    assert all(r.status_code == 200 for r in responses)
    return [r.content.decode() for r in responses]


def _analyze_timeline_queries(conn):
    """Match the planner statistics used for the master response capture."""
    conn.execute("ANALYZE files, records, records_price, dim_model")


def _timeline_schema(conn):
    return conn.execute(
        "SELECT column_name, udt_name, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_name = 'files' "
        "AND table_schema = 'public' AND column_name = ANY(%s) ORDER BY column_name",
        (["ctx_ts", "ctx_line", "ctx_input", "ctx_output", "prompt_ts"],)).fetchall()


@pytest.mark.db
def test_typed_timelines_migrate_in_place(monkeypatch):
    """JSON timelines retain positions, nulls, counts and response bytes."""
    _timeline_clock(monkeypatch)
    name = scratch_db.create_database("timelines")
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
    db.reset_viz_pool()
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            fresh_shape = _timeline_schema(conn)
            _seed_old_timelines(conn)
            _seed_distinguishable_timelines(conn)
            _analyze_timeline_queries(conn)
            prompt_attnum = conn.execute(
                "SELECT attnum FROM pg_attribute WHERE attrelid = 'files'::regclass "
                "AND attname = 'prompt_ts'").fetchone()
        db.apply_schema()
        with db.viz_conn() as conn:
            _analyze_timeline_queries(conn)
            assert _timeline_schema(conn) == [
                ("ctx_input", "_int4", "NO", "'{}'::integer[]"),
                ("ctx_line", "_int4", "NO", "'{}'::integer[]"),
                ("ctx_output", "_int4", "NO", "'{}'::integer[]"),
                ("ctx_ts", "_timestamptz", "NO", "'{}'::timestamp with time zone[]"),
                ("prompt_ts", "_timestamptz", "NO", "'{}'::timestamp with time zone[]"),
            ]
            assert _timeline_schema(conn) == fresh_shape
            assert conn.execute(
                "SELECT attnum FROM pg_attribute WHERE attrelid = 'files'::regclass "
                "AND attname = 'prompt_ts'").fetchone() == prompt_attnum
            columns = scratch_db.table_columns(conn, "files")
            assert "ctx_turns" not in columns
            assert {"ctx_ts", "ctx_line", "ctx_input", "ctx_output"} <= columns
            row = conn.execute(
                "SELECT ctx_ts, ctx_line, ctx_input, ctx_output, prompt_ts "
                "FROM files WHERE file_key = 'claude/timeline-project/timeline/timeline.jsonl'").fetchone()
            assert row is not None
            assert row == (
                [None, datetime(2026, 5, 1, 10, tzinfo=timezone.utc),
                 datetime(2026, 6, 23, 10, tzinfo=timezone.utc)],
                [2, 4, 6], [100, 250, 80], [5, 9, 7],
                [None, None, datetime(2026, 5, 1, 10, tzinfo=timezone.utc),
                 datetime(2026, 6, 23, 10, tzinfo=timezone.utc)])
            assert conn.execute(
                "SELECT ctx_ts, ctx_line, ctx_input, ctx_output, prompt_ts "
                "FROM files WHERE session_id = 'empty'").fetchone() == ([], [], [], [], [])
            for column in ("ctx_line", "ctx_input", "ctx_output"):
                with pytest.raises(psycopg.errors.CheckViolation):
                    with conn.transaction():
                        conn.execute(f"UPDATE files SET {column} = '{{}}' "
                                     "WHERE session_id = 'timeline'")
            attnums = conn.execute(
                "SELECT attnum FROM pg_attribute WHERE attrelid = 'files'::regclass "
                "ORDER BY attnum").fetchall()
            conn.execute("DELETE FROM schema_stamp")
            conn.commit()
        responses = _timeline_responses()
        assert responses == _LEGACY_TIMELINE_RESPONSES, {
            path: (old, new) for path, old, new in
            zip(_TIMELINE_PATHS, _LEGACY_TIMELINE_RESPONSES, responses, strict=True)
            if old != new}
        db.apply_schema()
        with db.viz_conn() as conn:
            _analyze_timeline_queries(conn)
            assert _timeline_schema(conn) == fresh_shape
            assert conn.execute(
                "SELECT attnum FROM pg_attribute WHERE attrelid = 'files'::regclass "
                "ORDER BY attnum").fetchall() == attnums
        assert _timeline_responses() == _LEGACY_TIMELINE_RESPONSES
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)


@pytest.mark.parametrize("field", ["idx", "delta"])
def test_persist_refuses_non_derivable_ctx_turns(fresh_db, field):
    """Dropping a non-derived idx/delta must not silently change a trace."""
    turns = [dict(t) for t in _TIMELINE_TURNS]
    turns[1][field] += 1
    parsed = {"ctx_turns": turns, "turn_count": 3, "prompt_count": 0,
              "records": [], "tool_uses": []}
    obj = r2.R2Object("claude/timeline-project/timeline/timeline.jsonl",
                      "e", 1, _TIMELINE_NOW)
    proj = {"project_id": "timeline-project", "display_name": "timeline-project",
            "first_seen_at": _TIMELINE_NOW, "last_seen_at": _TIMELINE_NOW}
    with pytest.raises(ValueError, match="ctx turn.*idx/delta"):
        ingest._persist(obj, proj, parsed, "test")  # pylint: disable=protected-access
    with db.viz_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone() == (0,)


# Response bytes captured from the JSONB readers after analyzing the seed tables.
_LEGACY_TIMELINE_RESPONSES = [
    (
        '{"traces":[{"model":"synthetic-wire-model","turns":[100,250,80]},{"model":"","turns":[901,800]},{"mo'
        'del":"","turns":[303,707]},{"model":"","turns":[909,4000]}]}'
    ),
    (
        '{"range":"30d","project":null,"per_turn":{"n":9,"mean":894,"p50":707,"p90":1527,"p99":3752,"max":400'
        '0},"per_session_final":{"n":3,"mean":1595,"p50":707,"p90":3341,"p99":3934,"max":4000}}'
    ),
    (
        '{"session_id":"timeline","file_key":"timeline-project/timeline/timeline.jsonl","turns":[{"ts":"","id'
        'x":1,"line":2,"delta":100,"input":100,"output":5},{"ts":"2026-05-01T10:00:00+00:00","idx":2,"line":4'
        ',"delta":150,"input":250,"output":9},{"ts":"2026-06-23T10:00:00+00:00","idx":3,"line":6,"delta":-170'
        ',"input":80,"output":7}],"total_turns":3,"final_ctx":80}'
    ),
    (
        '{"session_id":"empty","file_key":"timeline-project/empty/empty.jsonl","turns":[],"total_turns":0,"fi'
        'nal_ctx":0}'
    ),
    (
        '{"session_id":"timeline","project_id":"timeline-project","first_event_at":"2026-06-23T12:00:30+00:00'
        '","last_event_at":"2026-06-23T12:01:00+00:00","duration_s":30,"request_count":2,"input_tokens":33,"o'
        'utput_tokens":39,"cache_create_5m_tokens":51,"cache_create_1h_tokens":57,"cache_read_tokens":69,"cos'
        't_usd":0.0,"models":{"synthetic-wire-model":2},"limit_hits":0,"r2_key":"timeline-project/timeline/ti'
        'meline.jsonl","ctx_trace":[{"t":1777629600,"ctx":250},{"t":1782208800,"ctx":80}],"burn":{"tps":4.7,"'
        'model":"synthetic-wire-model","hit_5h_limit":false}}'
    ),
    (
        '{"range":"30d","project":null,"bucket_s":21600,"hourly":[{"hour":"2026-06-23T15:00:00+00:00","model"'
        ':"timeline-model","long_context":false,"provider":null,"cost_usd":0.0,"requests":1,"session_count":1'
        ',"lines_added":0,"lines_deleted":0}],"token_types":[],"cost_by_model":[],"cost_by_model_provider":[]'
        ',"cost_by_project":[],"tokens_by_project":[],"rate_limit_hits":[],"hide_rate_limits":false,"sessions'
        '":[{"session_id":"timeline","start_ts":1782216000.0,"end_ts":1782216000.0,"requests":1,"input_tokens'
        '":0,"output_tokens":0,"cache_create_tokens":0,"cache_read_tokens":0,"cost_usd":0.0,"model":"timeline'
        '-model","models_used":["timeline-model"],"ctx_at_end":4000}],"total_sessions":1,"main_w_usage":1,"ma'
        'in_empty":3,"subagent_files":1,"subagent_only_sessions":0,"total_prompts":6,"total_turns":8,"respons'
        'e_sizes":[]}'
    ),
]
