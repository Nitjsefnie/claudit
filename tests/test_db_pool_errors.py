"""Issue #449: a pool whose DSN the server refuses must fail with the
driver's error, not a bare PoolTimeout.

The pool starts its connection workers in the background: a DSN the
server rejects (wrong password, missing database) fails only inside
those workers — logged on the psycopg pool logger at most — while the
first client sees a bare PoolTimeout with the cause suppressed
(`raise ... from None` in getconn). First boot then fails on a timeout
that names neither the database nor the reason, and the FATAL is
swallowed.

viz_pool()/auth_pool() therefore probe the DSN with one bounded,
synchronous connection at first construction: the driver's message
reaches the operator chained to a RuntimeError naming the env var to
check (the SV-SCHEMA-FAIL-FAST shape), before any client waits.

The probe DSN reaches the test's Postgres server through the same
PGHOST/PGPORT/PGUSER environment the suite's scratch databases use,
but names a database that does not exist, so the server answers with
the driver's FATAL: database "..." does not exist.
"""
from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from backend import db
from tests import scratch_db

_BAD_DSN = "postgresql:///claudit_test_no_such_db_449"


@pytest.fixture(name="bad_pool_env")
def _bad_pool_env(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL_VIZ", _BAD_DSN)
    monkeypatch.setenv("DATABASE_URL_AUTH", _BAD_DSN)
    db.reset_viz_pool()
    db.reset_auth_pool()
    yield
    db.reset_viz_pool()
    db.reset_auth_pool()


@pytest.mark.db
def test_viz_pool_failure_names_the_driver_error(bad_pool_env) -> None:
    with pytest.raises(RuntimeError) as excinfo:
        db.viz_pool()
    msg = str(excinfo.value)
    # The driver's FATAL reaches the surfaced message...
    assert "does not exist" in msg
    # ...naming the env var whose DSN to check (fail-fast baseline).
    assert "DATABASE_URL_VIZ" in msg
    # The driver's exception is chained, not swallowed.
    assert isinstance(excinfo.value.__cause__, psycopg.OperationalError)


@pytest.mark.db
def test_auth_pool_failure_names_the_driver_error(bad_pool_env) -> None:
    with pytest.raises(RuntimeError) as excinfo:
        db.auth_pool()
    msg = str(excinfo.value)
    assert "does not exist" in msg
    assert "DATABASE_URL_AUTH" in msg
    assert isinstance(excinfo.value.__cause__, psycopg.OperationalError)


@pytest.mark.db
def test_auth_pool_rejects_a_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #460: the auth pool's connections are read-only at the
    server, so a write reaching the auth DB fails loudly instead of
    committing (the doctrine's read-only invariant, enforced)."""
    name = scratch_db.create_database("auth_ro")
    monkeypatch.setenv("DATABASE_URL_AUTH", f"postgresql:///{name}")
    db.reset_auth_pool()
    try:
        conn_admin = psycopg.connect(f"postgresql:///{name}", autocommit=True)
        conn_admin.execute(  # pylint: disable=no-member
            "CREATE TABLE users (user_id integer PRIMARY KEY, "
            "config jsonb NOT NULL)")
        conn_admin.execute(  # pylint: disable=no-member
            "INSERT INTO users VALUES (1, '{}'::jsonb)")
        conn_admin.close()  # pylint: disable=no-member
        with db.auth_conn() as conn:
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute(
                    "UPDATE users SET config = %s::jsonb WHERE user_id = 1",
                    ("{}",))
    finally:
        db.reset_auth_pool()
        scratch_db.drop_database(name)
