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

from contextlib import closing
import os

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, cache, db
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
