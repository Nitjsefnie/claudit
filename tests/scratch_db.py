"""The one place a test database is named, created or dropped.

Every name is `claudit_test_run_<epoch>_<pid>_<hex>[_<label>]`, unique to
this process, so two suites on one Postgres server never touch each
other's databases (issue #68). Import it as `tests.scratch_db` only: the
run tag is drawn at import, and a second import path would draw another.

Deliberately free of `backend` imports at module load:
conftest imports this module before it claims
CLAUDIT_DATABASE_URL_VIZ, and backend.app would claim it first.
"""
from __future__ import annotations

import itertools
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

# Every schema file, in the order db.SCHEMA_PATHS applies them. A test
# database missing a table the app reads fails in a way that looks like a
# product bug, so the fixture applies the whole schema and not just its
# first file (issue #436 added the second, #469 the third). Equal to
# backend.schema_files.SCHEMA_PATHS, element for element:
# test_the_scratch_schema_lists_every_schema_files_path fails the change
# that drops one.
SCHEMA = (
    Path(__file__).resolve().parents[1] / "backend" / "schema.sql",
    Path(__file__).resolve().parents[1] / "backend" / "schema_web_metrics.sql",
    Path(__file__).resolve().parents[1] / "backend" / "schema_request_fee.sql",
    Path(__file__).resolve().parents[1] / "backend" / "schema_replay.sql",
    Path(__file__).resolve().parents[1] / "backend" / "schema_web_search.sql",
    Path(__file__).resolve().parents[1] / "backend" / "schema_cache_write.sql",
    Path(__file__).resolve().parents[1] / "backend" / "schema_collection.sql",
    Path(__file__).resolve().parents[1] / "backend" / "schema_widths.sql",
    Path(__file__).resolve().parents[1] / "backend" / "schema_dims.sql",
    Path(__file__).resolve().parents[1] / "backend" / "schema_types.sql",
    Path(__file__).resolve().parents[1] / "backend"
    / "schema_records_price.sql",
    Path(__file__).resolve().parents[1] / "backend" / "schema_timelines.sql",
)

# The schema every earlier file builds: everything except the #959
# migration itself (the width guard, the shape file, the type
# compaction that converges a database already in that shape, and the
# records_price split those end at) — the LAST SCHEMA_PATHS entries.
# A test standing an old-shape database up before applying the current
# chain starts from this one tuple; keeping it here is what stops three
# files from restating the same filter (and drifting apart on it).
OLD_SCHEMA = tuple(
    p for p in SCHEMA
    if p.name not in ("schema_widths.sql", "schema_dims.sql",
                      "schema_types.sql", "schema_records_price.sql",
                      "schema_timelines.sql"))

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


def table_columns(conn, table: str) -> set[str]:
    """One public table's column names — the catalog read every schema
    assertion makes, defined once so the three schema suites cannot drift
    apart on it."""
    return {row[0] for row in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s", (table,)
    ).fetchall()}


def seq_used(conn, sequence: str) -> int:
    """Values the sequence has handed out; a fresh one has spent none."""
    last, called = conn.execute(
        f"SELECT last_value, is_called FROM {sequence}"  # noqa: S608
    ).fetchone()
    return last - 1 + (1 if called else 0)


def seed_projects_with_file(conn, project_ids, file_key, session_id, ts,
                            alias=None) -> None:
    """Two `projects` rows and the one `files` row that ties the source to
    the target — the seed the alias-fold and lane-rekey target-upsert tests
    each used to spell out for themselves."""
    for pid in project_ids:
        conn.execute(
            "INSERT INTO projects (project_id, display_name, "
            "first_seen_at, last_seen_at) VALUES (%s, %s, %s, %s)",
            (pid, pid, ts, ts))
    conn.execute(
        "INSERT INTO files (file_key, project_id, session_id, is_main, "
        "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
        "parser_version) VALUES (%s, %s, %s, TRUE, 'e', 1, %s, %s, '0')",
        (file_key, project_ids[0], session_id, ts, ts))
    if alias is not None:
        conn.execute(
            "INSERT INTO project_aliases (pattern, project_id) "
            "VALUES (%s, %s)", (alias, project_ids[1]))


def hold_run_lease() -> None:
    """Hold one connection named after this run until the process exits.

    The sweep skips any run whose lease is live, which is the only signal
    that crosses hosts and PID namespaces and survives the gaps between
    tests when nobody is connected to the run's databases.

    The connection sits on THIS run's own small lease database, not on
    `postgres`/`template1`: Postgres refuses any other client's database
    creation for as long as a template has an open connection, and the
    lease is open for the whole run (issue #87)."""
    if _lease and not _lease[0].closed:
        return
    create_empty_database(db_name("lease"))
    conn = psycopg.connect(dbname=db_name("lease"), application_name=db_name(),
                           autocommit=True, connect_timeout=5)
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


def create_database(label: str, schema=SCHEMA) -> str:
    """This run's database for `label`, with `schema` applied if given.

    `schema` is one path, a sequence of them, or None. Every file goes to
    psql in ONE invocation, so the fixture cannot leave a half-applied
    schema the way a per-file loop would.
    """
    hold_run_lease()
    name = db_name(label)
    create_empty_database(name)
    if schema is not None:
        files = [schema] if isinstance(schema, Path) else list(schema)
        if not files:
            raise ValueError("schema must name at least one file, or be None")
        ddl = None
        if tuple(files) == SCHEMA:
            from tests.schema_policy import fixture_ddl  # pylint: disable=import-outside-toplevel

            ddl = fixture_ddl(files)
        file_args = ["-f", "-"] if ddl is not None else list(
            itertools.chain.from_iterable(("-f", str(f)) for f in files))
        proc = subprocess.run(
            ["psql", "-q", "-X", "-v", "ON_ERROR_STOP=1", "-d", name,
             *file_args], input=ddl, capture_output=True, text=True, check=False)
        if proc.returncode:
            raise RuntimeError(f"applying schema to {name} failed:\n{proc.stderr}")
    return name


def drop_database(name: str) -> None:
    with closing(admin_connection()) as conn:
        _drop(conn, name, force=True)


def scratch_viz_database(monkeypatch, label: str):
    """Fixture body: a fresh database as CLAUDIT_DATABASE_URL_VIZ for the test."""
    from backend import db  # pylint: disable=import-outside-toplevel
    name = create_database(label)
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
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
        # Release the lease first: the lease database is one of the rows
        # below, and its WITH (FORCE) drop must not terminate our own
        # backend (issue #87).
        if _lease and not _lease[0].closed:
            _lease[0].close()
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
