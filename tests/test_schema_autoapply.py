"""Startup applies backend/schema.sql, so a deploy cannot outrun its DB.

Issue #43: schema_check() only asserted that `files` existed, so a
checkout that pulled code writing a new column started clean and then
failed every ingest with UndefinedColumn. Applying the idempotent
schema at boot removes the divergence rather than reporting it.
"""
from __future__ import annotations

import asyncio
import logging
import types
from contextlib import nullcontext

import pytest

from test_api import _app_with_data_fixture

from backend import app as app_mod
from backend import db

__all__ = ["_app_with_data_fixture"]


def _columns(table: str) -> set[str]:
    with db.viz_conn() as c:
        return {r[0] for r in c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s", (table,)
        ).fetchall()}


def test_apply_schema_restores_a_dropped_column(app_with_data):
    """The #43 reproduction: a DB missing a column the code writes is
    repaired at startup instead of failing at the next ingest."""
    with db.viz_conn() as c:
        c.execute("ALTER TABLE tool_uses DROP COLUMN error_kind")
        c.commit()
    assert "error_kind" not in _columns("tool_uses")

    db.apply_schema()

    assert "error_kind" in _columns("tool_uses")


def test_apply_schema_restores_a_dropped_rollup(app_with_data):
    """Whole relations come back too, not only columns."""
    with db.viz_conn() as c:
        c.execute("DROP TABLE IF EXISTS dispatch_rollup")
        c.commit()

    db.apply_schema()

    with db.viz_conn() as c:
        row = c.execute(
            "SELECT to_regclass('public.dispatch_rollup')").fetchone()
    assert row is not None and row[0] is not None


def test_apply_schema_creates_user_session(app_with_data):
    """Issues #94/#108: the per-user session-secret table is part of the
    startup-applied schema, and re-applying leaves its rows alone."""
    with db.viz_conn() as c:
        c.execute(
            "INSERT INTO user_session (user_id, secret) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET secret = EXCLUDED.secret",
            (7, "pre-existing"),
        )
        c.commit()

    db.apply_schema()

    with db.viz_conn() as c:
        row = c.execute(
            "SELECT secret FROM user_session WHERE user_id = 7").fetchone()
    assert row is not None and row[0] == "pre-existing"


def test_apply_schema_is_idempotent_and_preserves_data(app_with_data):
    """Re-applying on every boot must not disturb existing rows."""
    with db.viz_conn() as c:
        before = c.execute("SELECT COUNT(*) FROM records").fetchone()
    assert before is not None and before[0] > 0

    db.apply_schema()
    db.apply_schema()

    with db.viz_conn() as c:
        after = c.execute("SELECT COUNT(*) FROM records").fetchone()
    assert after is not None and after[0] == before[0]


def test_schema_path_is_module_relative():
    """Resolved next to db.py, so the service's cwd is irrelevant."""
    assert db.SCHEMA_PATH.name == "schema.sql"
    assert db.SCHEMA_PATH.is_file()


def test_startup_applies_the_schema_before_checking_it(monkeypatch):
    """Wiring: lifespan() must converge the DB before asserting its shape.

    Order matters — checking first would fail a boot that apply_schema()
    was about to repair.
    """
    calls = []
    monkeypatch.setattr(app_mod.db, "apply_schema",
                        lambda: calls.append("apply"))
    monkeypatch.setattr(app_mod.db, "schema_check",
                        lambda: calls.append("check"))
    monkeypatch.setattr(app_mod.events, "set_loop", lambda _loop: None)

    class _Sched:
        def add_job(self, *a, **k):
            pass

        def start(self):
            pass

        def shutdown(self, wait=True):
            pass

    monkeypatch.setattr(app_mod, "BackgroundScheduler", lambda **k: _Sched())
    monkeypatch.setattr(app_mod.events, "signal_shutdown", lambda: None)

    async def _run():
        async with app_mod.lifespan(app_mod.app):
            pass

    asyncio.run(_run())
    assert calls == ["apply", "check"]


# ---------------------------------------------------------------------------
# Unlock failure must not mask the migration's own error (issue #154): the
# unlock (and the commit ending its transaction) run in apply_schema's
# finally, where a failure — an aborted transaction left by a failed DDL,
# or a connection that died mid-migration — used to REPLACE the error the
# caller should have seen.
# ---------------------------------------------------------------------------


class _SchemaConn:
    """Stands in for a pooled viz connection inside apply_schema: records
    every statement and fails selectively."""

    def __init__(self, fail_unlock_with=None, fail_ddl_with=None):
        self.sqls = []
        self._fail_unlock_with = fail_unlock_with
        self._fail_ddl_with = fail_ddl_with

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        if (self._fail_unlock_with is not None
                and sql.startswith("SELECT pg_advisory_unlock")):
            raise self._fail_unlock_with
        if (self._fail_ddl_with is not None
                and not sql.startswith("SELECT pg_advisory_")):
            raise self._fail_ddl_with
        return types.SimpleNamespace(fetchone=lambda: (True,))

    def commit(self):
        pass


def _fake_viz_conn(monkeypatch, conn):
    """Point db.viz_conn at `conn` — apply_schema's only connection need."""
    monkeypatch.setattr(db, "viz_conn", lambda: nullcontext(conn))


def test_apply_schema_unlock_failure_does_not_mask_the_ddl_error(monkeypatch):
    """A failed DDL leaves an aborted transaction behind, so the unlock in
    the finally fails on it — and that failure replaced the migration's
    own error at the caller. The migration's error is the diagnosable one
    and must survive; the cleanup failure is swallowed and logged, with
    the unlock still attempted."""
    conn = _SchemaConn(
        fail_ddl_with=RuntimeError("the migration itself failed"),
        fail_unlock_with=RuntimeError("connection lost"))
    _fake_viz_conn(monkeypatch, conn)

    with pytest.raises(RuntimeError, match="the migration itself failed"):
        db.apply_schema()

    assert [s for s in conn.sqls if s.startswith("SELECT pg_advisory_unlock")], (
        "the unlock must still be attempted")


def test_apply_schema_swallows_unlock_failure_after_success(
        monkeypatch, caplog):
    """A clean migration with a failed unlock must not fail the boot: the
    migration committed, and the unlock failure's causes — a dead session,
    whose lock died with it, or the aborted transaction of a failed DDL,
    whose error aborts the boot and with it every pooled session — leave
    the lock free or the process dead either way."""
    conn = _SchemaConn(fail_unlock_with=RuntimeError("connection lost"))
    _fake_viz_conn(monkeypatch, conn)

    with caplog.at_level(logging.WARNING, logger="claudit.db"):
        db.apply_schema()

    assert [s for s in conn.sqls if s.startswith("SELECT pg_advisory_unlock")], (
        "the unlock must still be attempted")
    logged = [r for r in caplog.records
              if r.name == "claudit.db" and r.levelno >= logging.WARNING]
    assert logged, "a swallowed unlock failure must be logged, not silent"
    assert "connection lost" in logged[0].getMessage()
