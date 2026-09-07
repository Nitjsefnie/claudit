"""Startup applies backend/schema.sql, so a deploy cannot outrun its DB.

Issue #43: schema_check() only asserted that `files` existed, so a
checkout that pulled code writing a new column started clean and then
failed every ingest with UndefinedColumn. Applying the idempotent
schema at boot removes the divergence rather than reporting it.
"""
from __future__ import annotations

import asyncio

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
