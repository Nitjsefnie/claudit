"""Server-shutdown reap of live export renders (issue #363).

A SIGTERM during an in-flight export used to leave the render child running
and its mkstemp PNG in TMPDIR: uvicorn cancels the request task at the
graceful-shutdown deadline, awaits the lifespan shutdown, then re-raises the
captured SIGTERM — so the handler's own cleanup races process death and
loses. The fix reaps live renders in the lifespan shutdown handler, which
uvicorn does await. This module boots the app on its scratch databases as a
subprocess and exercises exactly that sequence.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import shutil
import urllib.error
import urllib.request
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import psycopg
import pytest

from backend import api_export
from backend import session as session_mod
from tests import scratch_db

REPO_ROOT = Path(__file__).resolve().parents[1]

# Bounded waits. The graceful-shutdown window the test configures is 1 s,
# so the server's exit lands within a few seconds either way; the child the
# fix kills is SIGKILLed, and its death is polled with a deadline, never
# assumed.
_SERVER_EXIT_TIMEOUT_S = 30
_CHILD_DEATH_TIMEOUT_S = 10.0
_MARKER_TIMEOUT_S = 10.0
_HEALTH_TIMEOUT_S = 30.0
_POLL_STEP_S = 0.05
# The renderer stalls until its release file appears, so a leaked child
# never exits on its own inside the test's window.
_RELEASE_DEADLINE_S = 30

_TEST_USER_ID = 7
_SESSION_SECRET = "unit-test-session-secret"

_MARKER_ENV = "CLAUDIT_EXPORT_TEST_MARKER"
_RELEASE_ENV = "CLAUDIT_EXPORT_TEST_RELEASE"

# The stand-in renderer runs as
# `#!<python> <standin> <plot script> --output=<out>`: the shebang
# interpreter receives the stand-in file as its script, then the plot
# script path and the render argv behind it. It reads the output path
# from the --output= element (the shape build_export_argv emits since
# issue #380) and dies loudly if that element is absent, so an argv
# shape regression fails here instead of silently skipping the render.
# It records its pid, stalls until the release file appears (or its
# deadline passes) and only then writes the PNG — so a leaked child is
# visible and never self-cleans.
_STANDIN_SOURCE = f"""import os, sys, time
out = next(a for a in sys.argv
           if a.startswith("--output=")).split("=", 1)[1]
marker = os.environ[{_MARKER_ENV!r}]
release = os.environ[{_RELEASE_ENV!r}]
with open(marker, "w") as fh:
    fh.write(str(os.getpid()))
deadline = time.monotonic() + {_RELEASE_DEADLINE_S}
while not os.path.exists(release) and time.monotonic() < deadline:
    time.sleep(0.01)
if os.path.exists(release):
    with open(out, "wb") as fh:
        fh.write(b"png")
"""


# The layer-control renderer: records its pid, stalls until the release
# file appears (or its deadline passes), exits without writing output.
_UNIT_RENDERER_SOURCE = f"""import os, sys, time
marker, release = sys.argv[1], sys.argv[2]
with open(marker, "w") as fh:
    fh.write(str(os.getpid()))
deadline = time.monotonic() + {_RELEASE_DEADLINE_S}
while not os.path.exists(release) and time.monotonic() < deadline:
    time.sleep(0.01)
"""


def _unit_renderer_argv(marker: Path, release: Path,
                        out_path: Path) -> list[str]:
    return [sys.executable, "-c", _UNIT_RENDERER_SOURCE,
            str(marker), str(release), str(out_path)]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _poll(deadline_s: float, what: str, cond: Callable[[], Any]) -> Any:
    """Bounded poll; the deadline is an assertion, not a silence."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        value = cond()
        if value:
            return value
        time.sleep(_POLL_STEP_S)
    raise AssertionError(f"{what} not observed within {deadline_s:g}s")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _provision_auth(auth_db: str, viz_db: str) -> None:
    """users.config JSONB (schema_check's one requirement) plus a signed-in
    session for a fixed user id, minted without a login: the user_session
    row carries the same credential fingerprint the auth row's config
    hashes to."""
    config = {
        "web_password_hash": "pbkdf2_sha256$600000$abcd$" + "0" * 64,
        "web_password_salt": "abcd",
    }
    cred_fp = session_mod.credential_fingerprint(config)
    with psycopg.connect(f"postgresql:///{auth_db}", autocommit=True) as conn:
        # The repo owns no auth schema; schema_check() requires exactly one
        # thing — users.config as JSONB — so that is exactly what is built
        # (the same shape smoke.py provisions).
        conn.execute(
            "CREATE TABLE users (user_id BIGINT PRIMARY KEY, "
            "config JSONB NOT NULL DEFAULT '{}'::jsonb)")
        conn.execute(
            "INSERT INTO users (user_id, config) VALUES (%s, %s)",
            (_TEST_USER_ID, json.dumps(config)))
    with psycopg.connect(f"postgresql:///{viz_db}", autocommit=True) as conn:
        conn.execute(
            "INSERT INTO user_session (user_id, secret, cred_fp) "
            "VALUES (%s, %s, %s)",
            (_TEST_USER_ID, _SESSION_SECRET, cred_fp))


def session_cookie() -> str:
    return session_mod.make_session_token(_TEST_USER_ID, _SESSION_SECRET, 0)


def _wait_for_health(port: int, proc: subprocess.Popen) -> None:
    """The server answers /health; the process exiting during boot is the
    failure mode, so the poll checks that too."""
    def healthy() -> bool:
        if proc.poll() is not None:
            raise AssertionError(
                f"server exited during boot (rc={proc.returncode})")
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                return resp.status == 200
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            return False

    _poll(_HEALTH_TIMEOUT_S, "server /health", healthy)


@pytest.fixture(name="server")
def _server_fixture(tmp_path, monkeypatch):  # pylint: disable=too-many-locals
    """The real app under uvicorn on its own scratch databases, with a
    signed-in user and a stand-in renderer registered via EXPORT_PYTHON.

    Yields the server process and the paths the test's assertions and
    cleanups need.
    """
    viz_db = scratch_db.create_database("export_shutdown")
    auth_db = scratch_db.create_database("auth", schema=None)
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{viz_db}")
    monkeypatch.setenv("DATABASE_URL_AUTH", f"postgresql:///{auth_db}")
    _provision_auth(auth_db, viz_db)
    monkeypatch.setenv("R2_ENDPOINT",
                       f"file://{REPO_ROOT / 'fixtures' / 'r2_mini'}/")
    monkeypatch.setenv("R2_BUCKET", "claude")
    monkeypatch.setenv("R2_ACCOUNT_ID", "")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("CLAUDIT_WARM_CACHE", "0")

    tmpdir = tmp_path / "server_tmp"
    tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmpdir))

    # Coordination files live OUTSIDE pytest's tmp_path: they are read and
    # written by the server's render child, whose lifetime is not bounded
    # by the test's, and the test polls them while the server runs. dir=
    # is explicit because this fixture runs after TMPDIR was monkeypatched
    # for the server: the real /tmp is pinned here, not resolved through
    # tempfile's warmed gettempdir() cache.
    coord = Path(tempfile.mkdtemp(prefix="claudit-export363-",
                                  dir=tempfile.gettempdir()))
    marker = coord / "render_started"
    release = coord / "render_release"
    standin = coord / "export_standin"
    standin.write_text(f"#!{sys.executable}\n{_STANDIN_SOURCE}",
                       encoding="utf-8")
    standin.chmod(0o755)
    monkeypatch.setenv("EXPORT_PYTHON", str(standin))
    monkeypatch.setenv(_MARKER_ENV, str(marker))
    monkeypatch.setenv(_RELEASE_ENV, str(release))

    port = _free_port()
    with subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--timeout-graceful-shutdown", "1"],
        cwd=REPO_ROOT,
        env=dict(os.environ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as proc:
        state = SimpleNamespace(proc=proc, port=port, marker=marker,
                                release=release, tmpdir=tmpdir, coord=coord)
        try:
            _wait_for_health(port, proc)
            yield state
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=_SERVER_EXIT_TIMEOUT_S)
            release.touch(exist_ok=True)
            shutil.rmtree(coord, ignore_errors=True)
            log = proc.stdout.read() if proc.stdout is not None else ""
            (tmp_path / "server.log").write_text(log or "", encoding="utf-8")
            if proc.stdout is not None:
                proc.stdout.close()
            scratch_db.drop_database(viz_db)
            scratch_db.drop_database(auth_db)


def test_shutdown_during_export_reaps_child_and_unlinks_png(server):
    """SIGTERM mid-render: no render child survives the shutdown and the
    temporary directory is left without the export PNG — the two halves of
    issue #363."""
    state = server
    request = (
        f"GET /api/export?range=30d HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{state.port}\r\n"
        f"Cookie: session={session_cookie()}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("ascii")
    sock = socket.create_connection(("127.0.0.1", state.port), timeout=10)
    sock.sendall(request)
    sock.settimeout(0.1)

    response = bytearray()
    eof = False

    def drain() -> None:
        """Read whatever has arrived; the response completes at shutdown."""
        nonlocal eof
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    eof = True
                    return
                response.extend(chunk)
        except (TimeoutError, BlockingIOError):
            return

    try:
        pid_text = _poll(
            _MARKER_TIMEOUT_S, "render started marker",
            lambda: state.marker.read_text(encoding="ascii")
            if state.marker.exists() else "")
    except AssertionError:
        raise AssertionError(
            f"render never started; response so far: "
            f"{bytes(response[:200])!r}") from None
    child_pid = int(pid_text)
    # The absence assertion below is only meaningful if the oracle was
    # live: the render's mkstemp file must be present before the signal.
    assert list(state.tmpdir.iterdir()), (
        "render's mkstemp output missing from TMPDIR before SIGTERM")
    state.proc.send_signal(signal.SIGTERM)
    state.proc.wait(timeout=_SERVER_EXIT_TIMEOUT_S)
    drain()

    if os.name == "posix":
        child_dead = _poll(
            _CHILD_DEATH_TIMEOUT_S, "render child death",
            lambda: not _pid_alive(child_pid))
        assert child_dead, (
            f"render child {child_pid} outlived the server shutdown")
    assert not list(state.tmpdir.iterdir()), (
        "export PNG left in the temporary directory: "
        f"{[p.name for p in state.tmpdir.iterdir()]}")
    assert eof, "the server never finished the connection"


def test_reap_live_renders_kills_child_and_unlinks_output(tmp_path):
    """Layer-level control for reap_live_renders (issue #363 review): with
    a live child registered, the reap kills it, unlinks its output and
    empties the registry. Red on the unfixed code (no function to call)
    and on a mutant that drops either step."""
    marker = tmp_path / "u363_started"
    release = tmp_path / "u363_release"
    out_path = tmp_path / "u363_out.png"

    async def observed_within(description: str, cond) -> None:
        """Await a condition with the deadline as the assertion. The loop
        stays live: the render task needs its turns while we wait."""
        async def poll() -> None:
            while not cond():
                await asyncio.sleep(0.05)

        try:
            await asyncio.wait_for(poll(), timeout=_MARKER_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise AssertionError(
                f"{description} not observed within {_MARKER_TIMEOUT_S:g}s"
            ) from None

    async def run() -> None:
        render_task: asyncio.Task[Any] | None = None
        try:
            # The mkstemp file belongs to the HTTP handler; the render
            # itself only receives its path. Create it so the reap's
            # unlink has the same precondition as production.
            out_path.touch()
            render_task = asyncio.create_task(api_export._render_export(  # pylint: disable=protected-access
                _unit_renderer_argv(marker, release, out_path),
                str(out_path)))
            await observed_within("unit render marker", marker.exists)
            await observed_within(
                "unit render registration",
                lambda: next(iter(api_export._live_renders), None)  # pylint: disable=protected-access
                is not None)
            proc = next(iter(api_export._live_renders))  # pylint: disable=protected-access
            assert proc is not None
            assert out_path.exists()
            await asyncio.wait_for(api_export.reap_live_renders(),
                                   timeout=_CHILD_DEATH_TIMEOUT_S)
            assert proc.returncode is not None, (
                "reap left the render child alive")
            assert not out_path.exists(), "reap left the render output"
            assert not api_export._live_renders, (  # pylint: disable=protected-access
                "reap left the registry non-empty")
        finally:
            release.touch(exist_ok=True)
            if render_task is not None:
                if render_task.done():
                    if not render_task.cancelled():
                        render_task.exception()
                else:
                    render_task.cancel()
                    try:
                        await render_task
                    except (asyncio.CancelledError, Exception):
                        pass

    asyncio.run(run())
