"""The one place a test database is named, created or dropped.

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
from collections.abc import Collection
from contextlib import closing
from pathlib import Path

import psycopg
from psycopg import sql

NAME_ROOT = "claudit_test"
RUN_PREFIX = f"{NAME_ROOT}_run_"
# A leftover younger than this may belong to a run still in progress.
STALE_AFTER_S = 6 * 3600

SCHEMA = Path(__file__).resolve().parents[1] / "backend" / "schema.sql"
_RUN_NAME = re.compile(
    rf"^(?P<base>{RUN_PREFIX}(?P<epoch>\d+)_(?P<pid>\d+)_[0-9a-f]{{8}})"
    r"(?:_[a-z0-9_]+)?$")
_LABEL = re.compile(r"^[a-z0-9_]+$")


def draw_run_tag() -> str:
    return f"{int(time.time())}_{os.getpid()}_{secrets.token_hex(4)}"


RUN_TAG = draw_run_tag()
# The lease connection, once taken; see hold_run_lease.
_lease: list[psycopg.Connection] = []


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


def admin_connection(**kwargs) -> psycopg.Connection:
    """A maintenance connection, to `postgres` or, as createdb falls back,
    to `template1`."""
    try:
        return psycopg.connect("postgresql:///postgres", autocommit=True,
                               connect_timeout=5, **kwargs)
    except psycopg.OperationalError:
        return psycopg.connect("postgresql:///template1", autocommit=True,
                               connect_timeout=5, **kwargs)


def hold_run_lease() -> None:
    """Hold one connection named after this run until the process exits.

    The sweep skips any run whose lease is live, which is the only signal
    that crosses hosts and PID namespaces and survives the gaps between
    tests when nobody is connected to the run's databases."""
    if _lease and not _lease[0].closed:
        return
    conn = admin_connection(application_name=db_name())
    try:
        # pylint misreads psycopg.connect's return type once kwargs pass through.
        conn.execute("SET idle_session_timeout = 0")  # pylint: disable=no-member
    except psycopg.errors.UndefinedObject:
        pass  # before Postgres 14 there is no such timeout to disable
    _lease[:] = [conn]


def _drop(conn: psycopg.Connection, name: str, force: bool) -> None:
    stmt = sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
    conn.execute(stmt + sql.SQL(" WITH (FORCE)") if force else stmt)


def create_empty_database(name: str) -> None:
    """Create `name` fresh. template0, because nobody can be connected to
    it, where a session on template1 would refuse every CREATE."""
    with closing(admin_connection()) as conn:
        _drop(conn, name, force=True)
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
            sql.Identifier(name)))


def create_database(label: str, schema: Path | None = SCHEMA) -> str:
    """This run's database for `label`, with `schema` applied if given."""
    hold_run_lease()
    name = db_name(label)
    create_empty_database(name)
    if schema is not None:
        proc = subprocess.run(
            ["psql", "-q", "-X", "-v", "ON_ERROR_STOP=1", "-d", name,
             "-f", str(schema)],
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


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sweep_stale_databases(max_age_s: int = STALE_AFTER_S,
                          names: Collection[str] | None = None) -> list[str]:
    """Drop leftovers of killed runs, optionally only among `names`.

    A candidate is run-named, owned by a role this one can act as, older
    than `max_age_s`, its run holds no lease, its PID is gone here, and
    nobody is connected to it."""
    cutoff = time.time() - max_age_s
    dropped = []
    with closing(admin_connection()) as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE starts_with(datname, %s)"
            " AND pg_has_role(datdba, 'USAGE')", (RUN_PREFIX,)).fetchall()
        leased = {a for (a,) in conn.execute(
            "SELECT application_name FROM pg_stat_activity")}
        for (name,) in rows:
            m = _RUN_NAME.match(name)
            if m is None or (names is not None and name not in names):
                continue
            if (int(m["epoch"]) > cutoff or m["base"] in leased
                    or pid_alive(int(m["pid"]))):
                continue
            try:
                # Unforced: Postgres refuses while anyone is connected.
                _drop(conn, name, force=False)
            except (psycopg.errors.ObjectInUse,
                    psycopg.errors.InsufficientPrivilege):
                continue
            dropped.append(name)
    return dropped
