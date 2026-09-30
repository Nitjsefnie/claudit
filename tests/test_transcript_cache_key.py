"""Issue #375: the transcript cache is keyed by (file_key, etag), never by
the etag alone.

In file:// mode an object's etag is derived from its size and mtime
alone (``sha1("{mtime_ns}:{size}")`` in the r2 listing), so two
transcripts of equal size and modification time share one etag — and an
etag-only cache key served one session's transcript for another: a
cross-session data leak between users of a multi-user deploy.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, cache, db, ingest
from tests import scratch_db

_ALICE = (
    b'{"type":"user","message":'
    b'{"role":"user","content":"alice-only secret"}}\n'
)
_BOB = (
    b'{"type":"user","message":'
    b'{"role":"user","content":"bob-only secret--"}}\n'
)

# The exact mtime both twin files carry (ns).
_TWIN_MTIME_NS = 1_000_000_000


@pytest.fixture(name="twin_app")
def _twin_app_fixture(monkeypatch):
    """Scratch DB + a file:// mirror holding two equal-size,
    equal-mtime transcripts, ingested; the api router mounted on a clean
    app (auth bypassed, as test_api's _build_api_client does)."""
    test_db = scratch_db.create_database("issue375")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    tmp = tempfile.mkdtemp(prefix="sv-issue375-")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")
    mirror = Path(tmp) / "r2" / "claude"
    for session, body in (("sess-alice", _ALICE), ("sess-bob", _BOB)):
        session_dir = mirror / "projA" / session
        session_dir.mkdir(parents=True)
        path = session_dir / f"{session}.jsonl"
        path.write_bytes(body)
        # One exact mtime for both: the file-mode etag hashes exactly
        # mtime_ns and size, so this manufactures the etag collision.
        os.utime(path, ns=(_TWIN_MTIME_NS, _TWIN_MTIME_NS))

    db.reset_viz_pool()
    ingest.run_ingest(trigger="manual")

    # Precondition: the issue's collision actually holds — both stored
    # etags are equal. If this ever stops holding, the tests below stop
    # exercising the bug and must be rebuilt.
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT session_id, r2_etag FROM files WHERE is_main"
        ).fetchall()
    etags = dict(rows)
    assert etags.get("sess-alice"), "no files row for sess-alice"
    assert etags["sess-alice"] == etags.get("sess-bob"), (
        "fixture lost the equal-size+equal-mtime etag collision"
    )

    app = FastAPI()
    app.include_router(api.router)
    yield TestClient(app)

    db.reset_viz_pool()
    shutil.rmtree(tmp)
    scratch_db.drop_database(test_db)


def test_equal_size_mtime_transcripts_not_served_for_each_other(twin_app):
    """Serving alice warms the cache; bob's GET must still return bob's
    own bytes. Under the etag-only key the second GET returned alice's
    transcript (issue #375)."""
    r_alice = twin_app.get("/api/sessions/sess-alice/transcript")
    assert r_alice.status_code == 200
    assert r_alice.content == _ALICE

    r_bob = twin_app.get("/api/sessions/sess-bob/transcript")
    assert r_bob.status_code == 200
    assert r_bob.content == _BOB


def test_transcript_cache_key_pairs_file_key_with_etag(twin_app):
    """The LRU entry sits under transcript_key(file_key, etag) — the key
    shape the orphan sweep's eviction must name too."""
    r = twin_app.get("/api/sessions/sess-alice/transcript")
    assert r.status_code == 200
    key = cache.transcript_key(
        "claude/projA/sess-alice/sess-alice.jsonl", r.headers["etag"]
    )
    assert cache.transcript_cache.get(key) == _ALICE
