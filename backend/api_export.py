"""GET /api/export — render the full matplotlib dashboard PNG.

Split out of api.py (issue #8 module split). Runs the reference plot
script as a subprocess against the viz DB and streams back the PNG.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from starlette.responses import Response

from backend import branding
from backend.api_common import _parse_range

router = APIRouter()

# Export-PNG render plumbing -------------------------------------------------
# System python (has matplotlib + psycopg); the app .venv does not. Override
# via EXPORT_PYTHON for dev/test boxes where matplotlib lives elsewhere.
_EXPORT_PYTHON = os.environ.get("EXPORT_PYTHON", "/usr/bin/python3")
_EXPORT_SCRIPT = str(Path(__file__).resolve().parents[1] / "scripts/plots/ccusage_plot_db.py")
_EXPORT_TIMEOUT_S = 120
_export_lock = asyncio.Semaphore(1)

# Live renders: spawned process -> its mkstemp output path, from spawn
# until the render completes or is reaped. The lifespan shutdown consults
# it (issue #363): uvicorn's cancellation of the in-flight request task
# cannot be relied on to unwind the handler's own cleanup before the
# process exits.
_live_renders: dict[asyncio.subprocess.Process, str] = {}

# A plot child that dies with this in its stderr is almost always the
# EXPORT_PYTHON interpreter lacking matplotlib (dev-only: requirements-dev.txt,
# not backend/requirements.txt) or psycopg — an environment problem, not a
# render failure, so it earns a 503 that names the fix (issue #115).
_MISSING_MODULE_RE = re.compile(r"ModuleNotFoundError")


def build_export_argv(rng: str, project: str | None, out_path: str) -> list[str]:
    """Construct the argv for the plot subprocess. Every value-taking
    option rides in --opt=value form — a single argv element — because a
    project id is a path slug that may start with '-' (every POSIX Claude
    project), which argparse would read as an option in the two-element
    space form (issue #380). The child inherits DATABASE_URL_VIZ from the
    environment, so the DSN is NOT passed on the command line (keeps
    credentials out of the process list)."""
    argv = [_EXPORT_PYTHON, _EXPORT_SCRIPT, f"--output={out_path}"]
    if rng == "all":
        argv.append("--all")
    else:
        argv.append(f"--period={rng}")
    if project:
        argv.append(f"--project={project}")
    return argv


def export_filename(rng: str, project: str | None) -> str:
    """Safe download filename: <brand>_<project-or-all>_<range>.png —
    the brand name slugified the same way the project is. Public: the
    filename is part of the branding contract (every APP_NAME surfaces),
    and the branding tests pin it directly."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", branding.brand_name())
    proj_slug = re.sub(r"[^A-Za-z0-9._-]", "_", project) if project else "all"
    return f"{name}_{proj_slug}_{rng}.png"


async def _reap(proc: asyncio.subprocess.Process) -> bool:
    """Kill a running child, drain its pipes, and wait until it is reaped."""
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    communicate_task = asyncio.create_task(proc.communicate())
    cancelled = False
    try:
        while not communicate_task.done():
            try:
                await asyncio.shield(communicate_task)
            except asyncio.CancelledError:
                cancelled = True
        communicate_task.result()
    except Exception as exc:
        print(f"[export] failed to drain child pipes while reaping: {exc}", file=sys.stderr)
        wait_task = asyncio.create_task(proc.wait())
        while not wait_task.done():
            try:
                await asyncio.shield(wait_task)
            except asyncio.CancelledError:
                cancelled = True
        wait_task.result()
    return cancelled


async def _render_export(argv: list[str], out_path: str) -> None:
    """Run the plot subprocess, bounded by _EXPORT_TIMEOUT_S. Raises
    HTTPException(503) on timeout or when the interpreter died of a missing
    module (an EXPORT_PYTHON environment problem), HTTPException(500) on
    any other non-zero exit. The child is killed and its pipes are drained on
    timeout, cancellation, or another exception while communicating."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _live_renders[proc] = out_path
    try:
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_EXPORT_TIMEOUT_S)
        except asyncio.TimeoutError:
            cancelled = await _reap(proc)
            if cancelled:
                raise asyncio.CancelledError() from None
            raise HTTPException(503, "export render timed out") from None
        except BaseException:
            await _reap(proc)
            raise
        if proc.returncode != 0:
            text = (stderr or b"").decode("utf-8", "replace")
            tail = text[-500:]
            print(f"[export] render failed (rc={proc.returncode}): {tail}", file=sys.stderr)
            if _MISSING_MODULE_RE.search(text):
                raise HTTPException(
                    503,
                    f"export render failed: EXPORT_PYTHON ({_EXPORT_PYTHON}) "
                    "is missing a Python module (ModuleNotFoundError) — point "
                    "EXPORT_PYTHON at an interpreter with backend/requirements.txt "
                    "and requirements-dev.txt installed (matplotlib and psycopg "
                    "must both be importable)",
                )
            raise HTTPException(500, "export render failed")
    finally:
        _live_renders.pop(proc, None)


async def reap_live_renders(budget_s: float) -> None:
    """Kill every live render child and unlink its output.

    Called from the lifespan shutdown handler (issue #363): uvicorn
    cancels the in-flight export task at the graceful-shutdown deadline,
    awaits the lifespan shutdown, then re-raises the captured SIGTERM —
    so the handler's own cleanup races process death and can lose, and
    even where it wins, `_reap`'s pipe drain can block forever when the
    renderer's own child inherits its pipes. The kill here is
    synchronous; the pipes are deliberately not drained — the process is
    exiting, and a drain waits for an EOF an orphaned grandchild may
    never deliver.

    `budget_s` bounds the reap as a WHOLE, on one deadline shared by the
    live children, rather than once per child (issue #414). The reap runs
    ahead of the lifespan's bounded ingest wait, so a per-child bound is
    drawn N times over: a stop arriving with two wedged children spent
    more than the whole teardown's share of TimeoutStopSec here, and the
    ingest row close behind it never ran. Every child is killed and
    unlinked whatever the budget is left of — both are instant — so what
    runs out is the reaping of a wedged child, which is a process the stop
    was going to kill regardless. Zero is a legal budget.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, budget_s)
    for proc, out_path in list(_live_renders.items()):
        _live_renders.pop(proc, None)
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        remaining = deadline - loop.time()
        if remaining > 0:
            try:
                await asyncio.wait_for(proc.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                # Reported the same way as the out-of-budget skip below:
                # this one line is the only signal that a render child
                # outlived the reap, and an earlier child eating the whole
                # budget is exactly the case issue #414 is about.
                print(f"[export] render child {proc.pid} still unreaped at "
                      "shutdown", file=sys.stderr)
        else:
            print(f"[export] render child {proc.pid} not reaped: the render "
                  "reap ran out of its share of the stop budget", file=sys.stderr)
        try:
            os.unlink(out_path)
        except OSError:
            pass


@router.get("/export")
async def export_png(
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
):
    """Render the full matplotlib dashboard PNG for the active filters.
    Logged-in only (guests are blocked in session.auth_middleware)."""
    _parse_range(rng)  # validation only — raises HTTPException(400) on garbage
    if _export_lock.locked():
        raise HTTPException(503, "an export is already in progress; try again shortly")
    fd, out_path = tempfile.mkstemp(suffix=".png", prefix="claudit_export_")
    os.close(fd)
    try:
        argv = build_export_argv(rng, project, out_path)
        async with _export_lock:
            await _render_export(argv, out_path)
        with open(out_path, "rb") as fh:
            png = fh.read()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "Content-Disposition":
                f'attachment; filename="{export_filename(rng, project)}"'
        },
    )
