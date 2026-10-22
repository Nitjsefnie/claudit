import inspect
import json
import lzma
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from psycopg import sql as pg_sql
from psycopg.rows import dict_row

from backend import api, cache, db, ingest, parser_version, uuid_split
from backend.api_ctx_growth import context_growth_traces
from backend.api_dashboard import dashboard
from backend.api_web_metrics import web_metrics_readout
from backend.ingest_identity import upsert_project
from backend.ingest_persist import _persist
from tests import dims_seed, mini_mirror, scratch_db
from tests.persist_fact_fixture import persist_fact_fixture
from tests.node_run import run_node

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIX_ROOT = _REPO_ROOT / "fixtures"

# One of the jsonl keys in fixtures/r2_mini, used as the object whose
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
    yield from scratch_db.scratch_viz_database(monkeypatch, "ingest")


@pytest.fixture(name="mini_r2_env")
def _mini_r2_env_fixture(monkeypatch):
    src = _REPO_ROOT / "fixtures/r2_mini"
    tmp = tempfile.mkdtemp(prefix="sv-ingest-")
    shutil.copytree(src, Path(tmp) / "r2")
    monkeypatch.setenv("CLAUDIT_R2_ENDPOINT", f"file://{tmp}/r2/")
    yield Path(tmp) / "r2" / "claude"
    shutil.rmtree(tmp)


def test_ingest_inserts_one_row_per_jsonl(fresh_db, mini_r2_env):
    """One row per transcript the mirror holds, whatever it holds.

    The counts are the MIRROR's, read from its tree (tests/mini_mirror):
    the assertion is that the ingest agrees with the corpus it was given,
    not that the corpus still has the size it had when this test was
    written. Claude- and lane-layout transcripts alike land here — the
    mirror grew the lane wires in."""
    shape = mini_mirror.counts()
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["inserted"] == shape["transcripts"]
    with db.viz_conn() as c:
        n = _scalar(c, "SELECT COUNT(*) FROM files")
        assert n == shape["transcripts"]
        n_main = _scalar(c, "SELECT COUNT(*) FROM files WHERE is_main")
        assert n_main == shape["main"]
        n_sess = _scalar(c, "SELECT COUNT(DISTINCT session_id) FROM files")
        assert n_sess == shape["sessions"]
        n_proj = _scalar(c, "SELECT COUNT(*) FROM projects WHERE project_id <> ''")
        assert n_proj == shape["projects"]


def test_records_populated_with_no_write_time_dedup(fresh_db, mini_r2_env):
    """sess-C main + sess-C agent + sess-D main all have uuid='shared-uuid-1'.
    The new ingest writes per-file with NO cross-file dedup at write time
    — so records has ALL three rows. Query-time DISTINCT ON is the dedup."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        n = _scalar(c, "SELECT COUNT(*) FROM records")
        assert n > 0
        # All three files' rows for 'shared-uuid-1' kept verbatim
        cnt = _scalar(c, f"SELECT COUNT(*) FROM records r WHERE {uuid_split.uuid_text_sql('r')} = 'shared-uuid-1'")
        assert cnt == 3
        # Query-time dedup gives 1
        cnt_distinct = _scalar(
            c, f"SELECT COUNT(DISTINCT {uuid_split.uuid_text_sql('r')}) "
               f"FROM records r "
               f"WHERE {uuid_split.uuid_text_sql('r')} = 'shared-uuid-1'")
        assert cnt_distinct == 1


def test_ctx_turns_stored_per_file(fresh_db, mini_r2_env):
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT file_key, turn_count, cardinality(ctx_input) FROM files"
        ).fetchall()
    for fk, tc, jlen in rows:
        assert tc == jlen, f"{fk}: turn_count={tc} but ctx_input has {jlen}"


def test_nul_in_tool_text_still_stores(fresh_db, mini_r2_env):
    """A NUL in a tool name or tool argument must not abort the file's
    transaction: _persist strips the byte from every text
    value it writes, whatever column carries it, so the transcript
    stores like any other."""
    fk = "claude/projNul/sess-nul/sess-nul.jsonl"
    f = mini_r2_env / "projNul" / "sess-nul" / "sess-nul.jsonl"
    f.parent.mkdir(parents=True)
    f.write_bytes((_FIX_ROOT / "parser" / "nul_tool_use.jsonl").read_bytes())
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        assert _scalar(c, "SELECT count(*) FROM files WHERE file_key = %s",
                       (fk,)) == 1, "NUL-bearing transcript refused at persist"
        rows = c.execute(
            "SELECT dtn.value, read_targets FROM tool_uses t JOIN files f ON f.file_id = t.file_id JOIN dim_tool_name dtn ON dtn.id = t.tool_name_id WHERE f.file_key = %s ORDER BY t.idx",
            (fk,)).fetchall()
    assert [r[0] for r in rows] == ["SkillSkill", "Read"]
    assert rows[1][1] == ["/tmp/ab.md"]


def test_persist_fact_values(fresh_db, monkeypatch):
    obj, proj, parsed, expected = persist_fact_fixture(monkeypatch)
    _persist(obj, proj, parsed, parser_version.PARSER_VERSION)
    with db.viz_conn() as c, c.cursor(row_factory=dict_row) as cur:
        file_id = dims_seed.file_id(c, obj.key)
        print("numeric_text_rows:", c.execute(
            "SELECT r.line_num, reply_latency_s::text, generation_s::text, "
            "output_tokens_per_s::text, cost_usd::text, "
            "long_context_input_mult::text, long_context_output_mult::text "
            "FROM records r JOIN records_price p USING (file_id, line_num) "
            "ORDER BY r.line_num").fetchall())
        for table, rows in expected.items():
            for row in rows:
                row["file_id"] = file_id
            actual = cur.execute(
                pg_sql.SQL("SELECT * FROM {} ORDER BY line_num").format(
                    pg_sql.Identifier(table))).fetchall()
            assert actual == rows, table


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


@pytest.mark.parametrize("display_name_set", [True, False])
def test_project_merge_skips_unchanged_row(fresh_db, display_name_set):
    ts = datetime(2026, 5, 7, tzinfo=timezone.utc)
    proj = {"project_id": "merge-project", "display_name": "Named Project",
            "display_name_set": display_name_set,
            "first_seen_at": ts, "last_seen_at": ts}
    with db.viz_conn() as c:
        upsert_project(c, proj)
        c.commit()
        before = _scalar(c, "SELECT xmin FROM projects WHERE project_id = %s",
                         (proj["project_id"],))
        upsert_project(c, proj)
        c.commit()
        assert _scalar(c, "SELECT xmin FROM projects WHERE project_id = %s",
                       (proj["project_id"],)) == before
        upsert_project(c, {**proj, "display_name_set": False,
                           "display_name": proj["project_id"]})
        c.commit()
        assert _scalar(c, "SELECT xmin FROM projects WHERE project_id = %s",
                       (proj["project_id"],)) == before


@pytest.mark.parametrize("stored_display, changes, expected", [
    ("merge-project", {"first_seen_at": datetime(2026, 5, 6, tzinfo=timezone.utc)},
     ("merge-project", 6, 7)),
    ("merge-project", {"last_seen_at": datetime(2026, 5, 8, tzinfo=timezone.utc)},
     ("merge-project", 7, 8)),
    ("merge-project", {"display_name": "Merge-Project"}, ("Merge-Project", 7, 7)),
    ("Named Project", {"display_name": "/marker/path", "display_name_set": True},
     ("/marker/path", 7, 7)),
])
def test_project_merge_applies_changed_values(fresh_db, stored_display, changes, expected):
    ts = datetime(2026, 5, 7, tzinfo=timezone.utc)
    proj = {"project_id": "merge-project", "display_name": stored_display,
            "display_name_set": False, "first_seen_at": ts, "last_seen_at": ts}
    with db.viz_conn() as c:
        upsert_project(c, proj)
        c.commit()
        upsert_project(c, {**proj, **changes})
        c.commit()
        row = c.execute(
            "SELECT display_name, first_seen_at, last_seen_at, xmin "
            "FROM projects WHERE project_id = %s", (proj["project_id"],)
        ).fetchone()
        assert row is not None
        assert row[:3] == (
            expected[0], datetime(2026, 5, expected[1], tzinfo=timezone.utc),
            datetime(2026, 5, expected[2], tzinfo=timezone.utc))
        upsert_project(c, {**proj, **changes})
        c.commit()
        assert _scalar(c, "SELECT xmin FROM projects WHERE project_id = %s",
                       (proj["project_id"],)) == row[3]


def test_xz_compressed_jsonl_ingests_transparently(fresh_db, mini_r2_env):
    """A `*.jsonl.xz` object ingests like its plain form: r2.get_object
    inflates it, the `.jsonl.xz` suffix is stripped for the stem so is_main
    still holds, and records populate. Here sess-A's main file is replaced
    by an xz copy — the same file count, the same main count, with records
    for sess-A."""
    plain = mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl"
    raw = plain.read_bytes()
    (plain.parent / "sess-A.jsonl.xz").write_bytes(lzma.compress(raw))
    plain.unlink()

    shape = mini_mirror.counts()
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["inserted"] == shape["transcripts"]
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
        assert n_main == shape["main"]
        n_rec = _scalar(c, "SELECT COUNT(*) FROM records r JOIN files f ON f.file_id = r.file_id WHERE f.file_key LIKE '%sess-A.jsonl.xz'")
        assert n_rec > 0, "records populate from decompressed bytes"


def test_is_canonical_matches_read_time_distinct_on(fresh_db, mini_r2_env):
    """The ingest-time flag must select exactly the rows the read-time
    `DISTINCT ON (uuid) ORDER BY uuid, <unattributed-last>, file_key`
    would have kept (the copy whose model is attributed beats
    an unattributed one; NULL counts unattributed).

    This is the invariant that lets the read endpoints filter a boolean
    instead of re-sorting the whole table (SV-CANONICAL-FLAG). The mini
    mirror carries a cross-session shared uuid, so there is a real
    duplicate to resolve.
    """
    ingest.run_ingest(trigger="manual")

    with db.viz_conn() as c:
        flagged = c.execute(
            "SELECT f.file_key, r.line_num FROM records r JOIN files f ON f.file_id = r.file_id WHERE r.is_canonical ORDER BY f.file_key, r.line_num"
        ).fetchall()
        # The legacy text rule, still the rule (R2: file_key TEXT order).
        uuid_text = uuid_split.uuid_text_sql("r")
        expected = c.execute(
            f"""
            SELECT file_key, line_num FROM (
              (SELECT DISTINCT ON ({uuid_text}) f.file_key, r.line_num
                 FROM records r JOIN files f ON f.file_id = r.file_id
                 JOIN dim_model dm ON dm.id = r.model_id
                WHERE {uuid_split.uuid_present_sql("r")}
                ORDER BY {uuid_text}, (COALESCE(dm.value, '')
                          IN ('', 'unknown', '(unknown)', '<synthetic>')),
                         f.file_key, r.line_num)
              UNION ALL
              (SELECT f.file_key, r.line_num FROM records r
                 JOIN files f ON f.file_id = r.file_id
                 WHERE {uuid_split.uuid_absent_sql("r")})
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


# --------------------------------------------------------------------------
# The winner rule's shared vocabulary: SQL and browser
# --------------------------------------------------------------------------

# The winner-rule shapes the SQL canonical pass and the browser's
# record-dedup must decide IDENTICALLY: each is one uuid's
# copies in file_key/arrival order, with the winner copy's arrival label.
# <synthetic> is Claude's harness-fabricated stub model; it names no
# model, so it sits in the unattributed class beside the two unknown
# fallbacks. records.model is NOT NULL, so the legacy-NULL member is
# unreachable here; the read-time DISTINCT ON test below covers it.
_REAL_MODEL = "claude-sonnet-4-5"
# Each copy is (arrival label, model, is_replay) — the replay member
# rides rank ahead of attribution.
_LOCKSTEP_SHAPES = [
    ("lane-unknown-then-real", [("a", "unknown", False), ("b", _REAL_MODEL, False)], "b"),
    ("real-then-lane-unknown", [("a", _REAL_MODEL, False), ("b", "unknown", False)], "a"),
    ("empty-model-then-real", [("a", "", False), ("b", _REAL_MODEL, False)], "b"),
    ("real-then-empty-model", [("a", _REAL_MODEL, False), ("b", "", False)], "a"),
    ("paren-unknown-then-real", [("a", "(unknown)", False), ("b", _REAL_MODEL, False)], "b"),
    ("real-then-paren-unknown", [("a", _REAL_MODEL, False), ("b", "(unknown)", False)], "a"),
    ("synthetic-then-real", [("a", "<synthetic>", False), ("b", _REAL_MODEL, False)], "b"),
    ("real-then-synthetic", [("a", _REAL_MODEL, False), ("b", "<synthetic>", False)], "a"),
    ("synthetic-vs-unknown", [("a", "<synthetic>", False), ("b", "unknown", False)], "a"),
    ("unknown-vs-synthetic", [("a", "unknown", False), ("b", "<synthetic>", False)], "a"),
    ("replay-then-original", [("a", _REAL_MODEL, True), ("b", _REAL_MODEL, False)], "b"),
    ("original-then-replay", [("a", _REAL_MODEL, False), ("b", _REAL_MODEL, True)], "a"),
    ("original-unknown-beats-attributed-replay",
     [("a", "unknown", False), ("b", _REAL_MODEL, True)], "a"),
]


def _seed_winner_shapes(c) -> None:
    """Seed every lockstep shape: one uuid per shape, one file per copy."""
    c.execute("INSERT INTO projects (project_id, display_name, first_seen_at, "
              "last_seen_at) VALUES ('p', 'p', now(), now())")
    for idx, (_name, copies, _winner) in enumerate(_LOCKSTEP_SHAPES):
        for pos, (label, model, replay) in enumerate(copies):
            c.execute(
                "INSERT INTO files (file_key, project_id, session_id, is_main, "
                "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
                "parser_version) VALUES (%s, 'p', 's', TRUE, 'e', 1, now(), "
                "now(), %s)",
                (f"shape-{idx}-{label}", parser_version.PARSER_VERSION))
            c.execute(
                "INSERT INTO records (file_id, line_num, fallback, model_id, is_replay) VALUES (%s, %s, %s, %s, %s)",
                (dims_seed.file_id(c, f"shape-{idx}-{label}"), pos + 1,
                 f"u-{idx}", dims_seed.dim_id(c, "dim_model", model), replay))


def _sql_winners() -> dict[str, str]:
    """The winning copy's arrival label per shape, from the flags."""
    with db.viz_conn() as c:
        flagged = c.execute(
            "SELECT f.file_key, r.is_canonical FROM records r JOIN files f ON f.file_id = r.file_id WHERE r.is_canonical"
        ).fetchall()
    winners = {}
    for file_key, _flag in flagged:
        if file_key.startswith("shape-"):
            idx = int(file_key.split("-")[1])
            winners[_LOCKSTEP_SHAPES[idx][0]] = file_key[-1]
    return winners


def _js_winners() -> dict[str, str]:
    """The same shapes through the browser's record-dedup, via node."""
    if shutil.which("node") is None:
        pytest.skip("node not available")
    shapes = {name: [[label, model, replay] for label, model, replay in copies]
              for name, copies, _winner in _LOCKSTEP_SHAPES}
    script = f"""
      global.window = {{}};
      require({str(_REPO_ROOT / 'src' / 'record-dedup.js')!r});
      const shapes = {json.dumps(shapes)};
      const out = {{}};
      for (const name in shapes) {{
        const seen = new Map();
        let winner = null;
        for (const [label, model, replay] of shapes[name]) {{
          if (window.recordDedup.decide(seen, {{ uuid: 'u', message: {{ model }},
                                                isReplay: replay === true }},
                                        1, new Map()) === 'keep') {{
            winner = label;
          }}
        }}
        out[name] = winner;
      }}
      console.log(JSON.stringify(out));
    """
    proc = run_node(["node", "-e", script], capture_output=True,
                    text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_canonical_winner_rule_is_lockstep_with_the_browser(fresh_db):
    """SV-CANONICAL-FLAG's vocabulary is the contract between the ingest's
    canonical pass and the browser's record-dedup. The SAME
    shape list drives both halves -- recompute_canonical over seeded rows
    and record-dedup's decide through node -- and each must crown the same
    winner per uuid, so a vocabulary edit on either side fails here before
    it can drift. The shapes cover every unattributed member against a
    real model in both file orders, plus member-vs-member ties that the
    file_key/arrival rule must settle the same way."""
    with db.viz_conn() as c:
        _seed_winner_shapes(c)
    ingest.recompute_canonical()
    sql = _sql_winners()
    js = _js_winners()
    want = {name: winner for name, _copies, winner in _LOCKSTEP_SHAPES}
    assert sql == want, f"SQL winners: {sql}"
    assert js == want, f"browser winners: {js}"


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
        web_metrics_readout, context_growth_traces,
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

    # The no-range warm: /api/models takes no range, so warm_common
    # warms ONE key per ingest, not one per WARM_RANGES entry. Forcing
    # `rng` into its key would reproduce a key nothing
    # writes — the same trap the /api/projects regression was.
    deadline = time.time() + 60
    while (cache.response_cache.get(_warm_key(api.list_models, None))
           is None and time.time() < deadline):
        time.sleep(0.25)
    assert cache.response_cache.get(
        _warm_key(api.list_models, None)) is not None, \
        "warm_common left /api/models uncached"


def _warm_key(fn, rng: str | None) -> str:
    """Reproduce cache_response's key for a request at `rng`.

    Built from the endpoint's own signature so it stays correct as params
    are added — which is exactly what broke /api/projects. `rng` is
    forced in only where the endpoint actually takes one; a no-range
    endpoint's key must carry no range or it matches nothing.
    """
    target = getattr(fn, "__wrapped__", fn)
    kwargs = {}
    for name, param in inspect.signature(target).parameters.items():
        default = param.default
        kwargs[name] = getattr(default, "default", default)
    if "rng" in kwargs:
        kwargs["rng"] = rng
    if "fresh" in kwargs:
        kwargs["fresh"] = 0
    return target.__qualname__ + ":" + repr(sorted(kwargs.items()))


def test_ingest_marks_response_cache_stale(fresh_db, mini_r2_env):
    """Ingest marks responses stale while leaving previous numbers servable.
    Refresh off the request path so readers do not all enter the uncached path."""
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
            f"SELECT f.file_key, r.line_num, "
            f"{uuid_split.uuid_text_sql('r')} AS uuid, r.request_id, dm.value, r.fresh_tokens, r.cache_creation_tokens, r.cache_read_tokens, r.output_tokens, r.eph5_tokens, r.eph1h_tokens, rp.cost_usd FROM records r JOIN records_price rp ON rp.file_id = r.file_id AND rp.line_num = r.line_num JOIN files f ON f.file_id = r.file_id JOIN dim_model dm ON dm.id = r.model_id ORDER BY f.file_key, r.line_num"
        ).fetchall()
        tools = c.execute(
            "SELECT f.file_key, t.line_num, t.idx, dtn.value, t.is_error FROM tool_uses t JOIN files f ON f.file_id = t.file_id JOIN dim_tool_name dtn ON dtn.id = t.tool_name_id ORDER BY f.file_key, t.line_num, t.idx"
        ).fetchall()
        projects = c.execute(
            "SELECT project_id, first_seen_at, last_seen_at FROM projects "
            "WHERE project_id <> '' ORDER BY project_id"
        ).fetchall()
    return files, records, tools, projects


def test_parallel_ingest_matches_sequential_exactly(
    fresh_db, mini_r2_env, monkeypatch
):
    """Concurrency must not change what lands in the DB.

    Fetch+parse is parallelised; if that leaked into ordering, dedup, or
    the per-file transaction boundary, the two snapshots would diverge.
    """
    monkeypatch.setenv("CLAUDIT_INGEST_WORKERS", "1")
    ingest.run_ingest("test-seq")
    sequential = _snapshot()

    with db.viz_conn() as c:
        c.execute("DELETE FROM files")
        c.execute("DELETE FROM projects")
        c.commit()

    monkeypatch.setenv("CLAUDIT_INGEST_WORKERS", "8")
    ingest.run_ingest("test-par")
    parallel = _snapshot()

    assert parallel[0] == sequential[0], "files differ"
    assert parallel[1] == sequential[1], "records differ"
    assert parallel[2] == sequential[2], "tool_uses differ"
    assert parallel[3] == sequential[3], "projects differ"
    assert len(sequential[1]) > 0, "fixture produced no records — vacuous test"


def test_ingest_workers_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("CLAUDIT_INGEST_WORKERS", raising=False)
    assert ingest.worker_count() >= 1
    monkeypatch.setenv("CLAUDIT_INGEST_WORKERS", "0")
    assert ingest.worker_count() == 1
    monkeypatch.setenv("CLAUDIT_INGEST_WORKERS", "not-a-number")
    assert ingest.worker_count() >= 1


def test_a_second_ingest_is_skipped_while_one_is_running(fresh_db, mini_r2_env):
    """A concurrent ingest must decline while another run is active.
    Cron can fire during a startup reparse; overlapping runs would duplicate
    fetches, parsing and rollup rebuilds while competing for progress state."""
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


def _suppress(pattern: str) -> None:
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
            c, "SELECT COUNT(*) FROM records r JOIN dim_model dm ON dm.id = r.model_id WHERE dm.value = 'claude-opus-4-7'")
        others = _scalar(
            c, "SELECT COUNT(*) FROM records r JOIN dim_model dm ON dm.id = r.model_id WHERE dm.value <> 'claude-opus-4-7'")
    assert before > 0 and others > 0, (
        "fixture must carry both, or this proves nothing")

    _suppress("claude-opus-%")
    ingest.run_ingest(trigger="manual")

    with db.viz_conn() as c:
        assert _scalar(
            c,
            "SELECT COUNT(*) FROM records r JOIN dim_model dm ON dm.id = r.model_id WHERE dm.value = 'claude-opus-4-7'") == 0
        assert _scalar(
            c, "SELECT COUNT(*) FROM records r JOIN dim_model dm ON dm.id = r.model_id WHERE dm.value <> 'claude-opus-4-7'"
        ) == others
        # The rollups read `records`, so they inherit the purge.
        assert _scalar(
            c, "SELECT COUNT(*) FROM usage_rollup u JOIN dim_model dm ON dm.id = u.model_id WHERE dm.value = 'claude-opus-4-7'") == 0


def test_suppression_matches_a_bare_model_id_exactly(fresh_db, mini_r2_env):
    """A pattern with no wildcard is still just an ILIKE — it must match
    that one model and leave its siblings alone."""
    ingest.run_ingest(trigger="manual")
    _suppress("claude-opus-4-7")
    ingest.run_ingest(trigger="manual")

    with db.viz_conn() as c:
        assert _scalar(
            c,
            "SELECT COUNT(*) FROM records r JOIN dim_model dm ON dm.id = r.model_id WHERE dm.value = 'claude-opus-4-7'") == 0
        assert _scalar(
            c, "SELECT COUNT(*) FROM records r JOIN dim_model dm ON dm.id = r.model_id WHERE dm.value = 'claude-sonnet-4-5'") > 0


def test_suppressed_tool_uses_go_with_their_records(fresh_db, mini_r2_env):
    """tool_rollup LEFT JOINs records for the model, so a tool call left
    behind by a purged record would resurface as an unlabelled model."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT f.file_key, r.line_num FROM records r JOIN files f ON f.file_id = r.file_id JOIN dim_model dm ON dm.id = r.model_id WHERE dm.value = 'claude-opus-4-7' ORDER BY f.file_key, r.line_num LIMIT 1"
        ).fetchone()
        assert row is not None
        c.execute(
            "INSERT INTO tool_uses (file_id, line_num, idx, tool_name_id) VALUES ((SELECT file_id FROM files WHERE file_key = %s), %s, 0, (SELECT id FROM dim_tool_name WHERE value = 'Bash'))",
            (row[0], row[1]))
        c.commit()

    _suppress("claude-opus-%")
    ingest.run_ingest(trigger="manual")

    with db.viz_conn() as c:
        assert _scalar(
            c, "SELECT COUNT(*) FROM tool_uses t JOIN files f ON f.file_id = t.file_id WHERE f.file_key = %s AND t.line_num = %s",
            (row[0], row[1])) == 0


def _plant(mirror, project, session, fixture):
    dest = mirror / project / session
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"{session}.jsonl"
    target.write_bytes(
        (_FIX_ROOT / "parser" / fixture).read_bytes()
    )
    return target


# Shared run-bookkeeping query for test_ingest_more.


def _last_run_finished_at():
    with db.viz_conn() as c:
        return _scalar(
            c,
            "SELECT finished_at FROM ingest_runs "
            "WHERE id = (SELECT MAX(id) FROM ingest_runs)",
        )
