from __future__ import annotations

import inspect
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "scratch_pg.sh"


@pytest.fixture(autouse=True)
def _require_tools() -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    bindir = None
    if shutil.which("pg_config"):
        result = subprocess.run(["pg_config", "--bindir"], capture_output=True,
                                text=True, check=False)
        if result.returncode == 0:
            bindir = Path(result.stdout.strip())
    if not all((bindir and shutil.which(name, path=str(bindir)))
               or shutil.which(name) for name in ("pg_ctl", "initdb")):
        pytest.skip("Postgres binaries not available via pg_config or PATH")


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=cwd,
                          capture_output=True, text=True, check=False,
                          timeout=60)


def _exports(stdout: str) -> dict[str, str]:
    values = {}
    lines = stdout.splitlines()
    assert len(lines) == 3, stdout
    for line in lines:
        words = shlex.split(line)
        assert len(words) == 2 and words[0] == "export", stdout
        key, value = words[1].split("=", 1)
        assert key not in values, stdout
        values[key] = value
    assert set(values) == {"PGHOST", "PGPORT", "PGUSER"}, stdout
    return values


@pytest.mark.db
@pytest.mark.parametrize("without_getuid", [False, True])
def test_start_exports_connect_and_stop_removes_cluster(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, without_getuid: bool) -> None:
    if without_getuid:
        monkeypatch.delattr(os, "getuid", raising=False)
    is_root = subprocess.check_output(["bash", "-c", "id -u"], text=True).strip() == "0"
    if is_root:
        parent = tmp_path
        temp_root = Path(tempfile.gettempdir()).resolve()
        while parent.is_relative_to(temp_root) and parent != temp_root:
            parent.chmod(parent.stat().st_mode | 0o111)
            parent = parent.parent
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = str(listener.getsockname()[1])
    directory = tmp_path / "cluster space's"
    started = _run("start", directory.name, port, cwd=tmp_path)
    try:
        assert started.returncode == 0, started.stderr
        values = _exports(started.stdout)
        expected_user = "postgres" if is_root else subprocess.check_output(
            ["bash", "-c", "id -un"], text=True).strip()
        assert values == {"PGHOST": str(directory), "PGPORT": port,
                          "PGUSER": expected_user}
        with psycopg.connect(host=values["PGHOST"], port=values["PGPORT"],
                             user=values["PGUSER"], dbname="postgres",
                             connect_timeout=5) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
            assert conn.execute("SHOW listen_addresses").fetchone() == ("",)
        exported = _run("env", str(directory), port, cwd=ROOT)
        assert exported.returncode == 0, exported.stderr
        assert _exports(exported.stdout) == values
        assert subprocess.run(
            ["bash", "-c", 'eval "$1"; printf "%s\\n" "$PGHOST" "$PGPORT" "$PGUSER"',
             "scratch-env", exported.stdout], capture_output=True, text=True,
            check=True).stdout.splitlines() == [str(directory), port, expected_user]
    finally:
        if directory.exists():
            stopped = _run("stop", str(directory), cwd=ROOT)
            assert stopped.returncode == 0, stopped.stderr
    assert not directory.exists()
    exported = _run("env", str(directory), port, cwd=ROOT)
    assert exported.returncode == 0, exported.stderr
    assert _exports(exported.stdout)["PGHOST"] == str(directory)
    assert not directory.exists()


@pytest.mark.db
@pytest.mark.parametrize("filename", ["keep", ".keep"])
def test_start_refuses_nonempty_uninitialised_directory(
        tmp_path: Path, filename: str) -> None:
    if subprocess.check_output(["bash", "-c", "id -u"], text=True).strip() == "0":
        parent = tmp_path
        temp_root = Path(tempfile.gettempdir()).resolve()
        while parent.is_relative_to(temp_root) and parent != temp_root:
            parent.chmod(parent.stat().st_mode | 0o111)
            parent = parent.parent
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = str(listener.getsockname()[1])
    directory = tmp_path / "unrelated"
    directory.mkdir()
    sentinel = directory / filename
    sentinel.write_text("keep me", encoding="utf-8")
    try:
        started = _run("start", str(directory), port, cwd=ROOT)
        assert started.returncode != 0, started.stdout
        assert "refusing" in started.stderr.lower()
        assert not started.stdout
        assert sentinel.read_text(encoding="utf-8") == "keep me"
        assert list(directory.iterdir()) == [sentinel]
    finally:
        if (directory / ".scratch_pg").exists():
            stopped = _run("stop", str(directory), cwd=ROOT)
            assert stopped.returncode == 0, stopped.stderr


def test_stop_refuses_uninitialised_directory(tmp_path: Path) -> None:
    directory = tmp_path / "unrelated"
    directory.mkdir()
    sentinel = directory / "keep"
    sentinel.write_text("keep me", encoding="utf-8")
    stopped = _run("stop", str(directory), "5443", cwd=ROOT)
    assert stopped.returncode != 0
    assert "marker" in stopped.stderr.lower()
    assert not stopped.stdout
    assert sentinel.read_text(encoding="utf-8") == "keep me"


@pytest.mark.parametrize("args", [
    (), ("start",), ("env",), ("stop",), ("start", ".pg"),
    ("env", ".pg", "bad"), ("start", ".pg", "54x"), ("stop", ".pg", "bad"),
])
def test_invalid_arguments_print_usage(args: tuple[str, ...], tmp_path: Path) -> None:
    result = _run(*args, cwd=tmp_path)
    assert result.returncode != 0
    assert "usage:" in result.stderr.lower()
    assert not result.stdout
    assert not (tmp_path / ".pg").exists()


@pytest.mark.parametrize("missing", [None, "pg_ctl", "initdb"])
def test_tool_check_resolves_executable_suffixes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str | None) -> None:
    bindir = tmp_path / "postgres"
    bindir.mkdir()
    for name in ("pg_ctl", "initdb"):
        if name != missing:
            executable = bindir / f"{name}.exe"
            executable.touch()
            executable.chmod(0o755)
    real_which = shutil.which

    def which(name: str, path: str | None = None) -> str | None:
        if path is None:
            return name if name in ("bash", "pg_config") else None
        assert path == str(bindir)
        # Model Windows suffix resolution while keeping real file checks.
        return real_which(f"{name}.exe", path=path)

    def pg_config(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args == ["pg_config", "--bindir"]
        return subprocess.CompletedProcess(args, 0, str(bindir), "")

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(subprocess, "run", pg_config)
    require_tools = inspect.unwrap(_require_tools)
    if missing:
        with pytest.raises(pytest.skip.Exception, match="Postgres binaries"):
            require_tools()
    else:
        try:
            require_tools()
        except pytest.skip.Exception as exc:
            pytest.fail(f"Installed .exe binaries were falsely skipped: {exc}")
