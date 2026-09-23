"""Several buckets per deploy: R2_BUCKET names several buckets joined by
'+', every stored file key is `<bucket>/<object-key>`, and a failure to
list ANY configured bucket aborts the whole run before the orphan sweep —
a partial listing must never be allowed to sweep a bucket's history.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, db, ingest, r2

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Two Claude-layout transcripts with the same object key shape but
# different bodies, so the two buckets' etags differ.
_TX_A = (
    '{"type":"user","timestamp":"2026-05-07T10:00:00Z","uuid":"u1",'
    '"message":{"role":"user","content":"hi"}}\n'
    '{"type":"assistant","timestamp":"2026-05-07T10:00:01Z","uuid":"a1",'
    '"requestId":"req-1","sessionId":"sessS",'
    '"message":{"role":"assistant","model":"claude-sonnet-4-5",'
    '"content":[{"type":"text","text":"from alpha"}],'
    '"usage":{"input_tokens":100,"output_tokens":200,'
    '"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
)
_TX_B = _TX_A.replace("from alpha", "from beta")

_OBJ_KEY = "projS/sessS/sessS.jsonl"


def _seed(root: Path, bucket: str, body: str) -> Path:
    """One transcript at <root>/<bucket>/projS/sessS/sessS.jsonl."""
    d = root / bucket / "projS" / "sessS"
    d.mkdir(parents=True)
    f = d / "sessS.jsonl"
    f.write_text(body)
    return f


@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    """Per-test schema reset on a separate DB (same shape as
    test_ingest's fixture, kept local so this module stands alone)."""
    test_db = "claudit_test"
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")
    os.system(f"createdb {test_db} 2>/dev/null")
    os.system(
        f"psql {test_db} -f {_REPO_ROOT / 'backend/schema.sql'} >/dev/null"
    )
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    db.reset_viz_pool()
    yield
    db.reset_viz_pool()
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")


@pytest.fixture(name="two_buckets")
def _two_buckets_fixture(monkeypatch, tmp_path):
    """alpha and beta mirrors holding the SAME object key."""
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/")
    monkeypatch.setenv("R2_BUCKET", "alpha+beta")
    _seed(tmp_path, "alpha", _TX_A)
    _seed(tmp_path, "beta", _TX_B)
    return tmp_path


# ---------------------------------------------------------------------------
# r2 client: bucket parsing, qualified keys, refusals
# ---------------------------------------------------------------------------


def test_buckets_splits_strips_and_dedupes(monkeypatch):
    monkeypatch.setenv("R2_BUCKET", "alpha + beta +alpha")
    assert r2.buckets() == ["alpha", "beta"]


def test_buckets_rejects_invalid_name(monkeypatch):
    monkeypatch.setenv("R2_BUCKET", "claude+Bad_Name")
    with pytest.raises(ValueError, match="Bad_Name"):
        r2.buckets()
    monkeypatch.setenv("R2_BUCKET", "alpha++beta")
    with pytest.raises(ValueError, match="''"):
        r2.buckets()


def test_buckets_default_is_claude(monkeypatch):
    monkeypatch.delenv("R2_BUCKET", raising=False)
    assert r2.buckets() == ["claude"]


def test_split_key_separates_bucket_from_object_key():
    assert r2.split_key("claude/proj/sess/file.jsonl") == (
        "claude",
        "proj/sess/file.jsonl",
    )
    with pytest.raises(ValueError, match="no bucket segment"):
        r2.split_key("noslash")
    with pytest.raises(ValueError, match="no bucket segment"):
        r2.split_key("claude/")


def test_list_keys_qualifies_every_object_with_its_bucket(two_buckets):
    keys = sorted(o.key for o in r2.list_keys())
    assert keys == [
        f"alpha/{_OBJ_KEY}",
        f"beta/{_OBJ_KEY}",
    ]


def test_get_object_reads_from_the_named_bucket(two_buckets):
    assert r2.get_object(f"alpha/{_OBJ_KEY}") == _TX_A.encode()
    assert r2.get_object(f"beta/{_OBJ_KEY}") == _TX_B.encode()


def test_get_object_refuses_unconfigured_bucket(monkeypatch, tmp_path):
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/")
    monkeypatch.setenv("R2_BUCKET", "alpha")
    _seed(tmp_path, "alpha", _TX_A)
    with pytest.raises(ValueError, match="not configured"):
        r2.get_object("c/x")
    with pytest.raises(ValueError, match="not configured"):
        r2.get_stream("c/x")


def test_traversal_still_blocked_on_the_object_key_part(
        monkeypatch, tmp_path):
    """The bucket segment is refused by name; traversal INSIDE the
    object-key part still hits _safe_join's PermissionError."""
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/")
    monkeypatch.setenv("R2_BUCKET", "alpha")
    (tmp_path / "alpha").mkdir()
    with pytest.raises(PermissionError):
        r2.get_object("alpha/../../etc/passwd")
    with pytest.raises(PermissionError):
        r2.get_stream("alpha/../../etc/passwd")


# ---------------------------------------------------------------------------
# ingest over several buckets
# ---------------------------------------------------------------------------


def test_single_bucket_stores_bucket_qualified_key(fresh_db, tmp_path,
                                                   monkeypatch):
    """A single-bucket deploy re-keys too: stored identity is
    'claude/<object-key>', not the bare object key."""
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/")
    monkeypatch.delenv("R2_BUCKET", raising=False)
    _seed(tmp_path, "claude", _TX_A)

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT file_key, project_id, session_id, is_main FROM files"
        ).fetchone()
    assert row == ("claude/projS/sessS/sessS.jsonl", "projS", "sessS", True)


def test_same_key_in_two_buckets_two_rows_one_project(
        fresh_db, two_buckets):
    """The same object key in two buckets is two files but ONE project:
    file identity is bucket-qualified, project identity is not."""
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["inserted"] == 2
    assert result["r2_listed"] == 2
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT file_key, project_id, session_id, is_main FROM files "
            "ORDER BY file_key"
        ).fetchall()
        n_projects = c.execute(
            "SELECT COUNT(*) FROM projects").fetchone()
    assert rows == [
        (f"alpha/{_OBJ_KEY}", "projS", "sessS", True),
        (f"beta/{_OBJ_KEY}", "projS", "sessS", True),
    ]
    assert n_projects is not None and n_projects[0] == 1


def test_orphan_sweep_is_per_object_never_per_bucket(fresh_db, two_buckets):
    """An object vanishing from bucket alpha deletes exactly alpha's row;
    beta's same-named key survives."""
    ingest.run_ingest(trigger="manual")
    (two_buckets / "alpha" / _OBJ_KEY).unlink()

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["deleted"] == 1
    with db.viz_conn() as c:
        keys = [r[0] for r in c.execute("SELECT file_key FROM files")]
    assert keys == [f"beta/{_OBJ_KEY}"]


def test_missing_bucket_mirror_directory_aborts_the_run_before_the_sweep(
        fresh_db, two_buckets):
    """A configured bucket whose mirror directory is GONE is not an empty
    bucket: its listing cannot be known complete, so the run must abort
    with a fatal instead of sweeping the bucket's whole history."""
    ingest.run_ingest(trigger="manual")
    shutil.rmtree(two_buckets / "beta")

    result = ingest.run_ingest(trigger="manual")

    assert result["error"] is not None, (
        "a missing bucket directory must abort the run, not list empty")
    assert result["deleted"] == 0, "a failed listing must never sweep"
    with db.viz_conn() as c:
        keys = [r[0] for r in c.execute(
            "SELECT file_key FROM files ORDER BY file_key")]
    assert keys == [f"alpha/{_OBJ_KEY}", f"beta/{_OBJ_KEY}"]


def test_listing_failure_aborts_the_run_before_the_orphan_sweep(
        fresh_db, two_buckets, monkeypatch):
    """Bucket beta's listing raises mid-walk: NO row of alpha or beta may
    be deleted, because neither listing was known complete. The run books
    a fatal error instead."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        before = c.execute(
            "SELECT file_key FROM files ORDER BY file_key").fetchall()

    real_list = r2.list_keys

    def broken(prefix: str = ""):
        """Alpha lists fine; beta's listing dies mid-walk."""
        objs = list(real_list(prefix="alpha/"))

        def gen():
            yield from objs
            raise RuntimeError("listing beta exploded")

        return gen()

    monkeypatch.setattr(ingest.r2, "list_keys", broken)
    result = ingest.run_ingest(trigger="manual")
    monkeypatch.undo()

    assert result["error"] is not None
    assert "RuntimeError" in result["error"]
    assert result["deleted"] == 0, "a failed listing must never sweep"
    with db.viz_conn() as c:
        after = c.execute(
            "SELECT file_key FROM files ORDER BY file_key").fetchall()
    assert after == before, "both buckets' rows survive"


# ---------------------------------------------------------------------------
# reads: transcript + sidecar stay inside the file's own bucket
# ---------------------------------------------------------------------------


@pytest.fixture(name="sidecar_app")
def _sidecar_app_fixture(fresh_db, tmp_path, monkeypatch):
    """A session whose file lives in bucket alpha, with one sidecar of
    its own; beta holds the SAME-shaped sidecar under the same relative
    path, so a request that reached across would find beta's copy."""
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/")
    monkeypatch.setenv("R2_BUCKET", "alpha+beta")
    _seed(tmp_path, "alpha", _TX_A)
    sess = tmp_path / "alpha" / "projT" / "sessT"
    sess.mkdir(parents=True)
    (sess / "sessT.jsonl").write_text(_TX_B)
    (sess / "data").mkdir()
    (sess / "data" / "ok.txt").write_text("alpha ok")
    beta_sess = tmp_path / "beta" / "projT" / "sessT"
    (beta_sess / "data").mkdir(parents=True)
    (beta_sess / "data" / "ok.txt").write_text("beta ok")
    (beta_sess / "data" / "x.txt").write_text("beta payload")

    ingest.run_ingest(trigger="manual")

    a = FastAPI()
    a.include_router(api.router)
    yield TestClient(a)


def test_transcript_serves_from_the_file_own_bucket(sidecar_app):
    r = sidecar_app.get("/api/sessions/sessT/transcript")
    assert r.status_code == 200
    assert r.text == _TX_B


def test_sidecar_serves_from_the_file_own_bucket(sidecar_app):
    r = sidecar_app.get("/api/sessions/sessT/sidecar?path=data/ok.txt")
    assert r.status_code == 200
    assert r.text == "alpha ok"


def test_sidecar_cannot_reach_another_bucket(sidecar_app):
    """alpha's sessT has no data/x.txt; beta's same-shaped session does.
    The request must 404 rather than fall through to the other bucket —
    the bucket served is the stored file's own, and the request never
    names one."""
    r = sidecar_app.get("/api/sessions/sessT/sidecar?path=data/x.txt")
    assert r.status_code == 404


def test_sidecar_dotdot_path_is_rejected(sidecar_app):
    """The only path from one bucket to another runs through '..' —
    rejected outright (and _safe_join backs the check up in r2)."""
    r = sidecar_app.get(
        "/api/sessions/sessT/sidecar"
        "?path=../beta/projT/sessT/data/x.txt"
    )
    assert r.status_code == 400


def test_sidecar_unconfigured_bucket_surfaces_as_an_error(sidecar_app):
    """A stored file key naming a bucket that is not configured is a
    server-side misconfiguration: r2's refusal (ValueError) must
    propagate out of the sidecar candidate loop, not be swallowed into a
    404 as if the object were merely missing."""
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, "
            "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
            "parser_version) VALUES ('ghost/p/ghost-sess/m.jsonl', 'projT', "
            "'ghost-sess', TRUE, 'e', 1, now(), now(), 'test')")
        c.commit()

    with pytest.raises(ValueError):
        sidecar_app.get("/api/sessions/ghost-sess/sidecar?path=data/ok.txt")


def test_single_bucket_missing_root_aborts_the_run_before_the_sweep(
        fresh_db, tmp_path, monkeypatch):
    """Single-bucket file mode: an endpoint root that is missing (an
    unmounted mountpoint) must abort the ingest with a fatal, never list
    empty — the orphan sweep would delete the bucket's entire history."""
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/absent/")
    monkeypatch.delenv("R2_BUCKET", raising=False)

    result = ingest.run_ingest(trigger="manual")

    assert result["error"] is not None, (
        "a missing endpoint root must abort the run, not list empty")
    assert result["deleted"] == 0, "a failed listing must never sweep"


# ---------------------------------------------------------------------------
# Startup validates R2_BUCKET
# ---------------------------------------------------------------------------


def test_startup_refuses_an_invalid_bucket_name(monkeypatch):
    """app.validate_bucket_config runs in lifespan, so a bad R2_BUCKET
    aborts boot — instead of the scheduler booking a fatal while the
    server half-serves and every transcript fetch 500s."""
    from backend import app as app_mod

    monkeypatch.setenv("R2_BUCKET", "claude+Bad_Name")
    with pytest.raises(ValueError, match="Bad_Name"):
        app_mod.validate_bucket_config()


def test_lifespan_calls_the_bucket_validation():
    from backend import app as app_mod
    import inspect

    src = inspect.getsource(app_mod.lifespan)
    assert "validate_bucket_config()" in src
