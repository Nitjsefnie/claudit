# pylint: disable=too-many-lines
# One file because export/auth/read tests share app_with_data; splitting
# duplicates setup or requires cross-module fixture imports.
import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend import api, api_export, db, ingest, pricing
from tests import scratch_db

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_plot_db_module(monkeypatch, request):
    """Import plot module by path with helper modules isolated per test."""
    path = _REPO_ROOT / "scripts/plots/ccusage_plot_db.py"
    helper_names = ("ccusage_plot_render", "ccusage_plot_timeline")
    present_before = set(sys.modules).intersection(helper_names)
    for name in helper_names:
        if name not in present_before:
            request.addfinalizer(
                lambda name=name: sys.modules.pop(name, None))
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("ccusage_plot_db", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_export_argv_period_and_project():
    argv = api_export.build_export_argv("7d", "myproj", "/tmp/out.png")
    assert "/tmp/out.png" in argv
    assert argv[argv.index("-p") + 1] == "7d"
    assert argv[argv.index("--project") + 1] == "myproj"
    assert "--db-url" not in argv  # DSN comes from inherited env, not argv
    assert "--all" not in argv


def test_build_export_argv_all_and_no_project():
    argv = api_export.build_export_argv("all", None, "/tmp/out.png")
    assert "--all" in argv
    assert "-p" not in argv
    assert "--project" not in argv
    assert "--db-url" not in argv


def test_export_returns_png_attachment(app_with_data, monkeypatch):
    captured = {}

    async def fake_render(argv, out_path):
        captured["argv"] = argv
        # Simulate the script writing a PNG.
        with open(out_path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"fake")

    monkeypatch.setattr(api_export, "_render_export", fake_render)

    resp = app_with_data.get("/api/export?range=7d")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"\x89PNG")
    assert "-p" in captured["argv"] and "7d" in captured["argv"]


def test_export_bad_range_400(app_with_data, monkeypatch):
    async def fake_render(argv, out_path):  # should never be called
        raise AssertionError("render must not run on bad range")
    monkeypatch.setattr(api_export, "_render_export", fake_render)
    resp = app_with_data.get("/api/export?range=banana")
    assert resp.status_code == 400


def test_export_render_timeout_returns_503(app_with_data, monkeypatch):
    async def fake_render(argv, out_path):
        raise HTTPException(503, "export render timed out")
    monkeypatch.setattr(api_export, "_render_export", fake_render)
    resp = app_with_data.get("/api/export?range=7d")
    assert resp.status_code == 503


def test_export_render_failure_returns_500(app_with_data, monkeypatch):
    async def fake_render(argv, out_path):
        raise HTTPException(500, "export render failed")
    monkeypatch.setattr(api_export, "_render_export", fake_render)
    resp = app_with_data.get("/api/export?range=7d")
    assert resp.status_code == 500


def _failing_child_argv(stderr_text: str) -> list[str]:
    """A real subprocess argv that writes `stderr_text` to stderr and
    exits 1 — what the plot child looks like on a missing module."""
    return [sys.executable, "-c",
            f"import sys; sys.stderr.write({stderr_text!r}); sys.exit(1)"]


@pytest.mark.parametrize("stderr_text", [
    "ModuleNotFoundError: No module named 'matplotlib'",
    "Traceback (most recent call last):\n"
    "ModuleNotFoundError: No module named 'psycopg'",
])
def test_export_missing_module_child_is_503_naming_export_python(
    app_with_data, tmp_path, stderr_text
):
    """A plot child that died of ModuleNotFoundError means the EXPORT_PYTHON
    interpreter lacks matplotlib or psycopg — answer 503 naming the knob,
    not an opaque 500 (issue #115)."""
    out_path = str(tmp_path / "out.png")
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(api_export._render_export(  # pylint: disable=protected-access
            _failing_child_argv(stderr_text), out_path))
    assert excinfo.value.status_code == 503
    assert "EXPORT_PYTHON" in str(excinfo.value.detail)


def test_export_other_child_failure_stays_500(app_with_data, tmp_path):
    """Any other nonzero exit keeps the plain 500 — only the missing-module
    shape is classified."""
    out_path = str(tmp_path / "out.png")
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(api_export._render_export(  # pylint: disable=protected-access
            _failing_child_argv("some other failure"), out_path))
    assert excinfo.value.status_code == 500
    assert str(excinfo.value.detail) == "export render failed"


def test_plot_db_project_filter_subsets_events(app_with_data, monkeypatch, request):
    """load_events(project=...) returns a strict subset of all-projects,
    and every returned event belongs to the requested project."""
    mod = _load_plot_db_module(monkeypatch, request)
    # Dynamic import hides DB_URL from static checking; main() rebinds it.
    setattr(mod, "DB_URL", os.environ["DATABASE_URL_VIZ"])

    all_events = mod.load_events(None, None)
    assert all_events, "fixture should yield records"

    # Discover a real project_id from the test DB.
    with closing(psycopg.connect(mod.DB_URL)) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT project_id FROM files ORDER BY 1")
        project_ids = [r[0] for r in cur.fetchall()]
    assert len(project_ids) >= 2, "mini fixture has 2 projects"
    target = project_ids[0]

    filtered = mod.load_events(None, None, project=target)
    assert filtered, "project filter should still yield records"
    assert len(filtered) < len(all_events), "filter must drop the other project"


def _build_api_client(mp, label: str):
    """Fresh DB + mini R2 + ingest, yielding a TestClient on the api router.

    Auth is bypassed by mounting only the router into a clean app.
    """
    test_db = scratch_db.create_database(label)
    mp.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    src = _REPO_ROOT / "fixtures/r2_mini"
    tmp = tempfile.mkdtemp(prefix="sv-api-")
    shutil.copytree(src, Path(tmp) / "r2")
    mp.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")

    db.reset_viz_pool()

    ingest.run_ingest(trigger="manual")

    a = FastAPI()
    a.include_router(api.router)

    yield TestClient(a)

    db.reset_viz_pool()
    shutil.rmtree(tmp)
    scratch_db.drop_database(test_db)


# Module-scoped because fresh DB + ingest cost 2-5s per read-only test.
# Tests that write use `app_with_fresh_data` below.
@pytest.fixture(scope="module", name="app_with_data")
def _app_with_data_fixture():
    mp = pytest.MonkeyPatch()          # monkeypatch itself is function-scoped
    try:
        yield from _build_api_client(mp, "api")
    finally:
        mp.undo()


@pytest.fixture(name="app_with_fresh_data")
def _app_with_fresh_data_fixture():
    """Function-scoped variant for tests that mutate rows, so they cannot
    contaminate the shared module-scoped client."""
    mp = pytest.MonkeyPatch()
    try:
        yield from _build_api_client(mp, "api_mut")
    finally:
        mp.undo()


def test_cost_by_context_buckets_and_cumulative_share(app_with_data):
    """Bars + cumulative share. The panel's whole reading is 'what
    fraction of spend sits above N tokens of context', so the cumulative
    column has to be monotonic and land on 1.0 at the last bucket."""
    r = app_with_data.get("/api/cost-by-context?range=all")
    assert r.status_code == 200
    body = r.json()
    buckets = body["buckets"]
    assert buckets, "fixture produced no buckets"

    edges = [b["ctx_bucket"] for b in buckets]
    assert edges == sorted(edges), "buckets must be ordered by context size"
    assert all(e % 50_000 == 0 for e in edges), "edges must be 50k-aligned"
    assert all(b["requests"] > 0 for b in buckets)

    cum = [b["cum_share"] for b in buckets]
    assert cum == sorted(cum), "cumulative share must be monotonic"
    assert cum[-1] == pytest.approx(1.0)

    total = sum(b["cost_usd"] for b in buckets)
    assert body["total_cost_usd"] == pytest.approx(total)


def test_cost_by_context_bad_range_400(app_with_data):
    assert app_with_data.get(
        "/api/cost-by-context?range=banana").status_code == 400


def test_cost_by_context_model_filter_subsets(app_with_data):
    """A model filter must never widen the result: it selects whole
    (bucket, model) rows out of the rollup."""
    allm = app_with_data.get("/api/cost-by-context?range=all").json()
    models = app_with_data.get("/api/models").json()["models"]
    assert models, "fixture has no models"
    top = models[0]["model"]          # rows are {model, n}, not strings
    one = app_with_data.get(
        f"/api/cost-by-context?range=all&model={top}").json()
    assert one["total_cost_usd"] <= allm["total_cost_usd"] + 1e-9
    assert one["total_cost_usd"] > 0


def test_projects(app_with_data):
    r = app_with_data.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    pids = sorted(p["project_id"] for p in body["projects"])
    assert pids == ["projA", "projB"]
    # session_count + total_cost drive the picker chip and its ordering.
    # file_count was dropped: nothing rendered it (and it was reporting
    # the joined record count, not the file count).
    for p in body["projects"]:
        assert "session_count" in p and "total_cost" in p
        assert "file_count" not in p


def test_projects_range_scoped_ordering_and_zero_token_exclusion(app_with_fresh_data):
    """SV-ISSUE-6 + the free-lane fix: /api/projects orders by RANGE-
    scoped cost — range-scoped TOKENS order the zero-cost projects and
    break cost ties — and drops any project whose ALL-TIME TOKENS are 0
    (no usage at all). Two different aggregates that must not be
    conflated. A lane priced at $0 has usage and must stay listed; a
    project with all-time usage but nothing in the selected range stays
    listed too, sorted to the bottom."""
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (project_id, display_name, first_seen_at, last_seen_at) VALUES "
            "('projNeverToken', 'projNeverToken', now(), now()), "
            "('projFree', 'projFree', now(), now()), "
            "('projOldCost', 'projOldCost', now(), now()), "
            "('projRecentCost', 'projRecentCost', now(), now())"
        )
        cur.execute(
            "INSERT INTO usage_rollup (session_id, project_id, hour, model, is_main, "
            "first_ts, last_ts, requests, fresh_tokens, cost_usd) VALUES "
            # No tokens ever — "no usage at all". Must be excluded
            # outright, even though it has a usage_rollup row (and even
            # though cost would have been 0 under the old rule too).
            "('sess-zero', 'projNeverToken', now() - interval '1 day', 'm', TRUE, now(), now(), 1, 0, 0), "
            # A FREE lane: tokens but zero cost. Must be LISTED — this is
            # the empty-picker bug the tokens rule fixes.
            "('sess-free', 'projFree', now() - interval '1 hour', 'm', TRUE, now(), now(), 4, 4000, 0), "
            # Real historical cost (bigger than projRecentCost's), but 60
            # days old — outside a 7d range. Must still be LISTED
            # (all-time tokens are nonzero) but sorted below the in-range
            # projects.
            "('sess-old', 'projOldCost', now() - interval '60 days', 'm', TRUE, now(), now(), 1, 1000, 5.00), "
            # Smaller all-time cost, but entirely inside the 7d range —
            # must outrank projOldCost despite the smaller all-time total,
            # proving the ordering is RANGE-scoped, not all-time.
            "('sess-recent', 'projRecentCost', now() - interval '1 hour', 'm', TRUE, now(), now(), 1, 1000, 1.00)"
        )
        conn.commit()

    r = app_with_fresh_data.get("/api/projects?range=7d")
    assert r.status_code == 200
    body = r.json()
    by_id = {p["project_id"]: p for p in body["projects"]}

    assert "projNeverToken" not in by_id, \
        "all-time-zero-token project must be excluded, not just range-filtered"
    assert "projFree" in by_id, \
        "a zero-cost lane with usage must be listed"
    assert by_id["projFree"]["total_cost"] == 0.0
    assert by_id["projOldCost"]["total_cost"] == 0.0
    assert by_id["projRecentCost"]["total_cost"] == 1.0

    pids_in_order = [p["project_id"] for p in body["projects"]]
    assert pids_in_order.index("projRecentCost") < pids_in_order.index("projOldCost"), (
        "ordering must follow range-scoped cost, not all-time cost — "
        "projOldCost's larger all-time total must NOT outrank projRecentCost"
    )
    assert pids_in_order.index("projFree") < pids_in_order.index("projOldCost"), (
        "zero-cost projects order by range-scoped tokens, then after every "
        "in-range costed project"
    )

    # Widening the range to include projOldCost's usage re-sorts it above
    # projRecentCost — proving the list is genuinely range-scoped, not a
    # fixed order computed once.
    r_wide = app_with_fresh_data.get("/api/projects?range=90d")
    assert r_wide.status_code == 200
    body_wide = r_wide.json()
    by_id_wide = {p["project_id"]: p for p in body_wide["projects"]}
    assert by_id_wide["projOldCost"]["total_cost"] == 5.0
    pids_wide = [p["project_id"] for p in body_wide["projects"]]
    assert pids_wide.index("projOldCost") < pids_wide.index("projRecentCost")


def test_cache_per_model_shape(app_with_data):
    r = app_with_data.get("/api/cache?range=3650d")
    assert r.status_code == 200
    body = r.json()
    assert "per_model" in body and "session_total" in body
    assert "top_output" in body and "top_cache_create" in body and "top_cache_read" in body
    if body["per_model"]:
        m = body["per_model"][0]
        assert {"model", "turns", "fresh", "cache_create", "cache_read",
                "output", "eph5", "eph1h", "hit_rate_pct",
                "cost_total", "cost_buckets"} <= set(m)
        assert {"fresh", "create_5m", "create_1h", "read", "output"} == set(m["cost_buckets"])


def test_cache_dedups_cross_file_uuid(app_with_data):
    """sess-C main + agent peer both have shared-uuid-1; sess-D main also
    has it. Records table holds 3 rows for that uuid; DISTINCT ON dedups
    to 1 in the per_model totals.

    sess-C main has input=1000, output=500 (single record).
    sess-C agent has input=1000, output=500 (same uuid → dedup'd).
    sess-D main has 2 records: shared-uuid-1 (1000/500, dedup'd) +
                                sess-D-only (50/25, kept).

    After cross-file dedup:
      shared-uuid-1 winner = lexicographically-first file_key, which is
      claude/projB/sess-C/agent-aaaa.jsonl (agent- < sess-)
      WAIT — actually 'a' < 's' so the agent file IS lexicographically
      first. Either way, ONE row claims the shared uuid; the other two
      drop. The remaining tally for projB: 1000 + 50 input, 500 + 25 output.
    """
    r = app_with_data.get("/api/cache?range=3650d&project=projB")
    body = r.json()
    assert body["session_total"]["fresh"] == 1050   # 1000 + 50
    assert body["session_total"]["output"] == 525   # 500 + 25
    assert body["session_total"]["turns"] == 2       # one shared + one unique


def test_cache_top_n_limited_to_10(app_with_data):
    r = app_with_data.get("/api/cache?range=3650d")
    body = r.json()
    assert len(body["top_output"]) <= 10
    assert len(body["top_cache_create"]) <= 10
    assert len(body["top_cache_read"]) <= 10


def test_cache_bad_range_400(app_with_data):
    r = app_with_data.get("/api/cache?range=abc")
    assert r.status_code == 400


def test_cache_session_total_matches_per_model_sum(app_with_data):
    r = app_with_data.get("/api/cache?range=3650d")
    body = r.json()
    sum_turns = sum(m["turns"] for m in body["per_model"])
    sum_cost = round(sum(m["cost_total"] for m in body["per_model"]), 4)
    assert body["session_total"]["turns"] == sum_turns
    assert body["session_total"]["cost_total"] == sum_cost


def test_cache_session_total_estimated_rate_true_when_any_model_estimated(
    app_with_data, monkeypatch
):
    """Fixture data carries two real models (claude-opus-4-7 and
    claude-sonnet-4-5), both exact matches normally. Drop one from the rate
    table so it resolves as "default" (estimated) while the other stays
    exact — a genuine mixed session, same shape a live account would show
    the day a new model ships before the rate table is updated.
    """
    patched = {k: v for k, v in pricing.MODEL_RATES.items() if k != "claude-sonnet-4-5"}
    monkeypatch.setattr(pricing, "MODEL_RATES", patched)

    body = app_with_data.get("/api/cache?range=3650d").json()
    assert len(body["per_model"]) >= 2, body["per_model"]
    flags = {m["model"]: m["estimated_rate"] for m in body["per_model"]}
    assert flags["claude-sonnet-4-5"] is True
    assert flags["claude-opus-4-7"] is False
    assert body["session_total"]["estimated_rate"] is True


def test_cache_session_total_estimated_rate_false_when_all_exact(app_with_data):
    body = app_with_data.get("/api/cache?range=3650d").json()
    assert body["per_model"], "fixture produced no per-model rows"
    assert all(m["estimated_rate"] is False for m in body["per_model"])
    assert body["session_total"]["estimated_rate"] is False


def test_cache_session_total_estimated_rate_false_for_empty_per_model(app_with_data):
    """An empty per_model list must not crash any(...) over it, and must not
    default-True a total with no contributing models."""
    r = app_with_data.get("/api/cache?range=3650d&model=nonexistent-model-zzz")
    assert r.status_code == 200
    body = r.json()
    assert body["per_model"] == []
    assert body["session_total"]["estimated_rate"] is False


def test_transcript_streams(app_with_data):
    r = app_with_data.get("/api/sessions/sess-A/transcript")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-ndjson"
    first = r.text.split("\n")[0]
    assert "type" in json.loads(first)


def test_transcript_etag_header(app_with_data):
    r = app_with_data.get("/api/sessions/sess-A/transcript")
    assert "etag" in {k.lower() for k in r.headers.keys()}


def test_transcript_404(app_with_data):
    r = app_with_data.get("/api/sessions/does-not-exist/transcript")
    assert r.status_code == 404


def test_sidecar_path_validation(app_with_data):
    r = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "data/tool-results/x.txt"},
    )
    assert r.status_code == 200
    assert r.text.strip() == "tool output"
    r2 = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "../../../etc/passwd"},
    )
    assert r2.status_code == 400


def test_sidecar_absolute_path_rejected(app_with_data):
    r = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "/etc/passwd"},
    )
    assert r.status_code == 400


def test_sidecar_missing_file_404(app_with_data):
    r = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "data/does-not-exist.txt"},
    )
    assert r.status_code == 404


def test_sidecar_path_nul_byte_rejected(app_with_data):
    """A `%00` in `path` must be a 400 naming the bad parameter. It used to
    pass validation and blow up `open()` with `ValueError: embedded null
    byte`, escaping as a 500 (issue #112)."""
    r = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "data/\x00/tool-results.txt"},
    )
    assert r.status_code == 400
    assert "path" in r.json()["detail"]


def test_sidecar_path_dot_component_rejected(app_with_data):
    """A `.` component (a whole-path `.` or mid-path) must be a 400 naming
    the bad parameter. A whole-path `.` used to become a directory key and
    blow up file-mode `open()` with IsADirectoryError — a 500; mid-path the
    two r2 backends silently DISAGREE (file mode resolves `a/./b`, S3 does
    not), so neither spelling may reach the object fetch (issue #112)."""
    r = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "."},
    )
    assert r.status_code == 400
    assert "path" in r.json()["detail"]
    r_dot = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "data/./tool-results/x.txt"},
    )
    assert r_dot.status_code == 400
    assert "path" in r_dot.json()["detail"]


@pytest.fixture(name="app_with_rl_data")
def _app_with_rl_data_fixture(monkeypatch):
    """Fresh DB + R2 mirror plus one session whose file mtime is current
    but which carries both an in-range and an out-of-range rate-limit hit.

    The out-of-range hit reproduces the bug where /api/dashboard filtered
    rate-limit hits by file mtime (r2_last_modified) rather than the hit's
    own ts. Yields (client, in_range_ts, out_of_range_ts).
    """
    test_db = scratch_db.create_database("api_rl")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    tmp = tempfile.mkdtemp(prefix="sv-api-rl-")
    shutil.copytree(_REPO_ROOT / "fixtures/r2_mini", Path(tmp) / "r2")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")

    now = datetime.now(timezone.utc)
    in_range = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out_range = (now - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _rl(ts, uid):
        return json.dumps({
            "type": "assistant", "timestamp": ts, "uuid": uid,
            "isApiErrorMessage": True, "error": "rate_limit",
            "message": {"role": "assistant", "content": [{
                "type": "text",
                "text": "Claude usage limit reached - you are out of "
                        "extra usage.",
            }]},
        })

    sess_dir = Path(tmp) / "r2" / "claude" / "projA" / "sess-RL"
    sess_dir.mkdir(parents=True)
    (sess_dir / "sess-RL.jsonl").write_text(
        json.dumps({"type": "user", "timestamp": in_range, "uuid": "rl-u1",
                    "message": {"role": "user", "content": "hi"}}) + "\n"
        + json.dumps({
            "type": "assistant", "timestamp": in_range, "uuid": "rl-a1",
            "requestId": "rl-req-1",
            "message": {"role": "assistant", "model": "claude-sonnet-4-5",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 10, "output_tokens": 20,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}}}) + "\n"
        + _rl(in_range, "rl-h1") + "\n"
        + _rl(out_range, "rl-h2") + "\n"
    )

    db.reset_viz_pool()

    ingest.run_ingest(trigger="manual")

    a = FastAPI()
    a.include_router(api.router)

    yield TestClient(a), in_range, out_range

    db.reset_viz_pool()
    shutil.rmtree(tmp)
    scratch_db.drop_database(test_db)


# ---------------------------------------------------------------- heatmap

def _insert_tz_probe_rows():
    """Two records with a unique model, one in winter (CET, UTC+1) and one
    in summer (CEST, UTC+2), to prove the endpoint is DST-aware."""
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (project_id, display_name, first_seen_at, last_seen_at) "
            "VALUES ('projTZ', 'projTZ', now(), now()) ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, r2_etag, "
            "r2_size_bytes, r2_last_modified, parsed_at, parser_version) "
            "VALUES ('projTZ/tz.jsonl', 'projTZ', 'tzsess', TRUE, 'etag-tz', 1, now(), now(), 'test')"
        )
        cur.execute(
            "INSERT INTO records (file_key, line_num, uuid, ts, model, output_tokens, cost_usd) VALUES "
            # 2026-01-15 is a Thursday (ISODOW 4); 10:30Z in CET (UTC+1) is 11:30 local.
            "('projTZ/tz.jsonl', 1, 'uuid-tz-winter', '2026-01-15T10:30:00Z', 'tz-probe-model', 10, 0.01), "
            # 2026-07-15 is a Wednesday (ISODOW 3); 10:30Z in CEST (UTC+2) is 12:30 local.
            "('projTZ/tz.jsonl', 2, 'uuid-tz-summer', '2026-07-15T10:30:00Z', 'tz-probe-model', 20, 0.02)"
        )
        conn.commit()

    # /api/activity-heatmap reads usage_rollup, which ingest rebuilds from
    # `records`. These rows were inserted behind ingest's back, so rebuild
    # it here or the endpoint cannot see them (SV-ROLLUP: the rollup is
    # derived state; anything mutating `records` outside ingest must
    # rebuild it).
    ingest.rebuild_rollup()


# --------------------------------------------------------- backend.app routes
