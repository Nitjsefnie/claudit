"""Orphan-sweep cache eviction tests: deleted transcripts leave the cache."""
import shutil
import tempfile
from pathlib import Path

import pytest

from backend import cache, db, ingest
from tests import scratch_db

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    """Per-test schema reset on a separate DB."""
    yield from scratch_db.scratch_viz_database(monkeypatch, "ingest")


@pytest.fixture(name="mini_r2_env")
def _mini_r2_env_fixture(monkeypatch):
    src = _REPO_ROOT / "fixtures/r2_mini"
    tmp = tempfile.mkdtemp(prefix="sv-ingest-")
    shutil.copytree(src, Path(tmp) / "r2")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")
    yield Path(tmp) / "r2" / "claude"
    shutil.rmtree(tmp)


def test_orphan_sweep_evicts_deleted_file_from_transcript_cache(
        fresh_db, mini_r2_env):
    """Issue #269: the sweep that drops a deleted file's rows must also
    evict its cached transcript bytes — they are keyed by r2_etag
    (api_sessions), so without the eviction the raw transcript stays
    served from cache after its source object is deleted. A survivor's
    entry is untouched."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT file_key, r2_etag FROM files "
            "WHERE file_key LIKE '%sess-B.jsonl' "
            "OR file_key LIKE '%sess-A.jsonl'").fetchall()
    etags = {Path(fk).name: etag for fk, etag in rows}
    doomed, survivor = etags["sess-B.jsonl"], etags["sess-A.jsonl"]
    assert doomed != survivor
    cache.transcript_cache.put(doomed, b'{"doomed": true}\n')
    cache.transcript_cache.put(survivor, b'{"survivor": true}\n')

    (mini_r2_env / "projA" / "sess-B" / "sess-B.jsonl").unlink()
    result = ingest.run_ingest(trigger="manual")
    assert result["deleted"] == 1

    assert cache.transcript_cache.get(doomed) is None
    assert cache.transcript_cache.get(survivor) == b'{"survivor": true}\n'


def test_full_scope_orphan_sweep_evicts_cached_transcripts(
        fresh_db, mini_r2_env):
    """Issue #269, the full-scope branch: an empty listing (seen_keys
    empty) deletes EVERY files row, and every deleted row's cached
    transcript bytes go with it."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        etags = [
            row[0] for row in c.execute(
                "SELECT r2_etag FROM files").fetchall()
        ]
    assert len(etags) == 5
    assert len(set(etags)) == 5
    for etag in etags:
        cache.transcript_cache.put(etag, b'{"body": true}\n')

    for p in mini_r2_env.rglob("*.jsonl"):
        p.unlink()
    result = ingest.run_ingest(trigger="manual")
    assert result["deleted"] == 5

    for etag in etags:
        assert cache.transcript_cache.get(etag) is None
