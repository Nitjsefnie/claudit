"""Issue #375: the transcript cache is keyed by (file_key, etag), never by
the etag alone.

In file:// mode an object's etag is derived from its size and mtime
alone (``sha1("{mtime_ns}:{size}")`` in the r2 listing), so two
transcripts of equal size and modification time share one etag — and an
etag-only cache key served one session's transcript for another: a
cross-session data leak between users of a multi-user deploy.

Issue #390 adds the behavioural pin for the replacement guarantee the
``cache.transcript_key`` docstring states — an object replaced under
its own key (new etag → new key) must not serve its predecessor's
cached bytes — derives the key-shape test's identity from the ``files``
row rather than a literal, and covers a file key that itself contains
the ``:`` separator.
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
_CAROL = (
    b'{"type":"user","message":'
    b'{"role":"user","content":"carol-only secret"}}\n'
)
# The replacement payload (issue #390): different bytes under the SAME
# object key, so the stored etag must move and the endpoint must build
# a new composite key rather than serve the predecessor's entry.
_ALICE_V2 = (
    b'{"type":"user","message":'
    b'{"role":"user","content":"alice-only secret: replaced"}}\n'
)

# The exact mtime the twin files carry (ns).
_TWIN_MTIME_NS = 1_000_000_000
# The replacement's fresh mtime: a different instant, so the new etag
# differs from the replaced one.
_REPLACED_MTIME_NS = _TWIN_MTIME_NS + 1

# The three twin sessions; sess:carol's directory name carries the `:`
# the composite key uses as its separator.
_TWINS = (
    ("sess-alice", _ALICE),
    ("sess-bob", _BOB),
    ("sess:carol", _CAROL),
)


@pytest.fixture(name="twin_app")
def _twin_app_fixture(monkeypatch):
    """Scratch DB + a file:// mirror holding three equal-size,
    equal-mtime transcripts, ingested; the api router mounted on a clean
    app (auth bypassed, as test_api's _build_api_client does)."""
    test_db = scratch_db.create_database("issue375")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    tmp = tempfile.mkdtemp(prefix="sv-issue375-")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")
    mirror = Path(tmp) / "r2" / "claude"
    for session, body in _TWINS:
        session_dir = mirror / "projA" / session
        session_dir.mkdir(parents=True)
        path = session_dir / f"{session}.jsonl"
        path.write_bytes(body)
        # One exact mtime for all: the file-mode etag hashes exactly
        # mtime_ns and size, so this manufactures the etag collision.
        os.utime(path, ns=(_TWIN_MTIME_NS, _TWIN_MTIME_NS))

    db.reset_viz_pool()
    ingest.run_ingest(trigger="manual")

    # Precondition: the issue's collision actually holds — every stored
    # etag is equal. If this ever stops holding, the tests below stop
    # exercising the bug and must be rebuilt.
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT session_id, r2_etag FROM files WHERE is_main"
        ).fetchall()
    etags = dict(rows)
    assert len(rows) == len(_TWINS), "missing main files row(s)"
    assert len(set(etags.values())) == 1, (
        "fixture lost the equal-size+equal-mtime etag collision"
    )

    app = FastAPI()
    app.include_router(api.router)
    yield TestClient(app)

    db.reset_viz_pool()
    shutil.rmtree(tmp)
    scratch_db.drop_database(test_db)


def test_equal_size_mtime_transcripts_not_served_for_each_other(twin_app):
    """Serving alice warms the cache; bob's and carol's GETs must still
    return their own bytes. Under the etag-only key the second GET
    returned alice's transcript (issue #375)."""
    r_alice = twin_app.get("/api/sessions/sess-alice/transcript")
    assert r_alice.status_code == 200
    assert r_alice.content == _ALICE

    r_bob = twin_app.get("/api/sessions/sess-bob/transcript")
    assert r_bob.status_code == 200
    assert r_bob.content == _BOB

    r_carol = twin_app.get("/api/sessions/sess:carol/transcript")
    assert r_carol.status_code == 200
    assert r_carol.content == _CAROL


def test_transcript_cache_key_pairs_file_key_with_etag(twin_app):
    """The LRU entry sits under transcript_key(file_key, etag), with
    both halves taken from the files row the endpoint itself reads —
    the key shape the orphan sweep's eviction must name too."""
    r = twin_app.get("/api/sessions/sess-alice/transcript")
    assert r.status_code == 200
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT file_key, r2_etag FROM files "
            "WHERE session_id = %s AND is_main = TRUE LIMIT 1",
            ("sess-alice",),
        ).fetchone()
    assert row is not None
    file_key, etag = row
    assert r.headers["etag"] == etag
    key = cache.transcript_key(file_key, etag)
    assert cache.transcript_cache.get(key) == _ALICE


def test_replaced_object_serves_new_bytes(twin_app):
    """The docstring's replacement guarantee, driven behaviourally: an
    object replaced under its own key (new bytes, fresh mtime) and
    re-ingested is served with its NEW bytes — the new etag mints a new
    composite key, so the predecessor's cached entry cannot be served.
    Under a key that drops the etag, the stale predecessor IS served
    (this test's mutant proof)."""
    r = twin_app.get("/api/sessions/sess-alice/transcript")
    assert r.status_code == 200
    assert r.content == _ALICE

    with db.viz_conn() as c:
        row = c.execute(
            "SELECT r2_etag FROM files "
            "WHERE session_id = %s AND is_main = TRUE LIMIT 1",
            ("sess-alice",),
        ).fetchone()
    assert row is not None
    old_etag = row[0]

    root = os.environ["R2_ENDPOINT"].removeprefix("file://")
    path = Path(root) / "claude" / "projA" / "sess-alice" / "sess-alice.jsonl"
    path.write_bytes(_ALICE_V2)
    os.utime(path, ns=(_REPLACED_MTIME_NS, _REPLACED_MTIME_NS))

    ingest.run_ingest(trigger="manual")

    replaced = twin_app.get("/api/sessions/sess-alice/transcript")
    assert replaced.status_code == 200
    assert replaced.content == _ALICE_V2
    assert replaced.headers["etag"] != old_etag
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT r2_etag FROM files "
            "WHERE session_id = %s AND is_main = TRUE LIMIT 1",
            ("sess-alice",),
        ).fetchone()
    assert row is not None
    assert replaced.headers["etag"] == row[0]


def test_separator_in_file_key_keeps_objects_apart(twin_app):
    """A file key that itself contains the composite key's `:`
    separator is still keyed per object: after alice's transcript has
    been served, carol's GET returns carol's own bytes, and the two LRU
    entries sit under distinct keys."""
    r_alice = twin_app.get("/api/sessions/sess-alice/transcript")
    assert r_alice.status_code == 200

    r_carol = twin_app.get("/api/sessions/sess:carol/transcript")
    assert r_carol.status_code == 200
    assert r_carol.content == _CAROL

    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT session_id, file_key, r2_etag FROM files WHERE is_main"
        ).fetchall()
    stored = {sid: cache.transcript_key(fk, etag) for sid, fk, etag in rows}
    assert stored["sess-alice"] != stored["sess:carol"]
    assert cache.transcript_cache.get(stored["sess-alice"]) == _ALICE
    assert cache.transcript_cache.get(stored["sess:carol"]) == _CAROL
