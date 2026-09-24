"""tool_uses.is_canonical (SV-CANONICAL-FLAG, tool half) and the
latency rollup's canonical filter. Split out of test_ingest.py, which is
at pylint's module-length limit."""
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, constants, db, ingest
# Importing the fixture functions registers them here under their
# @pytest.fixture(name=...) names.
from tests.test_ingest import (  # noqa: F401  # pylint: disable=unused-import
    _fresh_db_fixture, _mini_r2_env_fixture, _scalar,
)


def test_tool_uses_canonical_dedups_shared_tool_use_id(fresh_db, mini_r2_env):
    """A compaction sidecar replays the main file's tool_use blocks with
    the same ids. The mini mirror's shared-uuid line carries one such
    block in both sess-C.jsonl and agent-aaaa.jsonl; exactly one of the
    two rows is canonical, chosen by the same (file_key, line_num) order
    records use, and the rollups count it once."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT file_key, is_canonical FROM tool_uses "
            "WHERE tool_use_id = 'toolu_shared' ORDER BY file_key"
        ).fetchall()
        rolled = _scalar(c, "SELECT COALESCE(SUM(n_total), 0) FROM tool_rollup "
                            "WHERE tool_name = 'Bash'")
    assert [r[1] for r in rows] == [True, False], rows
    assert rows[0][0].endswith("agent-aaaa.jsonl")
    assert rolled == 1
    assert ingest.recompute_canonical() == 0


def _tool_usage_counts(client: TestClient, rng: str) -> dict[str, int]:
    body = client.get(f"/api/tool-usage?range={rng}").json()
    counts: dict[str, int] = {}
    for b in body["buckets"]:
        counts[b["tool"]] = counts.get(b["tool"], 0) + b["n"]
    return counts


def test_tool_usage_live_path_skips_replayed_calls(fresh_db, mini_r2_env):
    """The 24h view (buckets under an hour) reads tool_uses live; the
    replayed copy of toolu_shared is non-canonical and must not count
    there, so the live path agrees with the rollup-backed ranges."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        c.execute("UPDATE tool_uses SET ts = %s",
                  (datetime.now(timezone.utc),))
        c.commit()
    ingest.rebuild_tool_rollup()
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    live = _tool_usage_counts(client, "1d")
    rolled = _tool_usage_counts(client, "30d")
    assert live["Bash"] == 1
    assert live == rolled


def test_latency_rollup_counts_canonical_records_only(fresh_db, mini_r2_env):
    """The shared-uuid duplicate must not enter the latency percentiles."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        c.execute("UPDATE records SET reply_latency_s = 1.5")
        c.commit()
    ingest.rebuild_latency_rollup()
    with db.viz_conn() as c:
        canon = _scalar(c, "SELECT COUNT(*) FROM records WHERE is_canonical")
        rolled = _scalar(c, "SELECT SUM(n) FROM latency_rollup "
                            "WHERE project_id = '' AND bucket_s = %s",
                         (max(constants.LATENCY_BUCKETS),))
    assert rolled == canon


def test_files_models_survive_suppression(fresh_db, mini_r2_env):
    """A lane switch is only visible through files.models once the
    foreign rows are purged: the file still names the model whose
    records are gone."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        c.execute("INSERT INTO suppressed_models (pattern, note) "
                  "VALUES ('claude-opus-%', 'test')")
        c.commit()
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        left = _scalar(c, "SELECT COUNT(*) FROM records WHERE model LIKE 'claude-opus-%'")
        named = _scalar(c, "SELECT COUNT(*) FROM files f WHERE EXISTS ("
                           "SELECT 1 FROM unnest(f.models) m JOIN suppressed_models s "
                           "ON m ILIKE s.pattern)")
    assert left == 0
    assert named > 0
