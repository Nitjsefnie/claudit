"""The bucket segment of a stored file_key never reaches the browser.

file_key is stored bucket-qualified (`<bucket>/<object-key>`,
SV-FILES-RECORDS), and bucket names are infrastructure: a deploy may
read buckets the client must not learn about. With R2_BUCKET naming two
buckets and data in BOTH, no endpoint response body may contain either
bucket name as a path prefix, while the object-key part stays present.

Fixture pattern reused from tests/test_multi_bucket.py.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, app as app_mod, cache, db, ingest, r2
from tests import scratch_db


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
    yield from scratch_db.scratch_viz_database(monkeypatch, "bucket_redact")


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
    # /health is public (session._AUTH_PUBLIC_PATHS) and its error field
    # mirrors ingest_runs.error — include it in this module's sweep.
    a.get("/health")(app_mod.health)
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
        "/health",
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


# ---------------------------------------------------------------------------
# The PUBLIC error channel: /health serves ingest_runs.error, whose keys
# are bucket-qualified and whose fatal text can name a bucket or the
# mirror root
# ---------------------------------------------------------------------------


def test_redact_strips_bucket_names_and_mirror_root(monkeypatch, tmp_path):
    """r2.redact is the free-text net: the mirror root and every
    configured bucket name — path segment, quoted !r, list element after
    a separator, or key prefix — become placeholders."""
    monkeypatch.setenv("R2_BUCKET", "alpha+beta")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/mirror/")
    text = (
        "FileNotFoundError: no mirror directory for bucket 'beta'; "
        f"cannot stat {tmp_path}/mirror/alpha/projS/sessS/sessS.jsonl "
        "after 3 attempts: alpha/projS/a.jsonl, beta/projS/b.jsonl"
    )
    out = r2.redact(text) or ""
    assert "alpha" not in out and "beta" not in out
    assert "<mirror>" in out and "<bucket>" in out


def test_failure_summary_publicises_keys(monkeypatch):
    """failure_summary feeds ingest_runs.error, which the public /health
    serves: its keys are presentation, so they go through public_key."""
    monkeypatch.setenv("R2_BUCKET", "alpha+beta")
    out = ingest.failure_summary(
        [("alpha/projS/sessS/sessS.jsonl", "RuntimeError: dropped")])
    assert out == "1 object failed after retries: projS/sessS/sessS.jsonl"


def test_health_per_object_failure_serves_public_keys(
        redact_app, tmp_path, monkeypatch):
    """A partial run is ROUTINE. Its ingest_runs.error names the failed
    objects, and /health serves that field unauthenticated — so the keys
    must come out in their public form and the failure text must not
    name a bucket."""
    real_fetch = ingest._fetch_with_retry  # pylint: disable=protected-access

    def flaky(key: str) -> bytes:
        if key.startswith("alpha/"):
            raise RuntimeError(
                "GET beta/projS/sessS/sessS.jsonl dropped on mirror")
        return real_fetch(key)

    # Force a reparse of the alpha file (else the run has nothing to
    # fetch and the flaky fetch never fires).
    (tmp_path / "alpha" / _OBJ_KEY).touch()
    monkeypatch.setattr(ingest, "_fetch_with_retry", flaky)
    result = ingest.run_ingest(trigger="manual")
    monkeypatch.undo()
    assert result["failed"] == 1
    assert result["error"] is not None

    a = FastAPI()
    a.include_router(api.router)
    a.get("/health")(app_mod.health)
    body = TestClient(a).get("/health").text
    assert "alpha/" not in body, "bucket-qualified failure key leaked"
    assert "beta/" not in body, "bucket name in failure text leaked"
    assert "projS/sessS/sessS.jsonl" in body, "public key kept for triage"


def test_health_fatal_never_names_a_bucket(redact_app, monkeypatch):
    """A whole-run fatal (mirror FileNotFoundError, S3 error) names the
    bucket in its message. The stored fatal text is redacted of every
    configured bucket name; the full exception stays in the logs."""
    def broken(prefix: str = ""):
        raise FileNotFoundError(
            "no mirror directory for bucket 'beta'; listing refused")

    monkeypatch.setattr(ingest.r2, "list_keys", broken)
    result = ingest.run_ingest(trigger="manual")
    monkeypatch.undo()
    assert result["error"] is not None
    assert "FileNotFoundError" in result["error"], "type name kept"
    assert "beta" not in result["error"], "fatal text must be redacted"

    a = FastAPI()
    a.include_router(api.router)
    a.get("/health")(app_mod.health)
    body = TestClient(a).get("/health").text
    assert "beta" not in body
    assert "FileNotFoundError" in body, "the type name is safe to serve"


def test_health_db_failure_is_generic(redact_app, monkeypatch):
    """The DB-failure branch of /health must not echo driver exception
    text (which can name hosts, databases, buckets) on a public
    endpoint; details go to the logs."""
    def boom():
        raise RuntimeError(
            "connection failed: host 'alpha-db', database 'beta-rows'")

    monkeypatch.setattr(db, "viz_conn", boom)
    a = FastAPI()
    a.include_router(api.router)
    a.get("/health")(app_mod.health)
    body = TestClient(a).get("/health").text
    monkeypatch.undo()
    assert "alpha-db" not in body and "beta-rows" not in body
    assert "database unavailable" in body
