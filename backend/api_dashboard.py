"""GET /api/dashboard — hourly aggregates + sessions + ctx_at_end.

Split out of api.py (issue #8 module split). The endpoint body is broken
into _dashboard_sources (shared SQL fragments), _dashboard_queries (the
eight panel queries) and _dashboard_build (row folding) so no single
function trips the locals/branches/statements gates.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query
from starlette.requests import Request

from backend import db
from backend.api_common import Phases, _bucket_seconds, _parse_range
from backend.cache import cache_response
from backend.dashboard_queries import _dashboard_queries
from backend.dashboard_build import (
    _dashboard_build, _fold_tokens_by_project, TOKEN_TYPE_FIELDS,
    drop_zero_token_types, surviving_token_types,
)

__all__ = [
    "_fold_tokens_by_project", "TOKEN_TYPE_FIELDS",
    "drop_zero_token_types", "surviving_token_types",
]

router = APIRouter()


def _rollup_source(use_rollup: bool, roll_proj: str, roll_model: str) -> str:
    """The FROM+WHERE fragment for the pure-sums panels.

    Pre-aggregated source for everything that is a pure sum/count/min/max.
    `usage_rollup` is grain (session_id, hour, model) and is rebuilt at
    ingest, so the panels read numbers that are already summed instead of
    re-aggregating ~285k records on every request.

    It is only usable when the display bucket is at least an hour wide —
    the 24h view buckets at 5 minutes, which an hourly rollup cannot
    express. For those the live subquery below is shaped with the SAME
    column names, so every query after this point is written once.
    """
    if use_rollup:
        roll_from = "usage_rollup u"
        # date_trunc so the partial hour containing `since` is included
        # rather than silently dropped.
        roll_since = "u.hour >= date_trunc('hour', %s::timestamptz)"
    else:
        roll_from = """(
          SELECT f.session_id, f.project_id, f.is_main,
                 r.ts AS hour, r.ts AS first_ts, r.ts AS last_ts,
                 r.model AS model,
                 COALESCE(r.long_context, FALSE) AS long_context,
                 COALESCE(r.provider, '') AS provider,
                 1::bigint AS requests,
                 r.fresh_tokens, r.output_tokens, r.cache_creation_tokens,
                 r.cache_read_tokens, r.eph5_tokens, r.eph1h_tokens,
                 r.thinking_tokens, r.cost_usd
            FROM records r
            JOIN files f ON f.file_key = r.file_key
           WHERE r.is_canonical AND r.ts IS NOT NULL
        ) u"""
        roll_since = "u.hour >= %s"
    return f"""
        FROM {roll_from}
        WHERE {roll_since} {roll_proj} {roll_model}
    """


def _churn_source(project: str | None, model: str | None,
                  since: datetime, use_rollup: bool) -> dict[str, Any]:
    """The FROM+WHERE fragment (and its args) for the line-churn query.

    Churn lives on tool calls, so hourly-or-coarser buckets use tool_rollup
    while sub-hour buckets retain the live tool_uses path. Both filter on
    the call's own model (tool_uses.model, rolled up as-is), so a call on a
    line with no record still matches.
    """
    if use_rollup:
        proj_filter = "AND tu.project_id = %s" if project else ""
        model_filter = "AND tu.model LIKE %s" if model else ""
        args: list[Any] = [since]
        if project:
            args.append(project)
        if model:
            args.append(f"%{model}%")
        return {
            "sql": f"""
                FROM tool_rollup tu
                WHERE tu.hour >= date_trunc('hour', %s::timestamptz)
                  {proj_filter} {model_filter}
                  AND (tu.lines_added > 0 OR tu.lines_deleted > 0)
            """,
            "args": args,
            "ts": "tu.hour",
        }

    proj_filter = "AND f.project_id = %s" if project else ""
    model_filter = ""
    args = [since]
    if project:
        args.append(project)
    if model:
        model_filter = "AND tu.model LIKE %s"
        args.append(f"%{model}%")
    return {
        "sql": f"""
            FROM tool_uses tu
            JOIN files f ON f.file_key = tu.file_key
            WHERE tu.ts >= %s {proj_filter} {model_filter} AND tu.is_canonical
              AND (tu.lines_added > 0 OR tu.lines_deleted > 0)
        """,
        "args": args,
        "ts": "tu.ts",
    }


def _dashboard_sources(project: str | None, model: str | None,
                       since: datetime, bucket_s: int) -> dict:
    """Shared SQL fragments + their param lists for the panel queries."""
    proj_filter = ""
    model_filter = ""
    # `file_args` feeds the later per-FILE queries, which filter on
    # f.r2_last_modified + project only — never on model. Keep it free of
    # the model argument or every one of them mis-binds its placeholders.
    file_args: list[Any] = [since]
    if project:
        proj_filter = "AND f.project_id = %s"
        file_args.append(project)
    if model:
        model_filter = "AND d.model LIKE %s"

    # Cross-file uuid dedup is resolved at ingest into records.is_canonical
    # (see ingest.recompute_canonical), so reads filter a boolean instead of
    # re-sorting 296k rows per request. That also removes the ON COMMIT DROP
    # temp table this used to materialise: with dedup reduced to a WHERE
    # clause there is nothing expensive left to share between the queries,
    # and each one now scans records directly via records_canonical_ts_idx.
    canon_src = f"""
        FROM records d
        JOIN files f ON f.file_key = d.file_key
        WHERE d.ts >= %s AND d.is_canonical {proj_filter} {model_filter}
    """
    canon_args: list[Any] = [since]
    if project:
        canon_args.append(project)
    if model:
        canon_args.append(f"%{model}%")

    roll_proj = "AND u.project_id = %s" if project else ""
    roll_model = "AND u.model LIKE %s" if model else ""
    roll_src = _rollup_source(bucket_s >= 3600, roll_proj, roll_model)
    roll_args: list[Any] = [since]
    if project:
        roll_args.append(project)
    if model:
        roll_args.append(f"%{model}%")

    churn = _churn_source(project, model, since, bucket_s >= 3600)

    return {
        "canon_src": canon_src,
        "canon_args": canon_args,
        "roll_src": roll_src,
        "roll_args": roll_args,
        "churn_src": churn["sql"],
        "churn_args": churn["args"],
        "churn_ts": churn["ts"],
        "proj_filter": proj_filter,
        "file_args": file_args,
    }


@router.get("/dashboard")
def dashboard_route(
    request: Request,
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
    fresh: int = Query(0),
) -> dict:
    """Route wrapper around the cached dashboard payload.

    The cached body carries the enabled project-comparison fields
    (one cache entry shared by every caller, so `cache.warm(dashboard)`
    keeps working and a guest never triggers a second compute). Guests
    get the same payload MINUS those two keys: per-project names/costs/
    tokens are exactly what the guest gates on /api/projects and on
    project= exist to withhold (see session.auth_middleware), and
    /api/dashboard is guest-accessible. The dict comprehension copies
    rather than mutating so the cached object stays intact for later
    non-guest hits."""
    payload = dashboard(rng=rng, project=project, model=model, fresh=fresh)
    if bool(getattr(request.state, "is_guest", False)):
        payload = {k: v for k, v in payload.items()
                   if k not in ("cost_by_project", "tokens_by_project")}
    return payload


@cache_response
def dashboard(
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
    fresh: int = Query(0),
) -> dict:
    """Hourly aggregates + per-session burns + per-session ctx_at_end.

    Cross-file uuid dedup is resolved at ingest into
    records.is_canonical (ingest.recompute_canonical); the queries here
    just filter that boolean — there is no temp table (#790). Legacy
    NULL-uuid rows are kept verbatim and always canonical.
    `model=opus-4-7` filters the deduped body so every panel derived
    from it is constrained to records matching the model substring.
    cost_by_project is folded from the same rollup source as
    cost_by_model, and tokens_by_project beside it measures the billed
    token partition per project; the route wrapper strips BOTH
    per-project keys for guests."""
    delta = _parse_range(rng)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    src = _dashboard_sources(project, model, since, bucket_s)

    ph = Phases("dashboard")
    _t_sql = time.perf_counter()
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        rows = _dashboard_queries(c, ph, bucket_s, src)
    ph.mark("sql_total", time.perf_counter() - _t_sql)

    _t_build = time.perf_counter()
    out = _dashboard_build(rows, rng, project, bucket_s)
    ph.mark("build", time.perf_counter() - _t_build)
    ph.done(
        hourly=len(out.get("hourly", [])),
        sessions=len(out.get("sessions", [])),
        ctx_end=len(rows["ctx_end"]),
    )
    return out
