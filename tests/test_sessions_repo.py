"""web_sessions repository — record, lookup, revoke.

Uses the shared `auth_db` fixture from tests/conftest.py. The imports are
top-level (like tests/test_session.py): pytest loads conftest.py — and
therefore the test DSN setdefaults — before this module body runs.
"""
import contextlib

import pytest
from fastapi.testclient import TestClient

from backend import app as app_mod
from backend import db, sessions_repo


def test_record_then_active(auth_db):
    sessions_repo.record_session(7, "nonce-a", "curl/8", "10.0.0.1")
    assert sessions_repo.is_session_active("nonce-a") is True


def test_unknown_nonce_is_not_active(auth_db):
    assert sessions_repo.is_session_active("never-issued") is False


def test_revoked_nonce_is_not_active(auth_db):
    sessions_repo.record_session(7, "nonce-b", "curl/8", "10.0.0.1")
    assert sessions_repo.revoke_session(7, "nonce-b") is True
    assert sessions_repo.is_session_active("nonce-b") is False


def test_revoke_only_affects_own_user(auth_db):
    sessions_repo.record_session(7, "nonce-c", "curl/8", "10.0.0.1")
    assert sessions_repo.revoke_session(8, "nonce-c") is False
    assert sessions_repo.is_session_active("nonce-c") is True


def test_list_excludes_revoked(auth_db):
    sessions_repo.record_session(9, "nonce-d", "firefox", "10.0.0.2")
    sessions_repo.record_session(9, "nonce-e", "chrome", "10.0.0.3")
    sessions_repo.revoke_session(9, "nonce-e")
    rows = sessions_repo.list_sessions(9)
    nonces = {r["nonce"] for r in rows}
    assert nonces == {"nonce-d"}
    assert rows[0]["user_agent"] == "firefox"


def test_list_newest_first(auth_db):
    # Two live rows: pins ORDER BY created_at DESC, which the revoked-row
    # test above cannot (it only ever sees one row). The older row is
    # back-dated explicitly so a same-transaction timestamp tie can't
    # make the order ambiguous.
    sessions_repo.record_session(11, "nonce-old", "old-ua", "10.0.0.4")
    sessions_repo.record_session(11, "nonce-new", "new-ua", "10.0.0.5")
    with db.auth_conn() as c:
        c.execute(
            "UPDATE web_sessions "
            "SET created_at = created_at - interval '1 hour' "
            "WHERE nonce = %s",
            ("nonce-old",),
        )
    rows = sessions_repo.list_sessions(11)
    assert [r["nonce"] for r in rows] == ["nonce-new", "nonce-old"]


class _VizConnStub:
    """Satisfies schema_check's claudit.files probe without a viz DB.

    DATABASE_URL_VIZ defaults to 'claudit_test', which does not exist when
    this module runs standalone, and schema_check() checks the viz side
    first — without this stub the web_sessions assertion is never reached.
    The stub lets only the auth side of schema_check run for real."""

    def execute(self, _query):
        return self

    def fetchone(self):
        return ("files",)


@contextlib.contextmanager
def _viz_conn_stub():
    yield _VizConnStub()


def test_schema_check_requires_web_sessions(auth_db, monkeypatch):
    monkeypatch.setattr(db, "viz_conn", _viz_conn_stub)
    # Rename rather than DROP: atomic, no shell-out, and restores nothing
    # but the one table. Every mutating step sits inside the try so the
    # finally always restores.
    try:
        with db.auth_conn() as c:
            c.execute(
                "ALTER TABLE web_sessions RENAME TO web_sessions_hidden"
            )
        with pytest.raises(RuntimeError, match="web_sessions"):
            db.schema_check()
    finally:
        with db.auth_conn() as c:
            c.execute(
                "ALTER TABLE IF EXISTS web_sessions_hidden "
                "RENAME TO web_sessions"
            )
    # A restore that silently failed would corrupt every later test in the
    # module and look like an unrelated failure — assert the table is back.
    with db.auth_conn() as c:
        row = c.execute(
            "SELECT to_regclass('public.web_sessions')"
        ).fetchone()
    assert row is not None and row[0] is not None


class _FakeScheduler:
    """Stand-in for the lifespan's BackgroundScheduler.

    The real scheduler's thread races its own shutdown under TestClient
    (intermittent JobLookupError warnings), and its startup-ingest job hits
    the nonexistent 'claudit_test' viz DB — both incidental to what the
    startup-wiring test pins."""

    def __init__(self, **_kwargs):
        pass

    def add_job(self, *_args, **_kwargs):
        pass

    def start(self):
        pass

    def shutdown(self, **_kwargs):
        pass


def test_app_refuses_to_start_without_web_sessions(auth_db, monkeypatch):
    """Without web_sessions the app must fail AT STARTUP, not later.

    A spy on schema_check only proves the call happens somewhere in the
    lifespan — a guard moved to shutdown would still pass. Entering the
    TestClient body proves startup completed, so the guard must raise
    before `entered` flips True."""
    monkeypatch.setattr(db, "viz_conn", _viz_conn_stub)
    monkeypatch.setattr(app_mod, "BackgroundScheduler", _FakeScheduler)
    # Defence-in-depth, not a fix for current behaviour: db.schema_check()
    # (backend/app.py:31) raises BEFORE events.set_loop(...) (app.py:32), so
    # in this test as it stands the lifespan never writes TestClient's event
    # loop into the module-global events._main_loop. The monkeypatch reset
    # is what saves the suite under a mutant that moves the guard below
    # set_loop: then the closed loop WOULD escape into events._main_loop and
    # every later run_ingest() would die with "RuntimeError: Event loop is
    # closed". monkeypatch restores the pre-test value at teardown either
    # way, so the closed loop can never leak into later tests.
    monkeypatch.setattr(app_mod.events, "_main_loop", None)
    monkeypatch.setattr(app_mod.events, "_shutdown_event", None)
    entered = False
    try:
        with db.auth_conn() as c:
            c.execute(
                "ALTER TABLE web_sessions RENAME TO web_sessions_hidden"
            )
        with pytest.raises(RuntimeError, match="web_sessions"):
            with TestClient(app_mod.app):
                entered = True
        assert entered is False, (
            "startup completed; the guard did not abort boot"
        )
    finally:
        with db.auth_conn() as c:
            c.execute(
                "ALTER TABLE IF EXISTS web_sessions_hidden "
                "RENAME TO web_sessions"
            )
    # A restore that silently failed would corrupt every later test in the
    # module and look like an unrelated failure — assert the table is back.
    with db.auth_conn() as c:
        row = c.execute(
            "SELECT to_regclass('public.web_sessions')"
        ).fetchone()
    assert row is not None and row[0] is not None
