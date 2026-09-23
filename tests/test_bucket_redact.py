"""The bucket segment of a stored file_key never reaches the browser.

file_key is stored bucket-qualified (`<bucket>/<object-key>`,
SV-FILES-RECORDS), and bucket names are infrastructure: a deploy may
read buckets the client must not learn about. With R2_BUCKET naming two
buckets and data in BOTH, no endpoint response body may contain either
bucket name as a path prefix, while the object-key part stays present.

Fixture pattern reused from tests/test_multi_bucket.py.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, cache, db, ingest, r2

_REPO_ROOT = Path(__file__).resolve().parent.parent

_OBJ_KEY = "projS/sessS/sessS.jsonl"
_N_TURNS = 120  # >100: /api/reply-latency outliers need bucket_n >= 100


def _iso_ts(epoch_s: int) -> str:
    return (
        datetime.fromtimestamp(epoch_s, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _transcript() -> str:
    """_N_TURNS user→assistant turns, usage-bearing, with cache-create
    AND cache-read nonzero so all three /api/cache top lists fill.

    The turns start at the beginning of the NEXT 15-minute wall-clock
    bucket and run one second apart (~2 min total), so they all land in
    ONE 900s live-path latency bucket AND one 86400s rollup bucket —
    `bucket_n >= 100` holds on both /api/reply-latency paths without the
    test depending on where "now" sits inside the current bucket. The
    timestamps are slightly in the future, which every range filter
    admits (`ts >= since` has no upper bound).
    """
    base = (int(time.time()) // 900 + 1) * 900
    lines = []
    for i in range(_N_TURNS):
        lines.append(
            '{"type":"user","timestamp":"%s","uuid":"u%d",'
            '"message":{"role":"user","content":"q%d"}}'
            % (_iso_ts(base + i), i, i)
        )
        lines.append(
            '{"type":"assistant","timestamp":"%s","uuid":"a%d",'
            '"requestId":"req-%d","sessionId":"sessS",'
            '"message":{"role":"assistant","model":"claude-sonnet-4-5",'
            '"content":[{"type":"text","text":"ok %d"}],'
            '"usage":{"input_tokens":100,"output_tokens":%d,'
            '"cache_creation_input_tokens":%d,"cache_read_input_tokens":%d}}}'
            % (_iso_ts(base + i + 5), i, i, i, 200 + i, 5 + i % 3, 7 + i % 5)
        )
    return "\n".join(lines) + "\n"


@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    """Per-test schema reset on a separate DB (same shape as
    test_multi_bucket's fixture, kept local so this module stands
    alone)."""
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


@pytest.fixture(name="redact_app")
def _redact_app_fixture(fresh_db, tmp_path, monkeypatch):
    """Both buckets hold the SAME object key, so whichever row wins uuid
    dedup (and whichever main file a `LIMIT 1` picks) serves the same
    public form."""
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/")
    monkeypatch.setenv("R2_BUCKET", "alpha+beta")
    d = tmp_path / "alpha" / "projS" / "sessS"
    d.mkdir(parents=True)
    (d / "sessS.jsonl").write_text(_transcript())
    bd = tmp_path / "beta" / "projS" / "sessS"
    bd.mkdir(parents=True)
    (bd / "sessS.jsonl").write_text(_transcript())

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None, result["error"]

    # Response cache is process-global and keyed by endpoint+params only:
    # an entry another test module populated would serve foreign data.
    cache.response_cache.clear()

    a = FastAPI()
    a.include_router(api.router)
    yield TestClient(a)


# ---------------------------------------------------------------------------
# r2.public_key: the presentation rule, unit-tested
# ---------------------------------------------------------------------------


def test_public_key_strips_a_configured_bucket(monkeypatch):
    monkeypatch.setenv("R2_BUCKET", "alpha+beta")
    assert r2.public_key("alpha/proj/s/x.jsonl") == "proj/s/x.jsonl"
    assert r2.public_key("beta/proj/s/x.jsonl") == "proj/s/x.jsonl"


def test_public_key_keeps_an_unconfigured_first_segment(monkeypatch):
    """A key whose first segment is not a configured bucket (a legacy
    bare object key, or an already-public key) is returned unchanged."""
    monkeypatch.setenv("R2_BUCKET", "alpha")
    assert r2.public_key("proj/s/x.jsonl") == "proj/s/x.jsonl"


def test_public_key_edge_cases(monkeypatch):
    monkeypatch.setenv("R2_BUCKET", "alpha")
    assert r2.public_key("noslash") == "noslash"
    assert r2.public_key("") == ""
    assert r2.public_key(None) is None


# ---------------------------------------------------------------------------
# No endpoint serves a bucket prefix; the object key stays
# ---------------------------------------------------------------------------


def test_no_endpoint_serves_a_bucket_prefix(redact_app):
    endpoints = [
        "/api/dashboard?range=all",
        "/api/cache?range=all",
        "/api/reply-latency?range=all",
        "/api/reply-latency?range=2d",  # bucket 900s → the LIVE path
        "/api/sessions",
        "/api/sessions/sessS",
        "/api/context-growth/session/sessS",
    ]
    for url in endpoints:
        r = redact_app.get(url)
        assert r.status_code == 200, url
        assert "alpha/" not in r.text, f"bucket leak in {url}"
        assert "beta/" not in r.text, f"bucket leak in {url}"

    # The object-key part is still served — the field keeps its use.
    assert _OBJ_KEY in redact_app.get("/api/cache?range=all").text
    detail = redact_app.get("/api/sessions/sessS")
    assert detail.json()["r2_key"] == _OBJ_KEY
    growth = redact_app.get("/api/context-growth/session/sessS")
    assert growth.json()["file_key"] == _OBJ_KEY


def test_latency_outliers_present_and_redacted(redact_app):
    """Guards the redaction against passing vacuously: both latency
    paths must actually RETURN outliers here, and every one carries the
    public key."""
    for rng in ("all", "2d"):
        lat = redact_app.get(f"/api/reply-latency?range={rng}").json()
        assert lat["outliers"], f"no outliers at range={rng}"
        for o in lat["outliers"]:
            assert o["file_key"] == _OBJ_KEY


def test_cache_top_lists_present(redact_app):
    """All three /api/cache top lists have rows (so their file_key
    redaction is exercised, not skipped on empty input)."""
    top = redact_app.get("/api/cache?range=all").json()
    assert top["top_output"]
    assert top["top_cache_create"]
    assert top["top_cache_read"]
    for row in top["top_output"] + top["top_cache_create"] \
            + top["top_cache_read"]:
        assert row["file_key"] == _OBJ_KEY
