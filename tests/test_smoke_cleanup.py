"""The smoke script cleans up after itself on every exit path (issue #87).

main() used to drop its two scratch databases only on the success path,
so a run that failed after provisioning left them for the stale sweep
(six hours). These tests fail ci_smoke.provision in controlled ways and
hold the cleanup in place.
"""
from __future__ import annotations

import importlib.util
import sys
from contextlib import closing
from pathlib import Path

import psycopg

from tests import scratch_db

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load():
    """Import scripts/ci/smoke.py by path.

    scripts/ci is not a package and deliberately has no __init__.py — it
    holds standalone CI entry points, not an importable library.
    """
    path = REPO_ROOT / "scripts" / "ci" / "smoke.py"
    spec = importlib.util.spec_from_file_location("ci_smoke", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_smoke"] = module
    spec.loader.exec_module(module)
    return module


ci_smoke = _load()


def _exists(name: str) -> bool:
    with closing(scratch_db.admin_connection()) as conn:
        return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                            (name,)).fetchone() is not None


def _failing_provision(monkeypatch):
    """Replace ci_smoke.provision with one that really creates one of this
    run's scratch databases and then fails, like a broken schema apply."""
    def provision():
        scratch_db.create_database("smoke")
        raise ci_smoke.SmokeFailure("schema apply exploded")

    monkeypatch.setattr(ci_smoke, "provision", provision)


def _argv(*extra: str):
    old = sys.argv
    sys.argv = ["smoke.py", *extra]
    return old


def test_a_failed_run_drops_what_it_created(monkeypatch, capsys):
    _failing_provision(monkeypatch)
    name = scratch_db.db_name("smoke")
    old = _argv()
    try:
        assert ci_smoke.main() == 1
    finally:
        sys.argv = old
    assert not _exists(name)
    capsys.readouterr()


def test_keep_databases_spares_them_even_on_failure(monkeypatch, capsys):
    _failing_provision(monkeypatch)
    name = scratch_db.db_name("smoke")
    old = _argv("--keep-databases")
    try:
        assert ci_smoke.main() == 1
    finally:
        sys.argv = old
    assert _exists(name)
    scratch_db.drop_database(name)
    capsys.readouterr()


def test_a_cleanup_failure_never_masks_the_exit_code(monkeypatch, capsys):
    _failing_provision(monkeypatch)

    def broken_drop():
        raise psycopg.OperationalError("server went away mid-cleanup")

    monkeypatch.setattr(scratch_db, "drop_run_databases", broken_drop)
    name = scratch_db.db_name("smoke")
    old = _argv()
    try:
        assert ci_smoke.main() == 1
    finally:
        sys.argv = old
    scratch_db.drop_database(name)      # the broken cleanup leaked it
    capsys.readouterr()
