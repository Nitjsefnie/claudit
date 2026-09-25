"""schema_check() discriminates auth-DB failure causes (issue #122).

An empty information_schema probe for users.config has three causes:
the column is genuinely absent; the role holds no SELECT on users (a
table without SELECT is invisible in information_schema.columns); or
DATABASE_URL_AUTH points at the wrong (empty) database. The old code
reported all three as "no 'config' column" — the live finding was a
connectable role with no SELECT producing that message against a DB
where the column exists. These tests pin the discrimination: privilege
and wrong-DB failures must name their own cause.
"""
from contextlib import closing

import psycopg
import pytest
from psycopg import sql

from backend import db
from tests import scratch_db

# Roles are cluster-global, so the name must be run-unique to never
# collide with a concurrent suite on the same server. (Prefix avoids the
# scratch_db NAME_ROOT — the guard forbids test code naming test DBs.)
_ROLE = f"claudit_chk_role_{scratch_db.RUN_TAG}"


@pytest.fixture(autouse=True)
def _viz_ready(monkeypatch):
    """A viz DB with the app schema applied, so schema_check's viz half
    passes and every case isolates the auth-DB behaviour under test."""
    name = scratch_db.create_database("schema_chk_viz")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{name}")
    db.reset_viz_pool()
    yield
    db.reset_viz_pool()
    scratch_db.drop_database(name)


@pytest.fixture(name="auth_env")
def _auth_env_fixture(monkeypatch):
    """Per-case auth database + pool reset bookkeeping. `auth_env(label)`
    creates a fresh empty auth DB (once per label) and returns its name;
    `auth_env(dsn=...)` re-points the pool at an explicit DSN without
    touching any database (e.g. connecting as a restricted role)."""
    created: dict[str, str] = {}

    def _make(label: str | None = None, dsn: str | None = None) -> str:
        if label is not None and label not in created:
            name = scratch_db.db_name(label)
            scratch_db.create_empty_database(name)
            created[label] = name
        elif label is not None:
            name = created[label]
        else:
            name = next(iter(created.values())) if created else ""
        monkeypatch.setenv("DATABASE_URL_AUTH", dsn or f"postgresql:///{name}")
        db.reset_auth_pool()
        return name

    yield _make
    db.reset_auth_pool()
    for name in created.values():
        scratch_db.drop_database(name)


def _auth_conn(name: str):
    """Autocommit admin (postgres) connection to the named auth DB."""
    return psycopg.connect(f"postgresql:///{name}", autocommit=True,
                           connect_timeout=5)


def test_happy_path_returns_none(auth_env):
    """users.config jsonb + SELECT granted -> schema_check() is silent."""
    name = auth_env("schema_chk_ok")
    with closing(_auth_conn(name)) as c:
        c.execute("CREATE TABLE public.users (id bigint PRIMARY KEY, "
                  "config jsonb)")
    assert db.schema_check() is None


def test_column_absent_reports_missing_column(auth_env):
    """Genuinely absent column keeps the original message — case (c)."""
    name = auth_env("schema_chk_nocol")
    with closing(_auth_conn(name)) as c:
        c.execute("CREATE TABLE public.users (id bigint PRIMARY KEY)")
    with pytest.raises(RuntimeError, match=r"no 'config' column"):
        db.schema_check()


def test_wrong_type_reports_jsonb(auth_env):
    """Non-JSONB config keeps the type message."""
    name = auth_env("schema_chk_type")
    with closing(_auth_conn(name)) as c:
        c.execute("CREATE TABLE public.users (id bigint PRIMARY KEY, "
                  "config text)")
    with pytest.raises(RuntimeError, match=r"must be JSONB"):
        db.schema_check()


def test_missing_select_names_the_grant_not_the_column(auth_env):
    """A connectable role with no SELECT on users sees an empty
    information_schema — the error must name the privilege and the grant,
    NOT "no 'config' column" (issue #122)."""
    name = auth_env("schema_chk_priv")
    with closing(_auth_conn(name)) as c:
        c.execute("CREATE TABLE public.users (id bigint PRIMARY KEY, "
                  "config jsonb)")
        c.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'probe123'")
                  .format(sql.Identifier(_ROLE)))
        c.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}")
                  .format(sql.Identifier(name),
                          sql.Identifier(_ROLE)))
    try:
        # The DSN carries the role's own password so the case also holds
        # against TCP/scram servers (CI service container), not just
        # trust-socket dev servers.
        auth_env(dsn=f"dbname={name} user={_ROLE} password=probe123")
        with pytest.raises(RuntimeError) as excinfo:
            db.schema_check()
        msg = str(excinfo.value)
        assert "SELECT" in msg and _ROLE in msg
        assert "no 'config' column" not in msg
    finally:
        db.reset_auth_pool()
        with closing(_auth_conn("postgres")) as admin:
            admin.execute(sql.SQL(
                "REVOKE CONNECT ON DATABASE {} FROM {}")
                .format(sql.Identifier(name),
                        sql.Identifier(_ROLE)))
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}")
                          .format(sql.Identifier(_ROLE)))


def test_wrong_database_names_the_users_table_case(auth_env):
    """An empty (wrong) database has no users table at all — the error
    must point at DATABASE_URL_AUTH, NOT "no 'config' column"."""
    auth_env("schema_chk_wrongdb")
    with pytest.raises(RuntimeError) as excinfo:
        db.schema_check()
    msg = str(excinfo.value)
    assert "no 'users' table visible" in msg
    assert "DATABASE_URL_AUTH" in msg
    assert "no 'config' column" not in msg
