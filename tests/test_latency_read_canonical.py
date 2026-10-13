"""/api/reply-latency's live scans must filter is_canonical.

SV-CANONICAL-FLAG resolved cross-file uuid dedup at INGEST into
records.is_canonical so read endpoints filter a boolean instead of
sorting the whole table per request. `_latency_live` -- the live pass
for bucket widths `latency_rollup` does not store -- runs TWO scans
over `records`:

- `bands_sql`, the per-(bucket, model) percentile fold;
- `outliers_sql`, the per-(bucket, model) top/bottom 1% outlier pass.

Neither filtered the flag, so a replayed copy (a forked Codex rollout's
replayed prefix) counted again in the percentiles and could surface
among the outliers -- the same defect class as `tests/
test_sessions_read_canonical.py` and the /api/models read pin
(issue #644, `test_models_excludes_non_canonical_rows`).

The rollup path is fine: `backend/ingest_rollup_latency.py` already
filters `r.is_canonical` when it builds `latency_rollup`, so only the
live path needed the fix.

The test drives the live path by asking for `range=2d`, whose bucket
width is 900s -- not in `constants.LATENCY_BUCKETS` (3600, 21600,
43200, 86400) -- so `reply_latency` selects `_latency_live` and reads
the directly-inserted `records` rows.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import os

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend import api, cache, db, r2
from backend.api_common import _iso
from backend.constants import LATENCY_BUCKETS
from backend.ingest_rollup_latency import rebuild_latency_rollup
from tests import dims_seed, scratch_db

_MODEL = "lat-live-canon-probe"
_FILE_KEY = "projLat/sessLat/lat.jsonl"
# Above the outlier pass's bucket_n >= 100 gate, so the extreme row has
# a chance to surface (an empty outlier list would pass vacuously).
_CANON_ROWS = 120
_CANON_LATENCY_S = 10.0
# The single non-canonical row: same model, same 900s bucket, an extreme
# latency that would rank #1 among the outliers if it counted.
_NONCANON_LINE = 9999
_NONCANON_LATENCY_S = 999.0


def _seed() -> None:
    """One project + file, then _CANON_ROWS canonical rows and one
    non-canonical extreme row, all sharing one (bucket, model) group
    (`now()` is the whole transaction's single timestamp, so every row
    lands in the same 900s bucket)."""
    with closing(psycopg.connect(os.environ["CLAUDIT_DATABASE_URL_VIZ"])) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (project_id, display_name, "
                "first_seen_at, last_seen_at) "
                "VALUES ('projLat', 'projLat', now(), now())")
            cur.execute(
                "INSERT INTO files (file_key, project_id, session_id, "
                "is_main, r2_etag, r2_size_bytes, r2_last_modified, "
                "parsed_at, parser_version) "
                "VALUES (%s, 'projLat', 'sessLat', TRUE, 'etag-lat', 1, "
                "now(), now(), 'test')",
                (_FILE_KEY,))
            fid = dims_seed.file_id(cur, _FILE_KEY)
            mid = dims_seed.dim_id(cur, "dim_model", _MODEL)
            cur.execute(
                "INSERT INTO records (file_id, line_num, fallback, ts, model_id, "
                "output_tokens, reply_latency_s, is_canonical) "
                "SELECT %s, g, 'uuid-lat-' || g, now(), %s, 1, %s, TRUE "
                "FROM generate_series(1, %s) AS g",
                (fid, mid, _CANON_LATENCY_S, _CANON_ROWS))
            cur.execute(
                "INSERT INTO records_price (file_id, line_num) "
                "SELECT %s, g FROM generate_series(1, %s) AS g",
                (fid, _CANON_ROWS))
            fid = dims_seed.file_id(cur, _FILE_KEY)
            mid = dims_seed.dim_id(cur, "dim_model", _MODEL)
            cur.execute(
                "INSERT INTO records (file_id, line_num, fallback, ts, model_id, "
                "output_tokens, reply_latency_s, is_canonical) "
                "VALUES (%s, %s, 'uuid-lat-noncanon', now(), %s, 1, "
                "%s, FALSE)",
                (fid, _NONCANON_LINE, mid, _NONCANON_LATENCY_S))
            cur.execute(
                "INSERT INTO records_price (file_id, line_num) "
                "VALUES (%s, %s)",
                (fid, _NONCANON_LINE))
        conn.commit()


@pytest.fixture(name="lat_client")
def _lat_client_fixture(monkeypatch):
    """A fresh DB holding one seeded (bucket, model) group, served by a
    TestClient on the api router (auth bypassed)."""
    test_db = scratch_db.create_database("latency_canon")
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    db.reset_viz_pool()
    try:
        _seed()
        a = FastAPI()
        a.include_router(api.router)
        yield TestClient(a)
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(test_db)


def _latency(lat_client) -> dict:
    return lat_client.get(
        f"/api/reply-latency?range=2d&model={_MODEL}").json()


def _band(body: dict) -> dict:
    bands = [b for b in body["bands"] if b["model"] == _MODEL]
    assert len(bands) == 1, bands
    return bands[0]


def _outlier_lines(body: dict) -> set[int]:
    return {o["line"] for o in body["outliers"]}


def test_reply_latency_live_excludes_non_canonical(lat_client) -> None:
    """Both live scans drop a non-canonical row, and both count it once
    it is flipped canonical (SV-CANONICAL-FLAG)."""
    body = _latency(lat_client)
    band = _band(body)
    assert band["n"] == _CANON_ROWS, (
        f"bands_sql counted the non-canonical row: n={band['n']}")
    assert _NONCANON_LINE not in _outlier_lines(body), (
        "outliers_sql surfaced the non-canonical row")

    # Flip the row canonical out of band: production invalidates the
    # response cache at ingest; the test does the same by hand, exactly
    # as test_models_excludes_non_canonical_rows does.
    with closing(psycopg.connect(os.environ["CLAUDIT_DATABASE_URL_VIZ"])) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE records r SET is_canonical = TRUE "
                "FROM files f WHERE r.file_id = f.file_id "
                "AND f.file_key = %s AND r.line_num = %s",
                (_FILE_KEY, _NONCANON_LINE))
        conn.commit()
    cache.response_cache.clear()

    body = _latency(lat_client)
    assert _band(body)["n"] == _CANON_ROWS + 1
    assert _NONCANON_LINE in _outlier_lines(body), (
        "a canonical extreme row must surface among the outliers")


_OUT_NOW = datetime(2026, 6, 23, 10, 0, 0, 123400, tzinfo=timezone.utc)
_OUT_COLUMNS = "out_ts, out_latency_s, out_file_id, out_line_num, out_high"


def _legacy_rollup(rng, project, model, bucket_s, since):
    """JSONB reader oracle: the pre-array query and response projection."""
    args = [bucket_s, project or "", since]
    model_filter = ""
    if model:
        model_filter = "AND dm.value LIKE %s"
        args.append(f"%{model}%")
    with db.viz_conn() as conn:
        rows = conn.execute(db.sql_text(
            f"SELECT bucket, dm.value, n, p10, p50, p90, outliers "
            "FROM latency_rollup l JOIN dim_model dm ON dm.id = l.model_id "
            "JOIN projects pj ON pj.proj_id = l.proj_id "
            "WHERE bucket_s = %s AND pj.project_id = %s "
            "AND bucket >= to_timestamp(floor(EXTRACT(EPOCH FROM "
            f"%s::timestamptz) / {bucket_s}) * {bucket_s} + {bucket_s} / 2.0) "
            f"{model_filter} ORDER BY bucket, dm.value"), args).fetchall()
    return {"range": rng, "project": project, "model": model, "bucket_s": bucket_s,
            "bands": [{"ts": _iso(b), "model": m, "n": int(n or 0),
                       "p10": float(a or 0), "p50": float(c or 0), "p90": float(d or 0)}
                      for b, m, n, a, c, d, _ in rows],
            "outliers": [{"ts": o.get("ts"), "model": m,
                          "latency_s": float(o.get("latency_s") or 0),
                          "file_key": r2.public_key(o.get("file_key")),
                          "line": int(o.get("line_num") or 0)}
                         for _, m, _, _, _, _, dots in rows for o in dots or []]}


def _seed_legacy_outliers(conn):
    conn.execute("ALTER TABLE latency_rollup DROP CONSTRAINT IF EXISTS latency_out_cardinality")
    for column in _OUT_COLUMNS.split(", "):
        conn.execute(f"ALTER TABLE latency_rollup DROP COLUMN IF EXISTS {column}")
    conn.execute("ALTER TABLE latency_rollup ADD COLUMN IF NOT EXISTS "
                 "outliers JSONB NOT NULL DEFAULT '[]'")
    pid = dims_seed.seed_project(conn, "out-project")
    mid = dims_seed.dim_id(conn, "dim_model", "out-model")
    small = dims_seed.dim_id(conn, "dim_model", "small-model")
    ids = {}
    # Surrogate order opposes file_key order, so an id-based tie sort fails.
    for stem in ("z", "a"):
        key = f"claude/out-project/s/{stem}.jsonl"
        conn.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, "
            "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, parser_version) "
            "VALUES (%s, 'out-project', 's', TRUE, 'e', 1, %s, %s, 'test')",
            (key, _OUT_NOW, _OUT_NOW))
        ids[stem] = dims_seed.file_id(conn, key)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO records (file_id, line_num, ts, model_id, reply_latency_s) "
            "VALUES (%s, %s, %s, %s, %s)",
            [(ids["z"], 1, _OUT_NOW + timedelta(seconds=1), mid, 99),
             (ids["a"], 1, _OUT_NOW + timedelta(seconds=2), mid, 99),
             (ids["a"], 2, _OUT_NOW + timedelta(seconds=3), mid, 1.125),
             (ids["a"], 3, _OUT_NOW + timedelta(seconds=4), mid, 2.875)]
            + [(ids["a"], line, _OUT_NOW, mid, 10) for line in range(4, 200)]
            + [(ids["z"], 2, _OUT_NOW, small, 10)])
    for width in LATENCY_BUCKETS:
        for proj in (0, pid):
            conn.execute(
                "INSERT INTO latency_rollup "
                "(bucket_s, bucket, proj_id, model_id, n, p10, p50, p90, outliers) "
                "SELECT %s, to_timestamp(floor(EXTRACT(EPOCH FROM %s::timestamptz) "
                "/ %s) * %s + %s / 2), %s, %s, 200, 10, 10, 10, "
                "jsonb_agg(jsonb_build_object('ts', r.ts, 'latency_s', r.reply_latency_s, "
                "'file_key', f.file_key, 'line_num', r.line_num, "
                "'kind', CASE WHEN r.reply_latency_s = 99 THEN 'high' ELSE 'low' END) "
                "ORDER BY r.reply_latency_s DESC, f.file_key, r.line_num) "
                "FROM records r JOIN files f USING (file_id) "
                "WHERE model_id = %s AND reply_latency_s <> 10",
                (width, _OUT_NOW, width, width, width, proj, mid, mid))
            conn.execute(
                "INSERT INTO latency_rollup "
                "(bucket_s, bucket, proj_id, model_id, n, p10, p50, p90) "
                "SELECT %s, bucket, %s, %s, 1, 10, 10, 10 FROM latency_rollup "
                "WHERE bucket_s = %s AND proj_id = %s AND model_id = %s",
                (width, proj, small, width, proj, mid))
    orphan = dims_seed.seed_project(conn, "orphan-project")
    conn.execute(
        "INSERT INTO latency_rollup "
        "(bucket_s, bucket, proj_id, model_id, n, outliers) "
        "VALUES (3600, %s, %s, %s, 2, %s)",
        (_OUT_NOW, orphan, mid, dims_seed.Jsonb([
            {"ts": _OUT_NOW.isoformat(), "latency_s": 7.25,
             "file_key": "claude/orphan-project/s/missing.jsonl", "line_num": 17, "kind": "low"}])))
    return ids


def _outlier_responses():
    bodies = []
    for guest in (False, True):
        app = FastAPI()

        @app.middleware("http")
        async def flag(request: Request, call_next, is_guest=guest):
            request.state.is_guest = is_guest
            return await call_next(request)

        app.include_router(api.router)
        cache.response_cache.clear()
        with TestClient(app) as client:
            for rng in ("7d", "30d", "90d", "all"):
                for filters in ("", "&project=out-project&model=out-model"):
                    response = client.get(f"/api/reply-latency?range={rng}{filters}")
                    assert response.status_code == 200
                    assert len(response.json()["outliers"]) == 4
                    bodies.append(response.content)
    return bodies


def _latency_catalog(conn):
    columns = conn.execute(
        "SELECT column_name, udt_name, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_name = 'latency_rollup' "
        "ORDER BY ordinal_position").fetchall()
    constraints = conn.execute(
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'latency_rollup'::regclass ORDER BY conname").fetchall()
    return columns, constraints


def _assert_converted_outliers(conn, ids):
    arrays = conn.execute(
        f"SELECT {_OUT_COLUMNS} FROM latency_rollup "
        "WHERE n = 200 ORDER BY bucket_s, proj_id").fetchall()
    assert arrays == [([_OUT_NOW + timedelta(seconds=s) for s in (2, 1, 4, 3)],
                       [99, 99, 2.875, 1.125],
                       [ids['a'], ids['z'], ids['a'], ids['a']],
                       [1, 1, 3, 2], [True, True, False, False])] * 8
    assert conn.execute(
        f"SELECT {_OUT_COLUMNS} FROM latency_rollup WHERE n = 1"
    ).fetchall() == [(None,) * 5] * 8
    assert conn.execute(
        f"SELECT {_OUT_COLUMNS} FROM latency_rollup WHERE n = 2"
    ).fetchone() == ([_OUT_NOW], [7.25], [None], [17], [False])
    sizes = conn.execute(
        "SELECT pg_column_size(l) FROM latency_rollup l "
        "WHERE n IN (1, 200) ORDER BY bucket_s, proj_id, model_id").fetchall()
    for column in _OUT_COLUMNS.split(", "):
        for value in ("NULL", "'{}'"):
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(f"UPDATE latency_rollup SET {column} = {value} WHERE n = 200")
    return arrays, sizes


@pytest.mark.db
def test_outlier_arrays_migrate_and_rebuild_without_response_changes(monkeypatch):
    """Conversion retains order/NULLs, SQL timestamp spelling and response bytes."""
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return _OUT_NOW.astimezone(tz or timezone.utc)

    monkeypatch.setattr(api, "datetime", Clock)
    original_conn = db.viz_conn
    zone = "Europe/Prague"

    @contextmanager
    def zoned_conn():
        with original_conn() as conn:
            conn.execute("SELECT set_config('TimeZone', %s, false)", (zone,))
            yield conn

    name = scratch_db.create_database("out_arrays")
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
    monkeypatch.setattr(db, "viz_conn", zoned_conn)
    db.reset_viz_pool()
    try:
        with db.viz_conn() as conn:
            fresh = _latency_catalog(conn)
            ids = _seed_legacy_outliers(conn)
            conn.execute("ANALYZE latency_rollup, projects, dim_model, files")
        with monkeypatch.context() as legacy:
            legacy.setattr(api, "_latency_from_rollup", _legacy_rollup)
            before = _outlier_responses()
        db.apply_schema()
        with db.viz_conn() as conn:
            assert "outliers" not in scratch_db.table_columns(conn, "latency_rollup")
            assert _latency_catalog(conn) == fresh
            conn.execute("ANALYZE latency_rollup, projects, dim_model, files")
            arrays, sizes = _assert_converted_outliers(conn, ids)
            physical = conn.execute(
                "SELECT relfilenode, relnatts FROM pg_class WHERE oid = 'latency_rollup'::regclass"
            ).fetchone()
            conn.execute("DELETE FROM schema_stamp")
        assert _outlier_responses() == before
        db.apply_schema()
        with db.viz_conn() as conn:
            assert conn.execute(
                "SELECT relfilenode, relnatts FROM pg_class WHERE oid = 'latency_rollup'::regclass"
            ).fetchone() == physical
            assert _latency_catalog(conn) == fresh
        rebuild_latency_rollup()
        with db.viz_conn() as conn:
            assert conn.execute(
                f"SELECT {_OUT_COLUMNS} FROM latency_rollup "
                "WHERE n = 200 ORDER BY bucket_s, proj_id").fetchall() == arrays
            assert conn.execute(
                "SELECT pg_column_size(l) FROM latency_rollup l "
                "ORDER BY bucket_s, proj_id, model_id").fetchall() == sizes
        assert _outlier_responses() == before
        zone = "UTC"
        responses = _outlier_responses()
        assert responses != before
        assert b'2026-06-23T10:00:02.1234+00:00' in responses[0]
        with db.viz_conn() as conn:
            conn.execute("UPDATE latency_rollup SET out_file_id[1] = NULL WHERE n = 200")
        responses = _outlier_responses()
        assert b'"file_key":null' in responses[0]
        assert b'"file_key"' not in responses[8]
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)
