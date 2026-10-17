"""GET /api/export: render the reference matplotlib dashboard in a
subprocess against the viz DB and stream its PNG.
"""
from __future__ import annotations


import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from starlette.responses import Response

from backend.env_config import getenv
from backend import branding, flags
from backend.api_common import _parse_range
from scripts.plots.ccusage_plot_features import has_drawable_features

router = APIRouter()

# System python (has matplotlib + psycopg); the app .venv does not. Override
# via CLAUDIT_EXPORT_PYTHON for dev/test boxes where matplotlib lives elsewhere.
_EXPORT_PYTHON = getenv("CLAUDIT_EXPORT_PYTHON", "/usr/bin/python3")
_EXPORT_SCRIPT = str(Path(__file__).resolve().parents[1] / "scripts/plots/ccusage_plot_db.py")
_EXPORT_TIMEOUT_S = 120
_export_lock = asyncio.Semaphore(1)

# Live renders: spawned process -> its mkstemp output path, from spawn
# until the render completes or is reaped. The lifespan shutdown consults
# it: uvicorn's cancellation of the in-flight request task
# cannot be relied on to unwind the handler's own cleanup before the
# process exits.
_live_renders: dict[asyncio.subprocess.Process, str] = {}

# A plot child that dies with this in its stderr is almost always the
# CLAUDIT_EXPORT_PYTHON interpreter lacking matplotlib (dev-only: requirements-dev.txt,
# not backend/requirements.txt) or psycopg — an environment problem, not a
# render failure, so it earns a 503 that names the fix.
_MISSING_MODULE_RE = re.compile(r"ModuleNotFoundError")

# No live export's file can be older than this: the render subprocess is
# bounded at _EXPORT_TIMEOUT_S and the handler unlinks its output on every
# exit path, so a claudit_export_* file older than twice that is an orphan
# of a process that died mid-render. The wide margin keeps a
# live export in a process sharing the tmp directory (a dev box without the
# unit's PrivateTmp) from ever being swept.
_STALE_EXPORT_MIN_AGE_S = 2 * _EXPORT_TIMEOUT_S

# The mkstemp prefix the export handler writes; the sweep must target
# exactly what this module creates.
_EXPORT_TMP_PREFIX = "claudit_export_"

# The plot script signals "no data for the range/project" with exit 3
# (scripts/plots/ccusage_plot_db.py's empty path), distinct
# from 1 (genuine failure) and 2 (argparse). An empty result set is a
# normal outcome, so it earns a 404, not a 500 that reads as a server
# fault.
_NO_DATA_EXIT = 3


def build_export_argv(rng: str, project: str | None, out_path: str) -> list[str]:
    """Construct the argv for the plot subprocess. Every value-taking
    option rides in --opt=value form — a single argv element — because a
    project id is a path slug that may start with '-' (every POSIX Claude
    project), which argparse would read as an option in the two-element
    space form. The child inherits CLAUDIT_DATABASE_URL_VIZ from the
    environment, so the DSN is NOT passed on the command line (keeps
    credentials out of the process list)."""
    argv = [_EXPORT_PYTHON, _EXPORT_SCRIPT, f"--output={out_path}"]
    if rng == "all":
        argv.append("--all")
    else:
        argv.append(f"--period={rng}")
    if project:
        argv.append(f"--project={project}")
    disabled = flags.CONFIG.disabled_features()
    if disabled:
        argv.append('--disabled-features=' + json.dumps(disabled, separators=(',', ':')))
    return argv


def export_filename(rng: str, project: str | None) -> str:
    """Safe download filename: <brand>_<project-or-all>_<range>.png —
    the brand name slugified the same way the project is. Public: the
    filename is part of the branding contract (every CLAUDIT_APP_NAME surfaces),
    and the branding tests pin it directly."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", branding.brand_name())
    proj_slug = re.sub(r"[^A-Za-z0-9._-]", "_", project) if project else "all"
    return f"{name}_{proj_slug}_{rng}.png"


async def _reap(proc: asyncio.subprocess.Process) -> bool:
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
    HTTPException(404) when the child signals no data for the range or
    project (exit 3 — an empty outcome, not a failure), HTTPException(503)
    on timeout, when the interpreter died of a missing module, or when the
    interpreter itself could not be launched (a CLAUDIT_EXPORT_PYTHON environment
    problem), HTTPException(500) on any other non-zero exit. The child is
    killed and its pipes are drained on timeout, cancellation, or another
    exception while communicating."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        # Spawn failures occur outside the guarded render region: a missing
        # or unlaunchable CLAUDIT_EXPORT_PYTHON raises an OSError here. Return
        # 503 with the resolved interpreter and fix, as for missing modules,
        # so an environment error is not an unhandled server failure.
        raise HTTPException(
            503,
            f"export render failed: CLAUDIT_EXPORT_PYTHON ({_EXPORT_PYTHON}) "
            f"could not be launched ({exc.__class__.__name__}: {exc}) — "
            "point CLAUDIT_EXPORT_PYTHON at an interpreter with backend/"
            "requirements.txt and requirements-dev.txt installed "
            "(matplotlib and psycopg must both be importable)",
        ) from None
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
        if proc.returncode == _NO_DATA_EXIT:
            raise HTTPException(
                404, "no data to render for the requested range or project"
            ) from None
        if proc.returncode != 0:
            text = (stderr or b"").decode("utf-8", "replace")
            tail = text[-500:]
            print(f"[export] render failed (rc={proc.returncode}): {tail}", file=sys.stderr)
            if _MISSING_MODULE_RE.search(text):
                raise HTTPException(
                    503,
                    f"export render failed: CLAUDIT_EXPORT_PYTHON ({_EXPORT_PYTHON}) "
                    "is missing a Python module (ModuleNotFoundError) — point "
                    "CLAUDIT_EXPORT_PYTHON at an interpreter with backend/requirements.txt "
                    "and requirements-dev.txt installed (matplotlib and psycopg "
                    "must both be importable)",
                )
            raise HTTPException(500, "export render failed")
    finally:
        _live_renders.pop(proc, None)


async def reap_live_renders(budget_s: float) -> None:
    """Kill every live render child and unlink its output during shutdown.

    Uvicorn cancels the export task at its graceful deadline, awaits
    lifespan shutdown, then re-raises SIGTERM. Handler cleanup can lose
    that race. Kill synchronously and never drain pipes: an orphaned
    grandchild can hold them open forever.

    budget_s bounds the WHOLE reap on one deadline shared by all children.
    A per-child bound would consume the ingest wait and row-close budgets.
    Kill and unlink every child even when no reap time remains. Zero is a
    legal budget."""
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
                # Use the same signal as an out-of-budget skip: this child outlived
                # the reap, possibly because an earlier child consumed its budget.
                print(f"[export] render child {proc.pid} still unreaped at "
                      "shutdown", file=sys.stderr)
        else:
            print(f"[export] render child {proc.pid} not reaped: the render "
                  "reap ran out of its share of the stop budget", file=sys.stderr)
        try:
            os.unlink(out_path)
        except OSError:
            pass


def sweep_stale_exports(directory: str | None = None) -> int:
    """Unlink aged export tmp files at lifespan startup.

    SIGKILL bypasses graceful cleanup and leaves partial PNGs. Sweep only
    files older than _STALE_EXPORT_MIN_AGE_S, beyond any live render's
    lifetime even when another process shares tmp. Leave unrelated,
    unstatable and unlinkable files alone. Return the number removed."""
    tmp = Path(directory or tempfile.gettempdir())
    cutoff = time.time() - _STALE_EXPORT_MIN_AGE_S
    removed = 0
    try:
        candidates = list(tmp.glob(f"{_EXPORT_TMP_PREFIX}*"))
    except OSError as exc:
        print(f"[export] stale-export sweep could not list {tmp}: {exc}",
              file=sys.stderr)
        return 0
    for path in candidates:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        print(f"[export] swept {removed} stale export file(s) from {tmp}",
              file=sys.stderr)
    return removed


@router.get("/export")
async def export_png(
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
):
    """Render the full matplotlib dashboard PNG for the active filters.
    Logged-in only (guests are blocked in session.auth_middleware)."""
    if not has_drawable_features(flags.CONFIG.disabled_features()):
        raise HTTPException(404, "no enabled panels to render")
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
