import inspect
import json
import lzma
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

from backend import api, cache, constants, db, ingest
from backend.api_dashboard import dashboard

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIX_ROOT = _REPO_ROOT / "fixtures"

# One of the five jsonl keys in fixtures/r2_mini, used as the object whose
# fetch is made to fail. Stored file keys are bucket-qualified.
_FLAKY_KEY = "claude/projA/sess-A/sess-A.jsonl"


def _scalar(cur, sql: str, params=None):
    """First column of the first row; the query must yield one."""
    row = (cur.execute(sql, params) if params is not None
           else cur.execute(sql)).fetchone()
    assert row is not None, f"expected a row: {sql[:80]}"
    return row[0]


@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    """Per-test schema reset on a separate DB."""
    test_db = "claudit_test"
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")
    os.system(f"createdb {test_db} 2>/dev/null")
    os.system(f"psql {test_db} -f {_REPO_ROOT / 'backend/schema.sql'} >/dev/null")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    db.reset_viz_pool()
    yield
    db.reset_viz_pool()
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")


@pytest.fixture(name="mini_r2_env")
def _mini_r2_env_fixture(monkeypatch):
    src = _REPO_ROOT / "fixtures/r2_mini"
    tmp = tempfile.mkdtemp(prefix="sv-ingest-")
    shutil.copytree(src, Path(tmp) / "r2")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")
    yield Path(tmp) / "r2" / "claude"
    shutil.rmtree(tmp)


def test_ingest_inserts_one_row_per_jsonl(fresh_db, mini_r2_env):
    """Mini mirror has 5 jsonls (4 main + 1 peer) under 4 sessions
    in 2 projects. Expect 5 rows in `files`, 4 with is_main=true,
    4 distinct session_ids, 2 projects."""
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["inserted"] == 5
    with db.viz_conn() as c:
        n = _scalar(c, "SELECT COUNT(*) FROM files")
        assert n == 5
        n_main = _scalar(c, "SELECT COUNT(*) FROM files WHERE is_main")
        assert n_main == 4
        n_sess = _scalar(c, "SELECT COUNT(DISTINCT session_id) FROM files")
        assert n_sess == 4
        n_proj = _scalar(c, "SELECT COUNT(*) FROM projects")
        assert n_proj == 2


def test_records_populated_with_no_write_time_dedup(fresh_db, mini_r2_env):
    """sess-C main + sess-C agent + sess-D main all have uuid='shared-uuid-1'.
    The new ingest writes per-file with NO cross-file dedup at write time
    — so records has ALL three rows. Query-time DISTINCT ON is the dedup."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        n = _scalar(c, "SELECT COUNT(*) FROM records")
        assert n > 0
        # All three files' rows for 'shared-uuid-1' kept verbatim
        cnt = _scalar(c, "SELECT COUNT(*) FROM records WHERE uuid = 'shared-uuid-1'")
        assert cnt == 3
        # Query-time dedup gives 1
        cnt_distinct = _scalar(c, "SELECT COUNT(DISTINCT uuid) FROM records WHERE uuid = 'shared-uuid-1'")
        assert cnt_distinct == 1


def test_ctx_turns_stored_per_file(fresh_db, mini_r2_env):
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT file_key, turn_count, jsonb_array_length(ctx_turns) FROM files"
        ).fetchall()
    for fk, tc, jlen in rows:
        assert tc == jlen, f"{fk}: turn_count={tc} but ctx_turns has {jlen}"


def test_etag_change_triggers_per_file_reparse(fresh_db, mini_r2_env):
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        before_etag = _scalar(c, "SELECT r2_etag FROM files WHERE file_key LIKE '%sess-A.jsonl'")
    target = mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl"
    target.write_text(target.read_text() + "\n")
    result = ingest.run_ingest(trigger="manual")
    assert result["reparsed"] == 1
    with db.viz_conn() as c:
        after_etag = _scalar(c, "SELECT r2_etag FROM files WHERE file_key LIKE '%sess-A.jsonl'")
    assert before_etag != after_etag


def test_parser_version_bump_reparses_all(fresh_db, mini_r2_env, monkeypatch):
    ingest.run_ingest(trigger="manual")
    monkeypatch.setattr(constants, "PARSER_VERSION", "2")
    result = ingest.run_ingest(trigger="manual")
    assert result["reparsed"] == 5  # all 5 files


def test_deleted_file_removed(fresh_db, mini_r2_env):
    ingest.run_ingest(trigger="manual")
    target = mini_r2_env / "projA" / "sess-B" / "sess-B.jsonl"
    target.unlink()
    result = ingest.run_ingest(trigger="manual")
    assert result["deleted"] == 1
    with db.viz_conn() as c:
        n = _scalar(c, "SELECT COUNT(*) FROM files WHERE file_key LIKE '%sess-B.jsonl'")
        assert n == 0


def test_records_cascade_on_file_delete(fresh_db, mini_r2_env):
    ingest.run_ingest(trigger="manual")
    target = mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl"
    target.unlink()
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        n = _scalar(c, "SELECT COUNT(*) FROM records WHERE file_key LIKE '%sess-A.jsonl'")
        assert n == 0


def test_no_changes_second_run_is_zero_reparse(fresh_db, mini_r2_env):
    ingest.run_ingest(trigger="manual")
    result2 = ingest.run_ingest(trigger="manual")
    assert result2["inserted"] == 0
    assert result2["reparsed"] == 0


def test_first_seen_at_uses_least(fresh_db, mini_r2_env):
    """projects.first_seen_at must NOT be locked at first-ingest mtime.
    Add a NEW file under an existing project with an earlier mtime;
    re-ingest must drag first_seen_at backward via LEAST(...) in ON CONFLICT."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        before = _scalar(c, "SELECT first_seen_at FROM projects WHERE project_id = 'projA'")

    new_dir = mini_r2_env / "projA" / "sess-NEW"
    new_dir.mkdir()
    new_file = new_dir / "sess-NEW.jsonl"
    new_file.write_text(
        '{"type":"assistant","timestamp":"2026-05-07T09:00:00Z",'
        '"uuid":"u-new","requestId":"req-new","sessionId":"sess-NEW",'
        '"message":{"role":"assistant","model":"claude-sonnet-4-5",'
        '"content":[{"type":"text","text":"x"}],'
        '"usage":{"input_tokens":1,"output_tokens":1,'
        '"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
    )
    older_ts = before.timestamp() - 3600
    os.utime(new_file, (older_ts, older_ts))

    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        after = _scalar(c, "SELECT first_seen_at FROM projects WHERE project_id = 'projA'")
    assert after < before, f"first_seen_at should move backward: was {before}, now {after}"


def test_xz_compressed_jsonl_ingests_transparently(fresh_db, mini_r2_env):
    """A `*.jsonl.xz` object ingests like its plain form: r2.get_object
    inflates it, the `.jsonl.xz` suffix is stripped for the stem so is_main
    still holds, and records populate. Here sess-A's main file is replaced
    by an xz copy — still 5 files, still 4 main, with records for sess-A."""
    plain = mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl"
    raw = plain.read_bytes()
    (plain.parent / "sess-A.jsonl.xz").write_bytes(lzma.compress(raw))
    plain.unlink()

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["inserted"] == 5
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT file_key, is_main, session_id FROM files "
            "WHERE file_key LIKE '%sess-A.jsonl.xz'"
        ).fetchone()
        assert row is not None, "compressed file should ingest"
        assert row[0].endswith("sess-A/sess-A.jsonl.xz")
        assert row[1] is True, "stem after stripping .jsonl.xz == sess-A → is_main"
        assert row[2] == "sess-A"
        n_main = _scalar(c, "SELECT COUNT(*) FROM files WHERE is_main")
        assert n_main == 4
        n_rec = _scalar(c, "SELECT COUNT(*) FROM records WHERE file_key LIKE '%sess-A.jsonl.xz'")
        assert n_rec > 0, "records populate from decompressed bytes"


def test_is_canonical_matches_read_time_distinct_on(fresh_db, mini_r2_env):
    """The ingest-time flag must select exactly the rows the old read-time
    `DISTINCT ON (uuid) ORDER BY uuid, file_key` would have kept.

    This is the invariant that lets the read endpoints filter a boolean
    instead of re-sorting the whole table (SV-CANONICAL-FLAG). The mini
    mirror carries a cross-session shared uuid, so there is a real
    duplicate to resolve.
    """
    ingest.run_ingest(trigger="manual")

    with db.viz_conn() as c:
        flagged = c.execute(
            "SELECT file_key, line_num FROM records "
            "WHERE is_canonical ORDER BY file_key, line_num"
        ).fetchall()
        # What the read endpoints used to compute on every request.
        expected = c.execute(
            """
            SELECT file_key, line_num FROM (
              (SELECT DISTINCT ON (uuid) file_key, line_num
                 FROM records WHERE uuid IS NOT NULL
                ORDER BY uuid, file_key, line_num)
              UNION ALL
              (SELECT file_key, line_num FROM records WHERE uuid IS NULL)
            ) t ORDER BY file_key, line_num
            """
        ).fetchall()
        dupes = c.execute(
            "SELECT COUNT(*) FROM records WHERE NOT is_canonical"
        ).fetchone()

    assert flagged == expected
    assert dupes is not None and dupes[0] > 0, (
        "fixture must contain a cross-file duplicate, or this proves nothing"
    )


def test_recompute_canonical_is_idempotent(fresh_db, mini_r2_env):
    """A steady-state pass must not rewrite rows — it runs after every
    ingest, including no-op ones."""
    ingest.run_ingest(trigger="manual")
    assert ingest.recompute_canonical() == 0


def test_warm_common_covers_every_warmed_range(fresh_db, mini_r2_env, monkeypatch):
    """Every endpoint warm_common touches must be warmed for EVERY range
    it claims to cover — a warm keyed on something the UI never requests
    is dead work and leaves the real key cold.

    Regression: /api/projects gained a `range` parameter, but warm_common
    still called cache.warm(api.list_projects) bare. cache.warm falls back
    to the endpoint's signature default ("30d") while the UI opens on
    "all", so the one request every page load makes was never warmed.
    """
    monkeypatch.setenv("CLAUDIT_WARM_CACHE", "1")
    ingest.run_ingest(trigger="manual")

    warmed = (
        dashboard, api.activity_heatmap, api.tool_usage,
        api.tool_error_rate, api.reply_latency, api.list_projects,
    )
    # The warms run on a background pool; give them a bounded moment.
    deadline = time.time() + 60
    missing = None
    while time.time() < deadline:
        missing = [
            f"{fn.__qualname__}(range={rng})"
            for rng in ingest.WARM_RANGES
            for fn in warmed
            if cache.response_cache.get(_warm_key(fn, rng)) is None
        ]
        if not missing:
            break
        time.sleep(0.25)

    assert not missing, "warm_common left these uncached: " + ", ".join(missing)


def _warm_key(fn, rng: str) -> str:
    """Reproduce cache_response's key for a request at `rng`.

    Built from the endpoint's own signature so it stays correct as params
    are added — which is exactly what broke /api/projects.
    """
    target = getattr(fn, "__wrapped__", fn)
    kwargs = {}
    for name, param in inspect.signature(target).parameters.items():
        default = param.default
        kwargs[name] = getattr(default, "default", default)
    kwargs["rng"] = rng
    if "fresh" in kwargs:
        kwargs["fresh"] = 0
    return target.__qualname__ + ":" + repr(sorted(kwargs.items()))


def test_ingest_marks_response_cache_stale(fresh_db, mini_r2_env):
    """Ingest marks cached responses stale but leaves them SERVABLE.

    It used to clear() the cache, which dropped every reader onto the
    uncached path once an hour — 8s+ for /api/dashboard at range=all.
    Stale-while-revalidate keeps the previous numbers available while the
    refresh runs off the request path.
    """
    cache.response_cache.put("stale-key", {"v": "old"})
    entry = cache.response_cache.get_entry("stale-key")
    assert entry == ({"v": "old"}, False), "fresh before ingest"

    ingest.run_ingest(trigger="manual")

    entry = cache.response_cache.get_entry("stale-key")
    assert entry is not None, "ingest must NOT drop the entry"
    value, is_stale = entry
    assert value == {"v": "old"}, "previous response still servable"
    assert is_stale is True, "and flagged for background refresh"


def _snapshot():
    """Full ingest output, ordered so it is comparable across runs."""
    with db.viz_conn() as c:
        files = c.execute(
            "SELECT file_key, project_id, session_id, is_main, r2_etag, "
            "turn_count, prompt_count, parser_version FROM files "
            "ORDER BY file_key"
        ).fetchall()
        records = c.execute(
            "SELECT file_key, line_num, uuid, request_id, model, fresh_tokens, "
            "cache_creation_tokens, cache_read_tokens, output_tokens, "
            "eph5_tokens, eph1h_tokens, cost_usd FROM records "
            "ORDER BY file_key, line_num"
        ).fetchall()
        tools = c.execute(
            "SELECT file_key, line_num, idx, tool_name, is_error FROM tool_uses "
            "ORDER BY file_key, line_num, idx"
        ).fetchall()
        projects = c.execute(
            "SELECT project_id, first_seen_at, last_seen_at FROM projects "
            "ORDER BY project_id"
        ).fetchall()
    return files, records, tools, projects


def test_parallel_ingest_matches_sequential_exactly(
    fresh_db, mini_r2_env, monkeypatch
):
    """Concurrency must not change what lands in the DB.

    Fetch+parse is parallelised; if that leaked into ordering, dedup, or
    the per-file transaction boundary, the two snapshots would diverge.
    """
    monkeypatch.setenv("INGEST_WORKERS", "1")
    ingest.run_ingest("test-seq")
    sequential = _snapshot()

    # Wipe and re-ingest the identical mirror with a pool.
    with db.viz_conn() as c:
        c.execute("DELETE FROM files")
        c.execute("DELETE FROM projects")
        c.commit()

    monkeypatch.setenv("INGEST_WORKERS", "8")
    ingest.run_ingest("test-par")
    parallel = _snapshot()

    assert parallel[0] == sequential[0], "files differ"
    assert parallel[1] == sequential[1], "records differ"
    assert parallel[2] == sequential[2], "tool_uses differ"
    assert parallel[3] == sequential[3], "projects differ"
    assert len(sequential[1]) > 0, "fixture produced no records — vacuous test"


def test_ingest_workers_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("INGEST_WORKERS", raising=False)
    assert ingest.worker_count() >= 1
    monkeypatch.setenv("INGEST_WORKERS", "0")
    assert ingest.worker_count() == 1
    monkeypatch.setenv("INGEST_WORKERS", "not-a-number")
    assert ingest.worker_count() >= 1


def test_a_second_ingest_is_skipped_while_one_is_running(fresh_db, mini_r2_env):
    """The hourly cron fires regardless of whether a run is still going. A
    PARSER_VERSION bump makes a run take ~40min, so a cron landed on top of a
    startup reparse and both walked the whole bucket — duplicate GETs,
    duplicate parses, two sets of rollup rebuilds, and a progress readout that
    went backwards. A concurrent run must decline, not pile on."""
    started = threading.Event()
    release = threading.Event()
    seen = {}

    real = ingest.run_ingest_locked

    def slow(trigger):
        started.set()
        release.wait(timeout=30)
        return real(trigger)

    ingest.run_ingest_locked = slow
    try:
        t = threading.Thread(target=lambda: seen.update(first=ingest.run_ingest("startup")))
        t.start()
        assert started.wait(timeout=10), "first run never entered"
        # Second run arrives while the first still holds the lock.
        second = ingest.run_ingest(trigger="cron")
        release.set()
        t.join(timeout=60)
    finally:
        ingest.run_ingest_locked = real

    assert second.get("skipped") is True, second
    assert "already running" in second.get("reason", "")
    assert seen["first"].get("skipped") is not True, "the first run must NOT be skipped"


def test_ingest_runs_again_once_the_lock_is_free(fresh_db, mini_r2_env):
    """The skip is per-overlap, not sticky — the lock must be released."""
    ingest.run_ingest(trigger="manual")
    again = ingest.run_ingest(trigger="manual")
    assert again.get("skipped") is not True, again


def _suppress(pattern: str) -> None:
    """Add one suppression pattern, the way an operator would."""
    with db.viz_conn() as c:
        c.execute("INSERT INTO suppressed_models (pattern, note) "
                  "VALUES (%s, 'test')", (pattern,))
        c.commit()


def test_suppressed_model_records_are_purged(fresh_db, mini_r2_env):
    """A pattern added AFTER the rows landed takes effect on the next
    ingest — no reparse, no PARSER_VERSION bump.

    This is the leak it exists for: a session resumed on the other lane
    interleaves that provider's assistant entries into a transcript this
    bucket already owns, and pricing them against our table invents a cost.
    """
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        before = _scalar(
            c, "SELECT COUNT(*) FROM records WHERE model = 'claude-opus-4-7'")
        others = _scalar(
            c, "SELECT COUNT(*) FROM records WHERE model <> 'claude-opus-4-7'")
    assert before > 0 and others > 0, (
        "fixture must carry both, or this proves nothing")

    _suppress("claude-opus-%")
    ingest.run_ingest(trigger="manual")

    with db.viz_conn() as c:
        assert _scalar(
            c,
            "SELECT COUNT(*) FROM records WHERE model = 'claude-opus-4-7'") == 0
        assert _scalar(
            c, "SELECT COUNT(*) FROM records WHERE model <> 'claude-opus-4-7'"
        ) == others
        # The rollups read `records`, so they inherit the purge.
        assert _scalar(
            c, "SELECT COUNT(*) FROM usage_rollup "
               "WHERE model = 'claude-opus-4-7'") == 0


def test_suppression_matches_a_bare_model_id_exactly(fresh_db, mini_r2_env):
    """A pattern with no wildcard is still just an ILIKE — it must match
    that one model and leave its siblings alone."""
    ingest.run_ingest(trigger="manual")
    _suppress("claude-opus-4-7")
    ingest.run_ingest(trigger="manual")

    with db.viz_conn() as c:
        assert _scalar(
            c,
            "SELECT COUNT(*) FROM records WHERE model = 'claude-opus-4-7'") == 0
        assert _scalar(
            c, "SELECT COUNT(*) FROM records "
               "WHERE model = 'claude-sonnet-4-5'") > 0


def test_suppressed_tool_uses_go_with_their_records(fresh_db, mini_r2_env):
    """tool_rollup LEFT JOINs records for the model, so a tool call left
    behind by a purged record would resurface as an unlabelled model."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT file_key, line_num FROM records "
            "WHERE model = 'claude-opus-4-7' ORDER BY file_key, line_num LIMIT 1"
        ).fetchone()
        assert row is not None
        c.execute(
            "INSERT INTO tool_uses (file_key, line_num, idx, tool_name) "
            "VALUES (%s, %s, 0, 'Bash')", (row[0], row[1]))
        c.commit()

    _suppress("claude-opus-%")
    ingest.run_ingest(trigger="manual")

    with db.viz_conn() as c:
        assert _scalar(
            c, "SELECT COUNT(*) FROM tool_uses WHERE file_key = %s "
               "AND line_num = %s", (row[0], row[1])) == 0


def test_purge_suppressed_is_a_noop_with_an_empty_table(fresh_db, mini_r2_env):
    """It runs on every ingest, so the empty case is the common one —
    and this codebase also ships to a deploy that suppresses nothing."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        before = _scalar(c, "SELECT COUNT(*) FROM records")
    assert ingest.purge_suppressed() == 0
    with db.viz_conn() as c:
        assert _scalar(c, "SELECT COUNT(*) FROM records") == before


def test_purge_suppressed_is_idempotent(fresh_db, mini_r2_env):
    """Nothing left to delete on the second pass — it runs every ingest."""
    ingest.run_ingest(trigger="manual")
    _suppress("claude-opus-%")
    assert ingest.purge_suppressed() > 0
    assert ingest.purge_suppressed() == 0


def test_parser_version_ignores_the_environment(fresh_db, mini_r2_env,
                                                monkeypatch):
    """PARSER_VERSION is code-owned: setting the old env var must NOT
    trigger a reparse.

    It used to be read from .env, so a parser change shipped without an
    operator editing that file left every stored row on the previous
    semantics with nothing to detect the drift.
    """
    ingest.run_ingest(trigger="manual")
    monkeypatch.setenv("PARSER_VERSION", "999")
    result = ingest.run_ingest(trigger="manual")
    assert result["reparsed"] == 0

    with db.viz_conn() as c:
        stored = {r[0] for r in c.execute(
            "SELECT DISTINCT parser_version FROM files").fetchall()}
    assert stored == {constants.PARSER_VERSION}


def _plant(mirror, project, session, fixture):
    """Copy a parser fixture into the mini mirror as its own session."""
    dest = mirror / project / session
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"{session}.jsonl"
    target.write_bytes(
        (_FIX_ROOT / "parser" / fixture).read_bytes()
    )
    return target


def test_tool_failure_columns_persist(fresh_db, mini_r2_env):
    """error_kind/error_text survive the ingest INSERT, and only
    errored rows carry them."""
    _plant(mini_r2_env, "projK", "sess-K", "error_kinds.jsonl")
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT idx, is_error, error_kind, error_text FROM tool_uses "
            "WHERE file_key LIKE '%sess-K.jsonl' ORDER BY idx"
        ).fetchall()
    assert [r[2] for r in rows] == ["failed", "rejected", "tool_error", None]
    assert "PreToolUse hook" in rows[0][3]
    assert rows[3][1] is False and rows[3][3] is None


def test_agent_dispatch_columns_persist(fresh_db, mini_r2_env):
    """agent_type/agent_model survive the ingest INSERT and stay NULL
    for tools that dispatch nothing."""
    _plant(mini_r2_env, "projD", "sess-D", "agent_dispatch.jsonl")
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT idx, tool_name, agent_type, agent_model FROM tool_uses "
            "WHERE file_key LIKE '%sess-D.jsonl' ORDER BY idx"
        ).fetchall()
    assert rows[0][1:] == ("Agent", "Explore", "haiku")
    assert rows[1][2] is None and rows[1][3] is None
    assert rows[2][1] == "Bash" and rows[2][2] is None


def test_tool_error_rollup_totals_match_raw(fresh_db, mini_r2_env):
    """SV-ROLLUP: the pre-aggregate must equal the raw aggregate it
    stands in for, per kind."""
    _plant(mini_r2_env, "projK", "sess-K", "error_kinds.jsonl")
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rolled = dict(c.execute(
            "SELECT error_kind, SUM(n) FROM tool_error_rollup GROUP BY 1"
        ).fetchall())
        raw = dict(c.execute(
            "SELECT error_kind, COUNT(*) FROM tool_uses "
            "WHERE error_kind IS NOT NULL AND ts IS NOT NULL GROUP BY 1"
        ).fetchall())
    assert rolled == raw
    assert raw == {"failed": 1, "rejected": 1, "tool_error": 1}


def test_dispatch_rollup_totals_match_raw(fresh_db, mini_r2_env):
    """Every dispatch is counted, including one that named no model."""
    _plant(mini_r2_env, "projD", "sess-D", "agent_dispatch.jsonl")
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rolled = c.execute(
            "SELECT agent_type, agent_model, SUM(n) FROM dispatch_rollup "
            "GROUP BY 1, 2 ORDER BY 1"
        ).fetchall()
        raw_total = _scalar(c, "SELECT COUNT(*) FROM tool_uses "
                               "WHERE agent_type IS NOT NULL")
    assert rolled == [("Explore", "haiku", 1)]
    assert sum(r[2] for r in rolled) == raw_total


def test_dispatch_brief_columns_persist(fresh_db, mini_r2_env):
    """The briefing-shape columns survive the ingest INSERT, and stay
    NULL for a dispatch with no prompt and for non-dispatch tools."""
    _plant(mini_r2_env, "projB", "sess-B", "dispatch_brief_shape.jsonl")
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT idx, tool_name, dispatch_brief_ref, "
            "dispatch_prompt_chars FROM tool_uses "
            "WHERE file_key LIKE '%sess-B.jsonl' ORDER BY idx"
        ).fetchall()
    assert rows[0][2] is True
    assert rows[1][2] is False and rows[1][3] == 188
    assert rows[2][2] is False, "a path past the scan window is not a ref"
    assert rows[3][2] is None and rows[3][3] is None
    assert rows[4][1] == "Bash" and rows[4][2] is None


def test_dispatch_brief_rollup_totals_match_raw(fresh_db, mini_r2_env):
    """SV-ROLLUP: the pre-aggregate equals the raw aggregate it stands
    in for, on both the count and the summed prompt length."""
    _plant(mini_r2_env, "projB", "sess-B", "dispatch_brief_shape.jsonl")
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rolled = c.execute(
            "SELECT brief_ref, SUM(n), SUM(prompt_chars) "
            "FROM dispatch_brief_rollup GROUP BY 1 ORDER BY 1"
        ).fetchall()
        raw = c.execute(
            "SELECT dispatch_brief_ref, COUNT(*), "
            "COALESCE(SUM(dispatch_prompt_chars), 0) FROM tool_uses "
            "WHERE dispatch_brief_ref IS NOT NULL AND ts IS NOT NULL "
            "GROUP BY 1 ORDER BY 1"
        ).fetchall()
    assert rolled == raw
    # Two inline dispatches, one that delegates to a written brief.
    assert dict((r[0], r[1]) for r in rolled) == {False: 2, True: 1}


def test_rollups_rebuilt_when_nothing_changed(fresh_db, mini_r2_env):
    """Derived state is rebuilt on every successful ingest, not only
    when files changed — same contract as recompute_canonical()."""
    _plant(mini_r2_env, "projK", "sess-K", "error_kinds.jsonl")
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        c.execute("DELETE FROM tool_error_rollup")
        c.commit()
    result = ingest.run_ingest(trigger="manual")
    assert result["reparsed"] == 0
    with db.viz_conn() as c:
        assert _scalar(c, "SELECT COUNT(*) FROM tool_error_rollup") > 0


def test_lane_layout_ingests_with_marker_display_name(
        fresh_db, tmp_path, monkeypatch):
    """A lane bucket's sessions/ tree: wire.jsonl[.xz] under
    sessions/<project>/<session>/, a project.json marker carrying the
    project's directory, and a subagent wire under subagents/. Main and
    sidecar land under the SAME project, keyed by the Claude slug of the
    marker's path — the id a Claude-layout bucket derives for the same
    directory — the path becomes projects.display_name, and is_main
    splits main from subagent."""
    proj, sess = "8805b8ac99ad", "01a0-uuid"
    slug = "-home-me-lanework"
    bucket = tmp_path / "r2" / "claude"
    lane = bucket / "sessions" / proj / sess
    (lane / "subagents" / "019f-child").mkdir(parents=True)
    (lane / "wire.jsonl.xz").write_bytes(
        lzma.compress((_FIX_ROOT / "parser" / "codex_min.jsonl").read_bytes()))
    shutil.copy(
        _FIX_ROOT / "parser" / "kimi_code_min.jsonl",
        lane / "subagents" / "019f-child" / "wire.jsonl",
    )
    (bucket / "sessions" / proj / "project.json").write_text(
        json.dumps({"path": "/home/me/lanework"}))
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["inserted"] == 2
    assert result["r2_listed"] == 2, "the marker is not a transcript"
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT file_key, project_id, session_id, is_main FROM files "
            "ORDER BY is_main DESC"
        ).fetchall()
        display = _scalar(
            c, "SELECT display_name FROM projects WHERE project_id = %s",
            (slug,))
        hash_projects = _scalar(
            c, "SELECT COUNT(*) FROM projects WHERE project_id = %s",
            (proj,))
        n_records = _scalar(c, "SELECT COUNT(*) FROM records")
    assert [(r[2], r[3]) for r in rows] == [(sess, True), (sess, False)]
    assert all(r[0].startswith(f"claude/sessions/{proj}/{sess}/") for r in rows)
    assert all(r[1] == slug for r in rows), (
        "a marker path read this run keys the project by its slug")
    assert display == "/home/me/lanework"
    assert hash_projects == 0, (
        "no project row may be keyed by the bare hash when a marker was read")
    assert n_records > 0, "both wire files parsed into records"


def test_lane_layout_without_project_marker_displays_the_project_id(
        fresh_db, tmp_path, monkeypatch):
    """Same lane tree with the sessions/<project>/project.json marker
    ABSENT: ingest still succeeds and the project's display_name falls
    back to the project id, exactly as for a malformed marker."""
    proj, sess = "8805b8ac99ad", "01a0-uuid"
    bucket = tmp_path / "r2" / "claude"
    lane = bucket / "sessions" / proj / sess
    lane.mkdir(parents=True)
    (lane / "wire.jsonl.xz").write_bytes(
        lzma.compress((_FIX_ROOT / "parser" / "codex_min.jsonl").read_bytes()))
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["inserted"] == 1
    assert result["r2_listed"] == 1
    with db.viz_conn() as c:
        display = _scalar(
            c, "SELECT display_name FROM projects WHERE project_id = %s",
            (proj,))
        n_records = _scalar(c, "SELECT COUNT(*) FROM records")
    assert display == proj, "no marker: the project id is the display name"
    assert n_records > 0, "the wire file parsed into records"


def test_transient_marker_failure_keeps_the_stored_display_name(
        fresh_db, tmp_path, monkeypatch):
    """A run that reparses a lane project's files while its project.json
    marker fetch fails (marker gone, GET error) must keep the stored
    display_name AND the slug-keyed project id. Flipping an existing
    slug-keyed project back to its hash for that run splits one project
    into two ids until the next reparse."""
    proj, sess = "8805b8ac99ad", "01a0-uuid"
    slug = "-home-me-lanework"
    bucket = tmp_path / "r2" / "claude"
    lane = bucket / "sessions" / proj / sess
    lane.mkdir(parents=True)
    (lane / "wire.jsonl.xz").write_bytes(
        lzma.compress((_FIX_ROOT / "parser" / "codex_min.jsonl").read_bytes()))
    (bucket / "sessions" / proj / "project.json").write_text(
        json.dumps({"path": "/home/me/lanework"}))
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")

    ingest.run_ingest(trigger="manual")

    # The marker vanishes and the wire's etag changes: the next run
    # reparses the file having read no marker at all.
    (bucket / "sessions" / proj / "project.json").unlink()
    (lane / "wire.jsonl.xz").touch()
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["reparsed"] == 1

    with db.viz_conn() as c:
        display = _scalar(
            c, "SELECT display_name FROM projects WHERE project_id = %s",
            (slug,))
        hash_files = _scalar(
            c, "SELECT COUNT(*) FROM files WHERE project_id = %s", (proj,))
    assert display == "/home/me/lanework", (
        "a run that read no marker must preserve the stored display name"
    )
    assert hash_files == 0, (
        "no file may flip back to the hash id: the stored hash→slug "
        "mapping keeps one directory ONE project across a marker miss"
    )
