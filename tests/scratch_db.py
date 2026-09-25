"""The one place a test database name is made.

Every name is `claudit_test_run_<epoch>_<pid>_<hex>[_<label>]`, unique to
this process, so two suites on one Postgres server never touch each
other's databases (issue #68). Import it as `tests.scratch_db` only: the
run tag is drawn at import, and a second import path would draw another.

Deliberately free of `backend` imports except inside
`scratch_viz_database`: conftest imports this module before it claims
DATABASE_URL_VIZ, and backend.app would claim it first.
"""
import os
import re
import secrets
import subprocess
import time
from contextlib import closing
from pathlib import Path

import psycopg
from psycopg import sql

NAME_ROOT = "claudit_test"
RUN_PREFIX = f"{NAME_ROOT}_run_"
RUN_TAG = f"{int(time.time())}_{os.getpid()}_{secrets.token_hex(4)}"
# A leftover younger than this may belong to a run still in progress.
STALE_AFTER_S = 6 * 3600

_SCHEMA = Path(__file__).resolve().parents[1] / "backend" / "schema.sql"
_RUN_NAME = re.compile(
    rf"^{RUN_PREFIX}(?P<epoch>\d+)_(?P<pid>\d+)_[0-9a-f]{{8}}(?:_[a-z0-9_]+)?$")
_LABEL = re.compile(r"^[a-z0-9_]+$")


def db_name(label: str = "") -> str:
    """This run's database name for `label` (this run's base name if empty)."""
    if label and not _LABEL.match(label):
        raise ValueError(f"label must match {_LABEL.pattern}: {label!r}")
    name = f"{RUN_PREFIX}{RUN_TAG}" + (f"_{label}" if label else "")
    if len(name.encode()) > 63:
        raise ValueError(f"database name over Postgres's 63 bytes: {name}")
    return name


def is_run_database(name: str) -> bool:
    return _RUN_NAME.match(name) is not None


def admin_connection() -> psycopg.Connection:
    return psycopg.connect("postgresql:///postgres", autocommit=True,
                           connect_timeout=5)


def _drop(conn: psycopg.Connection, name: str, force: bool) -> None:
    stmt = sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
    conn.execute(stmt + sql.SQL(" WITH (FORCE)") if force else stmt)


def create_database(label: str) -> str:
    """Fresh database for `label` with backend/schema.sql applied."""
    name = db_name(label)
    with closing(admin_connection()) as conn:
        _drop(conn, name, force=True)
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    proc = subprocess.run(
        ["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", name, "-f", str(_SCHEMA)],
        capture_output=True, text=True, check=False)
    if proc.returncode:
        raise RuntimeError(f"applying schema to {name} failed:\n{proc.stderr}")
    return name


def drop_database(name: str) -> None:
    with closing(admin_connection()) as conn:
        _drop(conn, name, force=True)


def scratch_viz_database(monkeypatch, label: str):
    """Fixture body: a fresh database as DATABASE_URL_VIZ for the test."""
    from backend import db  # pylint: disable=import-outside-toplevel
    name = create_database(label)
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{name}")
    db.reset_viz_pool()
    try:
        yield name
    finally:
        db.reset_viz_pool()
        drop_database(name)


def drop_run_databases() -> list[str]:
    """Drop every database this run created; the end-of-session backstop
    for fixtures that never reached their own teardown."""
    with closing(admin_connection()) as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE starts_with(datname, %s)",
            (db_name(),)).fetchall()
        names = [n for (n,) in rows if is_run_database(n)]
        for name in names:
            _drop(conn, name, force=True)
    return names


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sweep_stale_databases(max_age_s: int = STALE_AFTER_S) -> list[str]:
    """Drop leftovers of killed runs: run-named, older than `max_age_s`,
    whose creating process is gone and which nobody is connected to."""
    cutoff = time.time() - max_age_s
    dropped = []
    with closing(admin_connection()) as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE starts_with(datname, %s)",
            (RUN_PREFIX,)).fetchall()
        for (name,) in rows:
            m = _RUN_NAME.match(name)
            if (m is None or int(m["epoch"]) > cutoff
                    or _pid_alive(int(m["pid"]))):
                continue
            try:
                # Unforced: Postgres refuses while anyone is connected.
                _drop(conn, name, force=False)
            except psycopg.errors.ObjectInUse:
                continue
            dropped.append(name)
    return dropped
