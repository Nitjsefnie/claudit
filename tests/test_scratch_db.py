"""Run-unique scratch databases (issue #68).

Two suites on one Postgres server used to share fixed database names, so
each run's teardown removed the other's database mid-test. Every name now
comes from tests/scratch_db.py; these tests hold that in place.
"""
import io
import os
import re
import secrets
import subprocess
import sys
import textwrap
import time
import tokenize
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


def _foreign_name(epoch: int, pid: int, label: str) -> str:
    """A run-shaped name no live run owns: its own random part."""
    return f"{scratch_db.RUN_PREFIX}{epoch}_{pid}_{secrets.token_hex(4)}_{label}"


def _base(name: str) -> str:
    return name.rsplit("_", 1)[0]


def _python(code: str, **env) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=_REPO_ROOT,
                          env={**os.environ, **env}, capture_output=True,
                          text=True, check=False)


def _old_epoch() -> int:
    return int(time.time()) - scratch_db.STALE_AFTER_S - 60


# ---- naming -----------------------------------------------------------

def test_every_name_carries_this_runs_tag():
    name = scratch_db.db_name("api_mut")
    assert name.startswith(scratch_db.RUN_PREFIX)
    assert name.endswith("_api_mut")
    assert scratch_db.is_run_database(name)
    assert len(name.encode()) <= 63


def test_three_processes_draw_three_tags():
    code = "from tests import scratch_db; print(scratch_db.RUN_TAG)"
    tags = {_python(code).stdout.strip(), _python(code).stdout.strip(),
            scratch_db.RUN_TAG}
    assert len(tags) == 3


def test_the_random_part_alone_separates_same_pid_same_second(monkeypatch):
    """Two containers can give pytest one PID in one second."""
    monkeypatch.setattr(scratch_db.time, "time", lambda: 1700000000.0)
    monkeypatch.setattr(scratch_db.os, "getpid", lambda: 7)
    assert scratch_db.draw_run_tag() != scratch_db.draw_run_tag()


@pytest.mark.parametrize("label", ["Api", "api-mut", "api mut", "ápi"])
def test_a_label_the_backstop_could_not_recognise_is_refused(label):
    with pytest.raises(ValueError):
        scratch_db.db_name(label)


def test_a_name_postgres_would_truncate_is_refused():
    with pytest.raises(ValueError):
        scratch_db.db_name("x" * 30)


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


# ---- create / drop ----------------------------------------------------

def test_create_applies_the_schema_and_drop_removes_it():
    name = scratch_db.create_database("roundtrip")
    try:
        with closing(psycopg.connect(f"postgresql:///{name}")) as conn:
            assert conn.execute("SELECT to_regclass('files')").fetchone() == ("files",)
    finally:
        scratch_db.drop_database(name)
    assert not _exists(name)


def test_a_schema_error_is_raised_not_swallowed(tmp_path):
    broken = tmp_path / "broken.sql"
    broken.write_text("CREATE TABLE t (;\n")
    try:
        with pytest.raises(RuntimeError, match="schema"):
            scratch_db.create_database("schema_err", schema=broken)
    finally:
        scratch_db.drop_database(scratch_db.db_name("schema_err"))


def test_this_run_holds_its_lease():
    with closing(scratch_db.admin_connection()) as conn:
        names = {a for (a,) in conn.execute(
            "SELECT application_name FROM pg_stat_activity")}
    assert scratch_db.db_name() in names


# ---- stale sweep ------------------------------------------------------

def test_sweep_drops_a_stale_orphan_and_keeps_everything_else():
    """Swept by name, so a concurrent run's sweep and this one never
    decide on each other's fabricated databases."""
    old, dead = _old_epoch(), _dead_pid()
    stale = _foreign_name(old, dead, "stale")
    young = _foreign_name(int(time.time()) - 3600, dead, "young")
    live_pid = _foreign_name(old, os.getpid(), "livepid")
    decoy = _foreign_name(old, dead, "decoy") + "X"
    names = [stale, young, live_pid, decoy]
    for n in names:
        scratch_db.create_empty_database(n)
    try:
        scratch_db.sweep_stale_databases(names=names)
        assert not _exists(stale)
        for kept in (young, live_pid, decoy):
            assert _exists(kept), kept
    finally:
        for n in names:
            scratch_db.drop_database(n)


def test_sweep_keeps_a_leased_run_whose_pid_it_cannot_see():
    """A live run in another PID namespace or on another host: its PID
    looks dead here and it is older than the threshold, and between
    tests nobody is connected to its database. Its lease keeps it."""
    leased = _foreign_name(_old_epoch(), _dead_pid(), "leased")
    with closing(scratch_db.admin_connection(application_name=_base(leased))):
        scratch_db.create_empty_database(leased)
        try:
            scratch_db.sweep_stale_databases(names=[leased])
            assert _exists(leased)
        finally:
            scratch_db.drop_database(leased)


def test_sweep_never_forces_a_database_someone_is_connected_to():
    # Young, so no other run's sweep ever considers it; max_age_s=0 makes
    # this sweep consider it.
    in_use = _foreign_name(int(time.time()), _dead_pid(), "inuse")
    scratch_db.create_empty_database(in_use)
    try:
        with closing(psycopg.connect(f"postgresql:///{in_use}")):
            scratch_db.sweep_stale_databases(max_age_s=0, names=[in_use])
            assert _exists(in_use)
    finally:
        scratch_db.drop_database(in_use)


def test_a_permission_error_means_the_process_is_alive(monkeypatch):
    def kill(pid, sig):
        raise PermissionError
    monkeypatch.setattr(scratch_db.os, "kill", kill)
    assert scratch_db.pid_alive(12345)


def test_another_roles_stale_database_is_left_alone_not_fatal():
    """On a server shared by several roles, a leftover owned by someone
    else must neither crash this run's sweep nor be dropped."""
    with closing(scratch_db.admin_connection()) as conn:
        can = conn.execute("SELECT rolsuper OR rolcreaterole FROM pg_roles"
                           " WHERE rolname = current_user").fetchone()
        if not (can and can[0]):
            pytest.skip("needs a role that can create roles")
        owner, sweeper = f"{scratch_db.db_name()}_o", f"{scratch_db.db_name()}_s"
        password = secrets.token_hex(16)
        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(owner)))
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(sweeper), sql.Literal(password)))
        # Young, so no other run's sweep takes it; max_age_s=0 below.
        foreign = _foreign_name(int(time.time()), _dead_pid(), "otherrole")
        try:
            scratch_db.create_empty_database(foreign)
            conn.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(foreign), sql.Identifier(owner)))
            try:
                with closing(psycopg.connect(
                        "postgresql:///postgres", user=sweeper,
                        password=password, connect_timeout=5)):
                    pass
            except psycopg.OperationalError:
                pytest.skip("server does not admit a password login here")
            proc = _python(
                "from tests import scratch_db; "
                f"scratch_db.sweep_stale_databases(0, names=[{foreign!r}])",
                PGUSER=sweeper, PGPASSWORD=password)
            assert proc.returncode == 0, proc.stderr
            assert _exists(foreign)
        finally:
            scratch_db.drop_database(foreign)
            for role in (owner, sweeper):
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


# ---- session wiring ---------------------------------------------------

def _nested_session(tmp_path, body: str, *hooks: str):
    (tmp_path / "conftest.py").write_text(
        f"from tests.conftest import {', '.join(hooks)}  # noqa: F401\n")
    (tmp_path / "test_nested.py").write_text(textwrap.dedent(body))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(tmp_path)],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        capture_output=True, text=True, check=False)


def test_a_failing_run_still_drops_every_database_it_created(tmp_path):
    """A nested session whose test creates a database and then fails,
    without ever dropping it: the session finalizer must."""
    record = tmp_path / "created.txt"
    proc = _nested_session(tmp_path, f"""
        from pathlib import Path
        from tests import scratch_db

        def test_leaks():
            Path({str(record)!r}).write_text(scratch_db.create_database("leak"))
            assert False
    """, "pytest_sessionfinish")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    name = record.read_text()
    assert scratch_db.is_run_database(name)
    assert not _exists(name)


def test_the_end_of_run_cleanup_spares_every_other_run():
    other = _foreign_name(int(time.time()), os.getpid(), "otherrun")
    scratch_db.create_empty_database(other)
    mine = scratch_db.create_database("mine")
    try:
        proc = _python("from tests import scratch_db; scratch_db.drop_run_databases()")
        assert proc.returncode == 0, proc.stderr
        assert _exists(other)
        assert _exists(mine)
    finally:
        scratch_db.drop_database(other)
        scratch_db.drop_database(mine)


def test_a_session_start_sweeps_stale_orphans(tmp_path):
    orphan = _foreign_name(_old_epoch(), _dead_pid(), "orphan")
    scratch_db.create_empty_database(orphan)
    try:
        proc = _nested_session(tmp_path, "def test_ok():\n    pass\n",
                               "pytest_sessionstart")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not _exists(orphan)
    finally:
        scratch_db.drop_database(orphan)


# ---- guard ------------------------------------------------------------

# What only the helper may do: name a test database, or create or drop one.
_FORBIDDEN = re.compile(
    rf"{scratch_db.NAME_ROOT}|\b(?:create|drop)db\b|\b(?:CREATE|DROP)\s+DATABASE\b",
    re.IGNORECASE)


def _code(path: Path) -> str:
    """The source without its comments; strings and docstrings stay."""
    toks = tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline)
    return " ".join(t.string for t in toks if t.type != tokenize.COMMENT)


def offending_files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.py")
                  if p.name != "scratch_db.py" and _FORBIDDEN.search(_code(p)))


def test_no_test_names_creates_or_drops_a_database_outside_the_helper():
    assert offending_files(_REPO_ROOT / "tests") == []


def test_the_guard_catches_every_plant_and_ignores_comments(tmp_path):
    # Plant text is split so this file does not trip its own guard.
    plants = {
        "test_literal.py": f'DB = "{scratch_db.NAME_ROOT}_api"\n',
        "test_cli.py": 'import os\nos.system("create" "db fixed")\n',
        "test_sql.py": 'SQL = "drop data" "base fixed"\n',
        "sub/test_nested.py": 'q = "CREATE " "DATABASE x"\n',
    }
    # Each plant is written joined, as a real offender would spell it.
    for rel, text in plants.items():
        path = tmp_path / rel
        path.parent.mkdir(exist_ok=True)
        path.write_text(text.replace('" "', ""))
    (tmp_path / "scratch_db.py").write_text(f'ROOT = "{scratch_db.NAME_ROOT}"\n')
    (tmp_path / "test_comment.py").write_text(
        f"# see {scratch_db.RUN_PREFIX}* and the drop" "db it replaced\nx = 1\n")
    assert offending_files(tmp_path) == sorted(plants)
