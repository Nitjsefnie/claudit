"""The context-growth family: /api/context-growth/{agg,session,traces}.

`agg` and `session/{id}` moved verbatim out of api.py when `traces`
joined the family (issue #644); the module keeps one copy of the
per-file trace SQL, which the panel endpoint owns alone. The dashboard
no longer shares it: it needs only each main file's last turn, so it
runs `ctx_end_rows` — a files-only projection, no records join — instead
of the full trace query (issue #838).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from starlette.requests import Request

from backend import db, r2
from backend.api_common import _parse_range, Phases
from backend.cache import cache_response

router = APIRouter()


def ctx_traces_rows(c: Any, since: datetime, project: str | None = None) -> list:
    """Per-FILE ctx traces — one row per main file AND per sub-agent
    file with usage. The "Per-Session Context Growth" panel treats each
    file as its own conversation, so a sub-agent invocation surfaces
    under whatever model it ran on, even if there's no main session file
    on disk. Moved verbatim from api_dashboard (issue #644); the panel
    endpoint owns it alone — the dashboard's ctx_at_end fold reads
    `ctx_end_rows` instead (issue #838)."""
    proj_filter = "AND f.project_id = %s" if project else ""
    args: list[Any] = [since]
    if project:
        args.append(project)
    return c.execute(
        f"""
        WITH scoped_files AS (
          SELECT f.file_key, f.session_id, f.is_main, f.ctx_turns
          FROM files f
          WHERE f.r2_last_modified >= %s {proj_filter}
            AND jsonb_array_length(f.ctx_turns) > 0
        ),
        -- Scoped to the files actually returned. Unrestricted, this
        -- ran an ordered-set aggregate over every record in the
        -- table on every request, ignoring both range and project.
        file_models AS (
          SELECT r.file_key,
                 COALESCE(
                   MODE() WITHIN GROUP (ORDER BY r.model) FILTER (
                     WHERE r.model <> '' AND r.model <> '<synthetic>'
                   ),
                   MODE() WITHIN GROUP (ORDER BY NULLIF(r.model, ''))
                 ) AS model
          FROM records r
          WHERE r.is_canonical
            AND r.file_key IN (SELECT file_key FROM scoped_files)
          GROUP BY r.file_key
        )
        SELECT sf.file_key, sf.session_id, sf.is_main,
               COALESCE(fm.model, '') AS model,
               sf.ctx_turns
        FROM scoped_files sf
        LEFT JOIN file_models fm ON fm.file_key = sf.file_key
        """,
        args,
    ).fetchall()


def ctx_end_rows(c: Any, since: datetime, project: str | None = None) -> dict:
    """Per-session context-at-end for the dashboard's session fold.

    The dashboard needs ONLY the last ctx turn of each MAIN file — the
    session panel's ctx_at_end — but it used to run the panel's full
    per-file trace query (ctx_traces_rows, with its file_models CTE over
    records) and then throw away every trace but the last turn. This runs
    ONE query over `files` alone, with no records join (issue #838).

    `ctx_turns` is machine-written by backend/ctx_turns.py, which always
    writes an object carrying a POSITIVE int `input`, so the jsonb
    projection (`-> -1 ->> 'input'`) is exact for real data and the
    `try`/`except` is defensive only: its shapes are unreachable from
    that writer. Should one occur anyway, the fallback is None — NOT the
    old server-side fold's behaviour, which yielded 0 for a missing or
    None `input` and raised on a non-numeric string.

    Rows come back ordered by (session_id, file_key), and the dict is
    built in that order, so the LAST row for one session id wins —
    deterministic by file_key. The old
    `{sid: turns for ... if is_main}` comprehension it replaces was
    unordered, so when two main files shared a session id its pick was
    arbitrary rather than the highest file_key.
    """
    proj_filter = "AND f.project_id = %s" if project else ""
    args: list[Any] = [since]
    if project:
        args.append(project)
    rows = c.execute(
        f"""
        SELECT f.session_id, f.ctx_turns -> -1 ->> 'input'
        FROM files f
        WHERE f.r2_last_modified >= %s {proj_filter}
          AND f.is_main = TRUE
          AND jsonb_array_length(f.ctx_turns) > 0
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
            "turns": [
                int(t.get("input", 0) or 0)
                for t in (turns or [])
                if isinstance(t, dict)
            ],
        }
        for (_fk, _sid, _is_main, model, turns) in rows
    ]


@router.get("/context-growth/traces")
@cache_response
def context_growth_traces(
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
) -> dict:
    """The per-file context traces the Per-Session Context Growth panel
    draws (issue #644: fetched by the panel itself — the traces were 82%
    of the dashboard payload's gzip while drawing one panel of ~30)."""
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
       - per_session_final: the LAST turn of each MAIN file's ctx_turns
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
              SELECT ((turn->>'input')::int) AS input_int
              FROM files f, jsonb_array_elements(f.ctx_turns) AS turn
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
              SELECT ((f.ctx_turns -> -1 ->> 'input')::int) AS final_input
              FROM files f
              WHERE f.is_main = TRUE
                AND f.r2_last_modified >= %s {proj_filter}
                AND jsonb_array_length(f.ctx_turns) > 0
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
    """Main-file ctx_turns, without file_key for guests (issue #244)."""
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT file_key, ctx_turns, turn_count "
            "FROM files WHERE session_id = %s AND is_main = TRUE LIMIT 1",
            (session_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "session not found")
    file_key, turns, count = row
    final_ctx, is_guest = 0, bool(getattr(request.state, "is_guest", False))
    if turns:
        try:
            final_ctx = int(turns[-1].get("input", 0))
        except (KeyError, IndexError, TypeError):
            final_ctx = 0
    return {
        "session_id": session_id,
        **({} if is_guest else {"file_key": r2.public_key(file_key)}),
        "turns": turns,
        "total_turns": count,
        "final_ctx": final_ctx,
    }
