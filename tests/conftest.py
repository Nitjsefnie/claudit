# Ordering is load-bearing: the DATABASE_URL_VIZ setdefault below MUST run
# before anything imports backend.app (directly, or transitively via
# backend.api/db/etc.), because backend/app.py:20 calls
# db.load_dotenv(".env") at import time, and .env pins DATABASE_URL_VIZ at
# the live production `claudit` database. Since db.load_dotenv only ever
# os.environ.setdefault()s (never overwrites), the first setdefault to run
# for this key wins the race for the whole test process. backend.cache is
# safe to import above it: it is stdlib-only and never touches the env.
# DATABASE_URL_AUTH gets the same guard: .env pins it at the live auth
# database, and without this setdefault any test that touches auth outside
# the auth_db fixture (e.g. a `pytest -k` partial run) would read from — and
# once login records sessions, WRITE to — the production database.
#
# pylint: disable=import-outside-toplevel,redefined-outer-name
# import-outside-toplevel: the backend.* imports inside the fixtures below
# are deferred ON PURPOSE — the setdefaults above must land in os.environ
# before any backend module is imported, or the guard is void. Do not hoist
# them. redefined-outer-name: the fixture-argument pattern (a fixture taking
# another fixture by name) is standard pytest; the fixture names are part of
# the shared contract consumed by other test modules and must not change.
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend import cache

_TEST_AUTH_DB = "claudit_test_auth"

os.environ.setdefault("DATABASE_URL_VIZ", "postgresql:///claudit_test")
os.environ.setdefault("DATABASE_URL_AUTH", f"postgresql:///{_TEST_AUTH_DB}")

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
os.environ.setdefault("PARSER_VERSION", "test")
os.environ.setdefault("ADMIN_TOKEN", "test-admin")
# TestClient runs over plain HTTP — Secure-flag cookies would never come
# back. A forced assignment, NOT a setdefault: an ambient exported
# COOKIE_SECURE=1 must not be able to break the suite.
os.environ["COOKIE_SECURE"] = "0"
# SESSION_COOKIE_DOMAIN from .env (or the ambient environment) would make
# the app issue domain-scoped cookies the test client's jar never returns
# for the `testserver` host, failing auth tests for reasons unrelated to
# any defect. Force it empty: backend/session.py maps "" to None, so
# cookies stay host-only. A forced assignment, NOT a setdefault, and it
# must run before any backend import so load_dotenv's setdefault in
# backend/app.py cannot win the race. Tests that exercise the rollout
# boundary monkeypatch the variable themselves and are unaffected.
os.environ["SESSION_COOKIE_DOMAIN"] = ""
# No background cache warming under test: a warm queued by run_ingest
# outlives the fixture that created its DB, and its queries then race the
# teardown that drops it — producing failures in unrelated tests.
os.environ["CLAUDIT_WARM_CACHE"] = "0"

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_response_cache():
    # response_cache is a process-global. Two tests with different fixtures
    # but identical query params would otherwise read each other's payloads.
    cache.response_cache.clear()
    yield
    cache.response_cache.clear()


_TEST_SECRET = "fixture-session-secret-0123456789"
_TEST_UID = 4242


@pytest.fixture(scope="module")
def auth_db():
    """A scratch auth DB with web_sessions and one seeded user row.

    Module-scoped: rows written by one test persist for the rest of the
    module. Two rules keep tests from seeing each other's state: nonces
    must be disjoint per test, always; and any test that asserts over
    list_sessions must use a private user_id (not _TEST_UID), since that
    query is keyed by user alone."""
    os.system(f"dropdb --if-exists {_TEST_AUTH_DB} 2>/dev/null")
    os.system(f"createdb {_TEST_AUTH_DB} 2>/dev/null")
    subprocess.run(
        ["psql", _TEST_AUTH_DB, "-c",
         "CREATE TABLE users (user_id BIGINT PRIMARY KEY, config JSONB NOT NULL)"],
        check=True, stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        ["psql", _TEST_AUTH_DB, "-f", str(_REPO_ROOT / "backend/schema_auth.sql")],
        check=True, stdout=subprocess.DEVNULL,
    )
    os.environ["DATABASE_URL_AUTH"] = f"postgresql:///{_TEST_AUTH_DB}"
    from backend import db
    db.reset_auth_pool()
    yield
    db.reset_auth_pool()
    os.system(f"dropdb --if-exists {_TEST_AUTH_DB} 2>/dev/null")


@pytest.fixture
def seeded_user(auth_db):
    """(user_id, session_secret) for a user row that exists in the auth DB."""
    import json
    from backend import auth, db, session as session_mod
    config = {session_mod.WEB_SESSION_SECRET_KEY: _TEST_SECRET}
    auth.set_web_password(config, "fixture-password")
    with db.auth_conn() as c:
        c.execute(
            "INSERT INTO users (user_id, config) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (user_id) DO UPDATE SET config = EXCLUDED.config",
            (_TEST_UID, json.dumps(config)),
        )
    return _TEST_UID, _TEST_SECRET


@pytest.fixture
def logged_in_client(seeded_user):
    """TestClient that has completed a real POST /login."""
    from fastapi.testclient import TestClient
    from backend.app import app
    uid, _ = seeded_user
    client = TestClient(app)
    resp = client.post(
        "/login",
        data={"user_id": str(uid), "password": "fixture-password"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return client


@pytest.fixture
def guest_client(auth_db):
    """TestClient holding a guest session cookie."""
    from fastapi.testclient import TestClient
    from backend.app import app
    client = TestClient(app)
    resp = client.post("/login/guest", follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return client
