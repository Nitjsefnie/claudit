# Ordering is load-bearing: the DATABASE_URL_VIZ setdefault below MUST run
# before anything imports backend.app (directly, or transitively via
# backend.api/db/etc.), because backend/app.py:20 calls
# db.load_dotenv(".env") at import time, and .env pins DATABASE_URL_VIZ at
# the live production `claudit` database. Since db.load_dotenv only ever
# os.environ.setdefault()s (never overwrites), the first setdefault to run
# for this key wins the race for the whole test process. backend.cache and
# backend.pricing are safe to import above it: both are stdlib-only and
# never touch the env; so is tests.scratch_db, which imports no backend
# module at load.
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from backend import cache, pricing
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
os.environ.setdefault("ADMIN_TOKEN", "test-admin")
# TestClient runs over plain HTTP — Secure-flag cookies would never come back.
os.environ.setdefault("COOKIE_SECURE", "0")
# No background cache warming under test: a warm queued by run_ingest
# outlives the fixture that created its DB, and its queries then race the
# teardown that drops it — producing failures in unrelated tests.
os.environ["CLAUDIT_WARM_CACHE"] = "0"


@pytest.fixture
def synthetic_dated_rate(monkeypatch):
    """Install a made-up dated-rate window for the duration of one test.

    pricing.DATED_RATES is empty in production right now (Sonnet 5's launch
    price became its standard price), but SV-DATED-RATES requires the
    machinery to keep working for the next promotion. Tests that exercise
    it install their own window here rather than depending on a live one —
    otherwise the code path is untested until the day a promotion lands.

    The rates are deliberately unlike any real price so a test asserting
    against them can never be mistaken for a pricing fact.
    """
    cutover = datetime(2026, 9, 1, tzinfo=timezone.utc)
    before = {
        "fresh": 9.00, "create_5m": 11.25, "create_1h": 18.00,
        "read": 0.90, "output": 45.00,
    }
    monkeypatch.setattr(pricing, "DATED_RATES", {"claude-sonnet-5": [(cutover, before)]})
    monkeypatch.setattr(pricing, "RATE_EPOCHS", [cutover])

    return SimpleNamespace(
        model="claude-sonnet-5",
        cutover=cutover,
        before=before,
        after=pricing.MODEL_RATES["claude-sonnet-5"],
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
