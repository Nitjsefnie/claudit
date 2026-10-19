from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend import api
from backend.cache import response_cache
from backend.constants import LATENCY_BUCKETS
from tests import dims_seed

pytest_plugins = ("tests.test_api",)

_CACHE_ROW_LISTS = ("top_output", "top_cache_create", "top_cache_read")
_PUBLIC_AGENT_KEY = "projB/sess-C/agent-aaaa.jsonl"
_ROLLUP_MODEL = "issue-244-rollup-seed"
_LIVE_MODEL = "issue-244-live-seed"


@pytest.fixture(scope="module", name="guest_client")
def guest_client_fixture():
    app = FastAPI()

    @app.middleware("http")
    async def set_guest_flag(request: Request, call_next):
        request.state.is_guest = True
        return await call_next(request)

    app.include_router(api.router)
    with TestClient(app) as client:
        yield client


def _fixture_agent_key() -> str:
    with psycopg.connect(os.environ["CLAUDIT_DATABASE_URL_VIZ"]) as conn:
        row = conn.execute(
            "SELECT file_key FROM files "
            "WHERE project_id = 'projB' AND session_id = 'sess-C' "
            "AND file_key LIKE '%/agent-aaaa.jsonl' LIMIT 1"
        ).fetchone()
    assert row is not None, "mini fixture agent file should exist"
    return row[0]


def _delete_seeded_records(file_key: str, line_nums: list[int]) -> None:
    with psycopg.connect(os.environ["CLAUDIT_DATABASE_URL_VIZ"]) as conn:
        conn.execute(
            "DELETE FROM records r USING files f "
            "WHERE r.file_id = f.file_id AND f.file_key = %s "
            "  AND r.line_num = ANY(%s)",
            (file_key, line_nums),
        )


def _seed_rollup_latency_outlier() -> tuple[int, int]:
    file_key = _fixture_agent_key()
    bucket_s = LATENCY_BUCKETS[-1]
    bucket_start = int(datetime.now(timezone.utc).timestamp() // bucket_s) * bucket_s
    bucket = datetime.fromtimestamp(bucket_start + bucket_s / 2, timezone.utc)
    line_num = 991001
    outlier = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "latency_s": 12.5,
        "file_key": file_key,
        "line_num": line_num,
        "kind": "high",
    }
    with psycopg.connect(os.environ["CLAUDIT_DATABASE_URL_VIZ"]) as conn:
        dims_seed.seed_latency_rollup(
            conn, "", bucket_s, bucket, model=_ROLLUP_MODEL, n=100,
            p10=1, p50=5, p90=10, outliers=[outlier],
        )
        conn.commit()
    return bucket_s, line_num


def _delete_rollup_latency_outlier() -> None:
    with psycopg.connect(os.environ["CLAUDIT_DATABASE_URL_VIZ"]) as conn:
        conn.execute(
            "DELETE FROM latency_rollup WHERE model_id = "
            "(SELECT id FROM dim_model WHERE value = %s)",
            (_ROLLUP_MODEL,),
        )


def _seed_live_latency_outliers() -> tuple[str, list[int]]:
    file_key = _fixture_agent_key()
    line_nums = list(range(992001, 992101))
    now = datetime.now(timezone.utc)
    seeded_rows = [
        (file_key, line_num, f"issue-244-live-{line_num}", now, _LIVE_MODEL,
         line_num - line_nums[0] + 1)
        for line_num in line_nums
    ]
    with psycopg.connect(os.environ["CLAUDIT_DATABASE_URL_VIZ"]) as conn:
        fid = dims_seed.file_id(conn, file_key)
        mid = dims_seed.dim_id(conn, "dim_model", _LIVE_MODEL)
        conn.cursor().executemany(
            "INSERT INTO records (file_id, line_num, request_id, ts, "
            "model_id, reply_latency_s) VALUES (%s, %s, %s, %s, %s, %s)",
            [(fid, line, req, ts, mid, lat)
             for (_fk, line, req, ts, _m, lat) in seeded_rows],
        )
    return file_key, line_nums


def _cache_rows(body: dict) -> list[dict]:
    return [row for name in _CACHE_ROW_LISTS for row in body[name]]


def _assert_cache_lists_populated(body: dict) -> None:
    for name in _CACHE_ROW_LISTS:
        assert body[name], f"{name} should contain a seeded top row"


def _assert_public_cache_keys(body: dict) -> None:
    _assert_cache_lists_populated(body)
    for name in _CACHE_ROW_LISTS:
        assert all("file_key" in row for row in body[name])
        assert any(row["file_key"] == _PUBLIC_AGENT_KEY for row in body[name])


def _assert_guest_cache_keys_removed(body: dict) -> None:
    _assert_cache_lists_populated(body)
    for row in _cache_rows(body):
        assert "file_key" not in row
        assert "request_id" in row


def test_cache_guest_redaction_preserves_cached_payload_in_both_orders(
    app_with_fresh_data, guest_client
):
    """Guest stripping copies rows, preserving the shared cached payload."""
    file_key = _fixture_agent_key()
    line_num = 990002
    response_cache.clear()
    try:
        with psycopg.connect(os.environ["CLAUDIT_DATABASE_URL_VIZ"]) as conn:
            fid = dims_seed.file_id(conn, file_key)
            mid = dims_seed.dim_id(conn, "dim_model",
                                   "issue-244-cache-seed")
            conn.execute(
                "INSERT INTO records (file_id, line_num, request_id, ts, "
                "model_id, output_tokens, cache_creation_tokens, "
                "cache_read_tokens) "
                "VALUES (%s, %s, %s, %s, %s, 800000000, 700000000, "
                "600000000)",
                (fid, line_num, str(uuid.uuid4()),
                 datetime.now(timezone.utc), mid),
            )
            # Both halves of the pair again: the cache read INNER-joins
            # the sidecar, and this test also caches the guest payload.
            conn.execute(
                "INSERT INTO records_price (file_id, line_num) VALUES (%s, %s)",
                (fid, line_num),
            )

        guest_first = guest_client.get("/api/cache?range=3650d")
        public_after_guest = app_with_fresh_data.get("/api/cache?range=3650d")
        assert guest_first.status_code == public_after_guest.status_code == 200
        _assert_guest_cache_keys_removed(guest_first.json())
        _assert_public_cache_keys(public_after_guest.json())

        response_cache.clear()
        public_first = app_with_fresh_data.get("/api/cache?range=3650d")
        guest_after_public = guest_client.get("/api/cache?range=3650d")
        assert public_first.status_code == guest_after_public.status_code == 200
        _assert_public_cache_keys(public_first.json())
        _assert_guest_cache_keys_removed(guest_after_public.json())
    finally:
        _delete_seeded_records(file_key, [line_num])
        response_cache.clear()


@pytest.mark.parametrize("source", ["rollup", "live"], ids=["rollup", "live"])
def test_reply_latency_guest_strip_seeded_outliers(
    app_with_fresh_data, guest_client, source
):
    if source == "rollup":
        bucket_s, line_num = _seed_rollup_latency_outlier()
        seeded_lines = {line_num}
        query = "/api/reply-latency?range=3650d"
    else:
        file_key, line_nums = _seed_live_latency_outliers()
        seeded_lines = {line_nums[0], line_nums[-1]}
        query = "/api/reply-latency?range=24h"
    response_cache.clear()
    try:
        public = app_with_fresh_data.get(query)
        guest = guest_client.get(query)
        assert public.status_code == guest.status_code == 200
        public_body = public.json()
        if source == "rollup":
            assert public_body["bucket_s"] == bucket_s
        else:
            assert public_body["bucket_s"] < 3600
        public_rows = public_body["outliers"]
        guest_rows = guest.json()["outliers"]
        assert public_rows
        assert guest_rows
        assert {row["line"] for row in public_rows} & seeded_lines
        assert {row["line"] for row in guest_rows} & seeded_lines
        assert all("file_key" in row for row in public_rows)
        for row in public_rows:
            if source == "live" or row["line"] in seeded_lines:
                assert row["file_key"] == _PUBLIC_AGENT_KEY
        assert all("file_key" not in row for row in guest_rows)
    finally:
        if source == "rollup":
            _delete_rollup_latency_outlier()
        else:
            _delete_seeded_records(file_key, line_nums)
        response_cache.clear()
