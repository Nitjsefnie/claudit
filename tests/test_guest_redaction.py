from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from backend import api
from backend.cache import response_cache
from backend.constants import LATENCY_BUCKETS

pytest_plugins = ("tests.test_api",)

_CACHE_ROW_LISTS = ("top_output", "top_cache_create", "top_cache_read")
_PUBLIC_AGENT_KEY = "projB/sess-C/agent-aaaa.jsonl"
_ROLLUP_MODEL = "issue-244-rollup-seed"
_LIVE_MODEL = "issue-244-live-seed"


@pytest.fixture(scope="module", name="guest_client")
def guest_client_fixture():
    """Mount the API router in a fresh app with the real guest state flag."""
    app = FastAPI()

    @app.middleware("http")
    async def set_guest_flag(request: Request, call_next):
        request.state.is_guest = True
        return await call_next(request)

    app.include_router(api.router)
    with TestClient(app) as client:
        yield client


def _fixture_agent_key() -> str:
    """Read the stored R2 key for the mini fixture's known agent file."""
    with psycopg.connect(os.environ["DATABASE_URL_VIZ"]) as conn:
        row = conn.execute(
            "SELECT file_key FROM files "
            "WHERE project_id = 'projB' AND session_id = 'sess-C' "
            "AND file_key LIKE '%/agent-aaaa.jsonl' LIMIT 1"
        ).fetchone()
    assert row is not None, "mini fixture agent file should exist"
    return row[0]


def _delete_seeded_records(file_key: str, line_nums: list[int]) -> None:
    with psycopg.connect(os.environ["DATABASE_URL_VIZ"]) as conn:
        conn.execute(
            "DELETE FROM records WHERE file_key = %s AND line_num = ANY(%s)",
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
    with psycopg.connect(os.environ["DATABASE_URL_VIZ"]) as conn:
        conn.execute(
            "INSERT INTO latency_rollup "
            "(bucket_s, bucket, project_id, model, n, p10, p50, p90, outliers) "
            "VALUES (%s, %s, '', %s, 100, 1, 5, 10, %s)",
            (bucket_s, bucket, _ROLLUP_MODEL, Jsonb([outlier])),
        )
    return bucket_s, line_num


def _delete_rollup_latency_outlier() -> None:
    with psycopg.connect(os.environ["DATABASE_URL_VIZ"]) as conn:
        conn.execute(
            "DELETE FROM latency_rollup WHERE model = %s", (_ROLLUP_MODEL,)
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
    with psycopg.connect(os.environ["DATABASE_URL_VIZ"]) as conn:
        conn.cursor().executemany(
            "INSERT INTO records (file_key, line_num, request_id, ts, model, "
            "reply_latency_s) VALUES (%s, %s, %s, %s, %s, %s)",
            seeded_rows,
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


def test_cache_guest_responses_remove_project_bearing_file_keys(
    app_with_fresh_data, guest_client
):
    """Guests lose the file identity from each cache top-request list."""
    file_key = _fixture_agent_key()
    line_num = 990001
    response_cache.clear()
    try:
        with psycopg.connect(os.environ["DATABASE_URL_VIZ"]) as conn:
            conn.execute(
                "INSERT INTO records (file_key, line_num, request_id, ts, model, "
                "output_tokens, cache_creation_tokens, cache_read_tokens) "
                "VALUES (%s, %s, %s, %s, %s, 800000000, 700000000, 600000000)",
                (file_key, line_num, str(uuid.uuid4()), datetime.now(timezone.utc),
                 "issue-244-cache-seed"),
            )
        guest = guest_client.get("/api/cache?range=3650d")
        public = app_with_fresh_data.get("/api/cache?range=3650d")

        assert guest.status_code == public.status_code == 200
        _assert_guest_cache_keys_removed(guest.json())
        _assert_public_cache_keys(public.json())
    finally:
        _delete_seeded_records(file_key, [line_num])
        response_cache.clear()


def test_cache_guest_redaction_preserves_cached_payload_in_both_orders(
    app_with_fresh_data, guest_client
):
    """Guest stripping copies rows, preserving the shared cached payload."""
    file_key = _fixture_agent_key()
    line_num = 990002
    response_cache.clear()
    try:
        with psycopg.connect(os.environ["DATABASE_URL_VIZ"]) as conn:
            conn.execute(
                "INSERT INTO records (file_key, line_num, request_id, ts, model, "
                "output_tokens, cache_creation_tokens, cache_read_tokens) "
                "VALUES (%s, %s, %s, %s, %s, 800000000, 700000000, 600000000)",
                (file_key, line_num, str(uuid.uuid4()), datetime.now(timezone.utc),
                 "issue-244-cache-seed"),
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


def test_reply_latency_guest_strip_rolls_up_seeded_outliers(
    app_with_fresh_data, guest_client
):
    """Guests lose file_key from real rollup outliers while public rows keep it."""
    bucket_s, line_num = _seed_rollup_latency_outlier()
    response_cache.clear()
    try:
        query = "/api/reply-latency?range=3650d"
        public = app_with_fresh_data.get(query)
        guest = guest_client.get(query)
        assert public.status_code == guest.status_code == 200
        public_body = public.json()
        assert public_body["bucket_s"] == bucket_s
        public_rows = public_body["outliers"]
        guest_rows = guest.json()["outliers"]
        public_row = next(row for row in public_rows if row["line"] == line_num)
        guest_row = next(row for row in guest_rows if row["line"] == line_num)
        assert public_row["file_key"] == _PUBLIC_AGENT_KEY
        assert all("file_key" in row for row in public_rows)
        assert "file_key" not in guest_row
        assert all("file_key" not in row for row in guest_rows)
    finally:
        _delete_rollup_latency_outlier()
        response_cache.clear()


def test_reply_latency_guest_strip_live_seeded_outliers(
    app_with_fresh_data, guest_client
):
    """Guests lose file_key from live outliers after the 100-row bucket threshold."""
    file_key, line_nums = _seed_live_latency_outliers()
    response_cache.clear()
    try:
        query = "/api/reply-latency?range=24h"
        public = app_with_fresh_data.get(query)
        guest = guest_client.get(query)
        assert public.status_code == guest.status_code == 200
        public_body = public.json()
        guest_body = guest.json()
        public_rows = public_body["outliers"]
        guest_rows = guest_body["outliers"]
        assert public_body["bucket_s"] < 3600
        assert public_rows
        assert all(row["file_key"] == _PUBLIC_AGENT_KEY for row in public_rows)
        assert {row["line"] for row in public_rows} & {line_nums[0], line_nums[-1]}
        assert guest_rows
        assert all("file_key" not in row for row in guest_rows)
        assert {row["line"] for row in guest_rows} & {line_nums[0], line_nums[-1]}
    finally:
        _delete_seeded_records(file_key, line_nums)
        response_cache.clear()


def test_context_growth_session_guest_response_omits_file_key(
    app_with_data, guest_client
):
    """Guests receive session context data without the main file identity."""
    path = "/api/context-growth/session/sess-A"
    guest = guest_client.get(path)
    public = app_with_data.get(path)

    assert guest.status_code == public.status_code == 200
    public_body = public.json()
    guest_body = guest.json()
    assert public_body["file_key"] == "projA/sess-A/sess-A.jsonl"
    assert "file_key" not in guest_body
    assert guest_body == {key: value for key, value in public_body.items()
                          if key != "file_key"}
