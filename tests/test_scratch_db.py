"""Run-unique scratch databases (issue #68).

Two suites on one Postgres server used to share fixed database names, so
each run's dropdb removed the other's database mid-test. Every name now
comes from tests/scratch_db.py; these tests hold that in place.
"""
import os
import subprocess
import sys
import textwrap
import time
from contextlib import closing
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from tests import scratch_db

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _exists(name: str) -> bool:
    with closing(scratch_db.admin_connection()) as conn:
        return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                            (name,)).fetchone() is not None


def _dead_pid() -> int:
    proc = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True, check=True)
    return int(proc.stdout)


def _run_tag_in_child() -> str:
    proc = subprocess.run(
        [sys.executable, "-c",
         "from tests import scratch_db; print(scratch_db.RUN_TAG)"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def test_every_name_carries_this_runs_tag():
    name = scratch_db.db_name("api_mut")
    assert name.startswith(scratch_db.RUN_PREFIX)
    assert name.endswith("_api_mut")
    assert scratch_db.is_run_database(name)
    assert len(name.encode()) <= 63


def test_two_processes_never_share_a_tag():
    assert _run_tag_in_child() != _run_tag_in_child() != scratch_db.RUN_TAG


def test_default_viz_dsn_is_run_unique_unless_exported():
    """conftest's setdefault: an exported DATABASE_URL_VIZ wins, otherwise
    the default names this run."""
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL_VIZ"}
    probe = ("import os, tests.conftest; from tests import scratch_db; "
             "print(os.environ['DATABASE_URL_VIZ'] + ' ' + scratch_db.RUN_TAG)")
    out = subprocess.run([sys.executable, "-c", probe], cwd=_REPO_ROOT, env=env,
                         capture_output=True, text=True, check=True).stdout
    dsn, tag = out.split()
    assert dsn == f"postgresql:///{scratch_db.RUN_PREFIX}{tag}"

    env["DATABASE_URL_VIZ"] = "postgresql:///exported_by_caller"
    out = subprocess.run([sys.executable, "-c", probe], cwd=_REPO_ROOT, env=env,
                         capture_output=True, text=True, check=True).stdout
    assert out.split()[0] == "postgresql:///exported_by_caller"


@pytest.mark.parametrize("name", [
    scratch_db.NAME_ROOT,
    f"{scratch_db.NAME_ROOT}_api",
    f"{scratch_db.NAME_ROOT}_api_mut",
    f"{scratch_db.RUN_PREFIX}x",
    f"{scratch_db.RUN_PREFIX}1700000000_12_zzzzzzzz_api",
    "claudit",
    "postgres",
])
def test_fixed_and_foreign_names_are_never_run_databases(name):
    assert not scratch_db.is_run_database(name)


def test_create_applies_the_schema_and_drop_removes_it():
    name = scratch_db.create_database("roundtrip")
    try:
        with closing(psycopg.connect(f"postgresql:///{name}")) as conn:
            assert conn.execute("SELECT to_regclass('files')").fetchone() == ("files",)
    finally:
        scratch_db.drop_database(name)
    assert not _exists(name)


def test_sweep_drops_only_stale_orphans():
    old = int(time.time()) - scratch_db.STALE_AFTER_S - 60
    dead = _dead_pid()
    suffix = scratch_db.RUN_TAG.rsplit("_", 1)[1]
    stale = f"{scratch_db.RUN_PREFIX}{old}_{dead}_{suffix}_sweep"
    young = f"{scratch_db.RUN_PREFIX}{int(time.time())}_{dead}_{suffix}_sweep"
    live_pid = f"{scratch_db.RUN_PREFIX}{old}_{os.getpid()}_{suffix}_sweep"
    in_use = f"{scratch_db.RUN_PREFIX}{old}_{dead}_{suffix}_inuse"
    decoy = f"{scratch_db.RUN_PREFIX}{old}_{dead}_{suffix}X"
    names = [stale, young, live_pid, in_use, decoy]
    with closing(scratch_db.admin_connection()) as admin:
        for n in names:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(n)))
    try:
        with closing(psycopg.connect(f"postgresql:///{in_use}")):
            dropped = scratch_db.sweep_stale_databases()
        assert stale in dropped
        assert not _exists(stale)
        for kept in (young, live_pid, in_use, decoy):
            assert kept not in dropped and _exists(kept), kept
    finally:
        for n in names:
            scratch_db.drop_database(n)


def test_a_failing_run_still_drops_every_database_it_created(tmp_path):
    """A nested pytest session whose test creates a database and then
    fails, without ever dropping it: the session finalizer must."""
    record = tmp_path / "created.txt"
    (tmp_path / "conftest.py").write_text(
        "from tests.conftest import pytest_sessionfinish  # noqa: F401\n")
    (tmp_path / "test_leak.py").write_text(textwrap.dedent(f"""
        from pathlib import Path
        from tests import scratch_db

        def test_leaks():
            Path({str(record)!r}).write_text(scratch_db.create_database("leak"))
            assert False
    """))
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    name = record.read_text()
    assert scratch_db.is_run_database(name)
    assert not _exists(name)


def offending_files(root: Path) -> list[str]:
    """Test sources outside the helper that spell a test database name."""
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*.py")
        if p.name != "scratch_db.py"
        and scratch_db.NAME_ROOT in p.read_text(encoding="utf-8"))


def test_no_test_file_names_a_database_literally():
    assert offending_files(_REPO_ROOT / "tests") == []


def test_the_literal_name_guard_catches_a_plant(tmp_path):
    (tmp_path / "scratch_db.py").write_text(f'ROOT = "{scratch_db.NAME_ROOT}"\n')
    (tmp_path / "test_clean.py").write_text("x = 1\n")
    (tmp_path / "test_planted.py").write_text(
        f'DB = "{scratch_db.NAME_ROOT}_api"\n')
    assert offending_files(tmp_path) == ["test_planted.py"]
