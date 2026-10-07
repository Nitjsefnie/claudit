# Ordering is load-bearing: the DATABASE_URL_VIZ setdefault below MUST run
# before an import reaches backend.app (directly, or transitively via
# backend.api/db/etc.). backend.app loads the repository-root .env after
# importing backend.db and before its other backend modules; that .env pins
# DATABASE_URL_VIZ at the live production `claudit` database. Since
# db.load_dotenv only ever os.environ.setdefault()s (never overwrites), the
# test value wins when it is set first. backend.cache and backend.pricing are
# safe to import above it: both are stdlib-only and never touch the env; so is
# tests.scratch_db, which imports no backend module at load.
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from backend import cache, pricing, rate_fingerprint
from tests import scratch_db

# An exported DATABASE_URL_VIZ wins; fixtures that create their own
# database take a run-unique name from scratch_db either way.
os.environ.setdefault("DATABASE_URL_VIZ", f"postgresql:///{scratch_db.db_name()}")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# ...and this directory, so one test module can import another's fixture
# instead of duplicating an expensive fresh-DB + mini-R2 setup.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Force file-mode R2 for unit tests; pytest never hits real R2.
os.environ.setdefault("R2_ENDPOINT", "file:///tmp/sv-test-r2/")
os.environ.setdefault("R2_BUCKET", "claude")
os.environ.setdefault("R2_ACCOUNT_ID", "")
os.environ.setdefault("R2_ACCESS_KEY_ID", "")
os.environ.setdefault("R2_SECRET_ACCESS_KEY", "")
# The blob cache's off default must not rest on the host env happening
# to leave it unset (issue #684): a developer's exported cache
# directory would otherwise answer every fetch's consult.
os.environ.pop("R2_BLOB_CACHE", None)
os.environ.setdefault("ADMIN_TOKEN", "test-admin")
# Keep settings captured during app imports deterministic in the test suite.
# Exported values still win because these are set with setdefault.
os.environ.setdefault("EXPORT_PYTHON", "/usr/bin/python3")
os.environ.setdefault("CLAUDIT_TIMING", "0")
# The in-process pipeline by default: forked parse children under pytest
# duplicate capture state and slow every ingest test. The process-pool
# tests opt in explicitly (test_ingest.py, issue #309).
os.environ["INGEST_PARSE_PROCESSES"] = "1"
os.environ["INGEST_PERSIST_THREADS"] = "1"
# TestClient runs over plain HTTP — Secure-flag cookies would never come back.
os.environ.setdefault("COOKIE_SECURE", "0")
# No background cache warming under test: a warm queued by run_ingest
# outlives the fixture that created its DB, and its queries then race the
# teardown that drops it — producing failures in unrelated tests.
os.environ["CLAUDIT_WARM_CACHE"] = "0"

# The seam's own env name, spelled once here. (Not CLAUDIT_TEST_NOW: the
# scratch-DB guard forbids test sources naming claudit_test*, strings
# included.)
TEST_NOW_ENV = "SEAM_NOW"


def seam_now() -> datetime:
    """The suite's injectable clock (SV-TEST-DATA: a verdict may not
    depend on the wall clock).

    The refresh bot appends rate stamps hourly, so a test that prices
    records at datetime.now() reads whichever entry is in force at that
    time of day — its verdict can flip with the clock even on a fixed
    seed. Tests whose records carry runtime-now timestamps take their
    instant from here; SEAM_NOW (epoch seconds, or an ISO-8601 instant —
    UTC when unzoned) fixes it, and unset, the real clock answers.
    """
    raw = os.environ.get(TEST_NOW_ENV)
    if not raw:
        return datetime.now(timezone.utc)
    text = raw.strip()
    try:
        return datetime.fromtimestamp(int(text), tz=timezone.utc)
    except ValueError:
        pass
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@pytest.fixture
def synthetic_dated_rate(monkeypatch):
    """Install a made-up dated-rate window for the duration of one test.

    SV-DATED-RATES requires the models-table window machinery to keep
    working for the next promotion, but the claude rows are tracked vendor
    rows since the vendor migration (issue #851), so the models table
    holds no claude row a live window could hang on. The fixture installs
    its OWN models-table row and window — synthetic rates, unlike any real
    price, so a test asserting against them can never be mistaken for a
    pricing fact.
    """
    cutover = datetime(2026, 9, 1, tzinfo=timezone.utc)
    before = {
        "fresh": 9.00, "create_5m": 11.25, "create_1h": 18.00,
        "read": 0.90, "output": 45.00,
    }
    after = {
        "fresh": 3.30, "create_5m": 4.125, "create_1h": 6.60,
        "read": 0.33, "output": 16.50,
    }
    monkeypatch.setattr(pricing, "MODEL_RATES", {"claude-sonnet-5": after})
    monkeypatch.setattr(pricing, "DATED_RATES", {"claude-sonnet-5": [(cutover, before)]})
    monkeypatch.setattr(pricing, "RATE_EPOCHS", [cutover])

    return SimpleNamespace(
        model="claude-sonnet-5",
        cutover=cutover,
        before=before,
        after=after,
    )


@pytest.fixture
def synthetic_provider_dated_rate(monkeypatch):
    """Install a made-up dated-rate row for one (model, provider) pair.

    SV-PROVIDER-RATES prices a record naming its serving host from
    PROVIDER_RATES, keyed (model, host), with SV-DATED-RATES windows and a
    row start on top. The live provider rows are single-entry today — every
    host so far has kept one price — so the machinery would go untested
    until the first refresh appends a move or first-sees a host. Tests
    install their own row here rather than depending on live data, the
    same reason the model side uses synthetic_dated_rate.

    The rates are deliberately unlike any real price so a test asserting
    against them can never be mistaken for a pricing fact.

    RATE_EPOCHS is deliberately NOT patched: a test that needs the union
    must see the one computed from the real tables (the model-side fixture
    patches it only to pin the exposed list, never to feed a fold).
    """
    cutover = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    before = {
        "fresh": 7.40, "create_5m": 9.25, "create_1h": 14.80,
        "read": 0.74, "output": 37.00,
    }
    after = {
        "fresh": 3.20, "create_5m": 4.00, "create_1h": 6.40,
        "read": 0.32, "output": 16.00,
    }
    row = ("acme/acme-9", "HostCo")
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {row: after})
    monkeypatch.setattr(
        pricing, "PROVIDER_DATED_RATES", {row: [(cutover, before)]})
    monkeypatch.setattr(pricing, "PROVIDER_STARTS", {row: start})

    return SimpleNamespace(
        model="acme/acme-9",
        host="HostCo",
        cutover=cutover,
        start=start,
        before=before,
        after=after,
    )


@pytest.fixture(autouse=True)
def _reset_response_cache():
    # response_cache is a process-global. Two tests with different fixtures
    # but identical query params would otherwise read each other's payloads.
    cache.response_cache.clear()
    yield
    cache.response_cache.clear()


@pytest.fixture(autouse=True)
def _isolate_transcript_cache(monkeypatch):
    """Keep one test's transcript reads out of the process-wide LRU."""
    original = cache.transcript_cache
    monkeypatch.setattr(
        cache,
        "transcript_cache",
        type(original)(
            max_bytes=original.max_bytes,
            idle_seconds=original.idle_seconds,
        ),
    )


@pytest.fixture(autouse=True)
def _fresh_match_key_cache():
    """pricing._match_key memoizes per normalised id against the rate
    table loaded at import (issue #350), and rate_fingerprint memoizes
    per pair against the same tables (issue #351). Tests patch the
    tables with synthetic rows; a cached entry computed under a patch
    must never outlive it, so every test starts and ends with empty
    caches. A test that patches the tables MID-TEST (after an ingest
    has already stamped fingerprints) must clear the fingerprint memo
    itself at the mutation site — without it the pass reads pre-patch
    fps and fails conservative-but-confusing (see the parity and
    cache-gate tests)."""
    pricing._MATCH_KEY_CACHE.clear()  # pylint: disable=protected-access
    rate_fingerprint.clear_fingerprint_cache()
    yield
    pricing._MATCH_KEY_CACHE.clear()  # pylint: disable=protected-access
    rate_fingerprint.clear_fingerprint_cache()


def pytest_collection_modifyitems(config, items):
    # The mechanical db/portable split (issue #27): every test whose
    # fixture closure reaches a registered DB fixture is marked `db`, so
    # the portable CI matrix can run -m "not db" with no server at all.
    # Imported in the hook because only collection needs it; same shape
    # as scratch_viz_database's local backend import.
    from tests.db_marker import mark_db_items  # pylint: disable=import-outside-toplevel

    mark_db_items(items)


def pytest_sessionstart(session):
    # Lease first, so no other run's sweep can take this run's databases;
    # then clear leftovers of runs killed before their finalizer ran.
    try:
        scratch_db.hold_run_lease()
        scratch_db.sweep_stale_databases()
    except psycopg.OperationalError:
        pass  # no reachable server: this run touches no database either


def pytest_sessionfinish(session, exitstatus):
    # Backstop for fixtures that failed before their own teardown.
    try:
        scratch_db.drop_run_databases()
    except psycopg.OperationalError:
        pass
