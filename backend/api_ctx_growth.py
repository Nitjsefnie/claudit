"""Context-growth /api/context-growth/{agg,session,traces} endpoints.

Owns per-file trace SQL. The dashboard needs only each main file's last turn
and uses the files-only ctx_end_rows projection instead.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from starlette.requests import Request

from backend import db, flags, r2
from backend.api_common import _parse_range, Phases
from backend.cache import cache_response
from backend.file_timelines import ctx_objects

router = APIRouter()


def ctx_traces_rows(c: Any, since: datetime, project: str | None = None) -> list:
    """Per-file traces for main and subagent files with usage.

    Each file is its own conversation, even without a main session file.
    This panel owns the trace query; the dashboard's ctx_at_end fold reads
    ctx_end_rows instead."""
    proj_filter = "AND f.project_id = %s" if project else ""
    args: list[Any] = [since]
    if project:
        args.append(project)
    return c.execute(
        f"""
        WITH scoped_files AS (
          SELECT f.file_id, f.file_key, f.session_id, f.is_main, f.ctx_input
          FROM files f
          WHERE f.r2_last_modified >= %s {proj_filter}
            AND cardinality(f.ctx_input) > 0
        ),
        -- Scoped to the files actually returned. Unrestricted, this
        -- ran an ordered-set aggregate over every record in the
        -- table on every request, ignoring both range and project.
        file_models AS (
          SELECT r.file_id,
                 COALESCE(
                   MODE() WITHIN GROUP (ORDER BY dm.value) FILTER (
                     WHERE dm.value <> '' AND dm.value <> '<synthetic>'
                   ),
                   MODE() WITHIN GROUP (ORDER BY NULLIF(dm.value, ''))
                 ) AS model
          FROM records r
          JOIN dim_model dm ON dm.id = r.model_id
          WHERE r.is_canonical
            AND r.file_id IN (SELECT file_id FROM scoped_files)
          GROUP BY r.file_id
        )
        SELECT sf.file_key, sf.session_id, sf.is_main,
               COALESCE(fm.model, '') AS model,
               sf.ctx_input
        FROM scoped_files sf
        LEFT JOIN file_models fm ON fm.file_id = sf.file_id
        """,
        args,
    ).fetchall()


def ctx_end_rows(c: Any, since: datetime, project: str | None = None) -> dict:
    """Last context input of each main file for the dashboard's session fold.

    Read files alone: full traces and a records join are unnecessary for
    the final input. A NULL input converts defensively to None.

    Rows are ordered by (session_id, file_key); the last row wins for each
    session, making multiple main files resolve to the highest file_key."""
    proj_filter = "AND f.project_id = %s" if project else ""
    args: list[Any] = [since]
    if project:
        args.append(project)
    rows = c.execute(
        f"""
        SELECT f.session_id, f.ctx_input[cardinality(f.ctx_input)]
        FROM files f
        WHERE f.r2_last_modified >= %s {proj_filter}
          AND f.is_main = TRUE
          AND cardinality(f.ctx_input) > 0
        ORDER BY f.session_id, f.file_key
        """,
        args,
    ).fetchall()
    out: dict[str, int | None] = {}
    for sid, text in rows:
        try:
            out[sid] = int(text)
        except (TypeError, ValueError):
            out[sid] = None
    return out


def project_traces(rows: list) -> list[dict]:
    """The traces endpoint's wire projection: {model, turns}, turns
    flattened to positionally-indexed ctx ints."""
    return [
        {
            "model": model or "",
            "turns": [int(value or 0) for value in (turns or [])],
        }
        for (_fk, _sid, _is_main, model, turns) in rows
    ]


@router.get("/context-growth/traces")
@cache_response
def context_growth_traces(
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
) -> dict:
    """Per-file traces fetched by the Context Growth panel itself, keeping
    full traces out of the shared dashboard payload."""
    delta = _parse_range(rng)
    since = datetime.now(timezone.utc) - delta

    ph = Phases("ctx_traces")
    with db.viz_conn() as c:
        rows = ctx_traces_rows(c, since, project)
    ph.done(files=len(rows))
    return {"traces": project_traces(rows)}


@router.get("/context-growth/agg")
@cache_response
def context_growth_agg(
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
) -> dict:
    """Distribution stats for context size, computed two ways:
       - per_turn: every turn across every file in scope (input distribution)
       - per_session_final: the LAST context input of each MAIN file
    Returns mean, p50, p90, p99, max, n for both."""
    delta = _parse_range(rng)
    since = datetime.now(timezone.utc) - delta
    proj_filter = ""
    args: list[Any] = [since]
    if project:
        proj_filter = "AND f.project_id = %s"
        args.append(project)

    with db.viz_conn() as c:
        per_turn = c.execute(
            db.sql_text(f"""
            SELECT
              COUNT(*) AS n,
              AVG(input_int) AS mean,
              PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY input_int) AS p50,
              PERCENTILE_CONT(0.9)  WITHIN GROUP (ORDER BY input_int) AS p90,
              PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY input_int) AS p99,
              MAX(input_int) AS max
            FROM (
              SELECT input_int
              FROM files f, unnest(f.ctx_input) AS input_int
              WHERE f.r2_last_modified >= %s {proj_filter}
            ) t
            """),
            args,
        ).fetchone()

        per_session = c.execute(
            db.sql_text(f"""
            SELECT
              COUNT(*) AS n,
              AVG(final_input) AS mean,
              PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY final_input) AS p50,
              PERCENTILE_CONT(0.9)  WITHIN GROUP (ORDER BY final_input) AS p90,
              PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY final_input) AS p99,
              MAX(final_input) AS max
            FROM (
              SELECT f.ctx_input[cardinality(f.ctx_input)] AS final_input
              FROM files f
              WHERE f.is_main = TRUE
                AND f.r2_last_modified >= %s {proj_filter}
                AND cardinality(f.ctx_input) > 0
            ) t
            """),
            args,
        ).fetchone()

    def _stats(row):
        if row is None:
            return {"n": 0, "mean": 0, "p50": 0, "p90": 0, "p99": 0, "max": 0}
        n, mean, p50, p90, p99, mx = row
        return {
            "n": int(n or 0),
            "mean": int(mean or 0),
            "p50": int(p50 or 0),
            "p90": int(p90 or 0),
            "p99": int(p99 or 0),
            "max": int(mx or 0),
        }

    return {
        "range": rng,
        "project": project,
        "per_turn": _stats(per_turn),
        "per_session_final": _stats(per_session),
    }


@router.get("/context-growth/session/{session_id}")
def context_growth_session(request: Request, session_id: str) -> dict:
    """Main-file context turns, without file_key for guests."""
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT file_key, ctx_ts, ctx_line, ctx_input, ctx_output, turn_count "
            "FROM files WHERE session_id = %s AND is_main = TRUE LIMIT 1",
            (session_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "session not found")
    file_key, timestamps, lines, inputs, outputs, count = row
    turns = ctx_objects(timestamps, lines, inputs, outputs)
    final_ctx = int(inputs[-1] or 0) if inputs else 0
    is_guest = bool(getattr(request.state, "is_guest", False))
    return {
        "session_id": session_id,
        **({} if is_guest else {"file_key": r2.public_key(file_key)}),
        "turns": turns,
        **({"total_turns": count} if flags.CONFIG.collection.enabled("file_extras") else {}),
        "final_ctx": final_ctx,
    }
