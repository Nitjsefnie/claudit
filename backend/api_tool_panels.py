"""Tool usage and error-rate panels, retaining the cached callable seams."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query

from backend import db
from backend.api_common import _bucket_seconds, _iso, _parse_range
from backend.cache import cache_response

router = APIRouter()


def _tool_usage_source(bucket_s: int, project: str | None,
                       model: str | None, since: datetime) -> tuple:
    """(FROM, ts column, count expr, since predicate, tail, args) for the
    bucketed tool-usage query.

    tool_rollup pre-aggregates (hour, project, model, tool); see
    ingest.rebuild_tool_rollup. Same bucket-width gate as /api/dashboard:
    the 24h view buckets finer than an hour and takes the live path, which
    filters on the call's own model (a dim id, joined) and, like the rollup,
    counts
    only canonical calls (a compaction sidecar's replays are not; see
    SV-CANONICAL-FLAG). Args come back in the order
    the placeholders appear in the SQL string: since (in WHERE), then
    project, then model (both in the tail).
    """
    args: list[Any] = [since]
    if project:
        args.append(project)
    if model:
        args.append(f"%{model}%")
    model_filter = "AND dm.value LIKE %s" if model else ""
    if bucket_s >= 3600:
        proj_filter = "AND pj.project_id = %s" if project else ""
        return (
            "tool_rollup tu JOIN dim_tool_name tn ON tn.id = tu.tool_name_id"
            " JOIN dim_model dm ON dm.id = tu.model_id"
            " JOIN projects pj ON pj.proj_id = tu.proj_id",
            "tu.hour", "SUM(tu.n_total)",
            "tu.hour >= date_trunc('hour', %s::timestamptz)",
            f"{proj_filter} {model_filter}", args,
        )

    proj_filter = "AND f.project_id = %s" if project else ""
    return (
        "tool_uses tu JOIN files f ON f.file_id = tu.file_id"
        " JOIN dim_tool_name tn ON tn.id = tu.tool_name_id"
        " LEFT JOIN dim_model dm ON dm.id = tu.model_id",
        "tu.ts", "COUNT(*)", "tu.ts >= %s AND tu.is_canonical",
        f"{proj_filter} {model_filter}", args,
    )


@router.get("/tool-usage")
@cache_response
def tool_usage(
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Bucketed tool-call counts. Bucket size = largest in [60s, 1d]
    that yields ≥100 bins across the range. Frontend stacks to 100%
    and promotes any tool that ever cracked top-N at any bucket.
    Tools that never make the cut land in 'Other'.

    `model=opus-4-7` filters to tool calls whose own model matches
    the substring."""
    delta = _parse_range(rng)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    tu_from, ts_col, cnt, since_pred, tail, args = (
        _tool_usage_source(bucket_s, project, model, since)
    )

    with db.viz_conn() as c:
        rows = c.execute(
            db.sql_text(f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM {ts_col}) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS bucket,
                   tn.value  AS tool,
                   {cnt}        AS n
            FROM {tu_from}
            WHERE {since_pred} {tail}
            GROUP BY 1, 2
            ORDER BY 1, 2
            """),
            args,
        ).fetchall()

    return {
        "range": rng,
        "project": project,
        "bucket_s": bucket_s,
        "buckets": [
            {"ts": _iso(b), "tool": t, "n": int(n or 0)}
            for (b, t, n) in rows
        ],
    }


def _tool_error_rate_sql(bucket_s: int, project: str | None,
                         model: str | None, args: list) -> str:
    """The bucketed (n_total, n_error) per (model, tool_name) query.

    Args appended in the order placeholders appear in the SQL string:
    tu.ts >= %s (already in `args`), then project, then model.
    """
    if bucket_s >= 3600:
        # n_rated/n_error already encode "is_error IS NOT NULL"; dm.value
        # <> '' drops rows stored before tool_uses.model existed, exactly
        # as the live path below does.
        proj_filter = ""
        if project:
            proj_filter = "AND pj.project_id = %s"
            args.append(project)
        model_filter = ""
        if model:
            model_filter = "AND dm.value LIKE %s"
            args.append(f"%{model}%")
        return f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM hour) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS bucket,
                   dm.value AS model, tn.value AS tool,
                   SUM(n_rated) AS n_total,
                   SUM(n_error) AS n_error
            FROM tool_rollup tr
            JOIN dim_model dm ON dm.id = tr.model_id
            JOIN dim_tool_name tn ON tn.id = tr.tool_name_id
            JOIN projects pj ON pj.proj_id = tr.proj_id
            WHERE hour >= date_trunc('hour', %s::timestamptz)
              AND dm.value <> ''
              {proj_filter}
              {model_filter}
            GROUP BY 1, 2, 3
            HAVING SUM(n_rated) > 0
            ORDER BY 1, 2, 3
        """
    proj_filter = ""
    if project:
        proj_filter = "AND f.project_id = %s"
        args.append(project)
    model_filter = ""
    if model:
        model_filter = "AND dm.value LIKE %s"
        args.append(f"%{model}%")
    return f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM tu.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS bucket,
                   dm.value     AS model,
                   tn.value     AS tool,
                   COUNT(*)                              AS n_total,
                   COUNT(*) FILTER (WHERE tu.is_error)   AS n_error
            FROM tool_uses tu
            JOIN files   f ON f.file_id = tu.file_id
            JOIN dim_tool_name tn ON tn.id = tu.tool_name_id
            LEFT JOIN dim_model dm ON dm.id = tu.model_id
            WHERE tu.is_error IS NOT NULL AND tu.is_canonical
              AND dm.value <> ''
              AND tu.ts >= %s
              {proj_filter}
              {model_filter}
            GROUP BY 1, 2, 3
            ORDER BY 1, 2, 3
        """


@router.get("/tool-error-rate")
@cache_response
def tool_error_rate(
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Bucketed (n_total, n_error) per (model, tool_name) over settled
    tool calls only (is_error IS NOT NULL). The frontend computes
    error-rate = n_error / n_total per series and EMA-smooths the
    sequence.

    `model` is an optional model substring filter (parity with
    /api/tool-usage). Cross-file uuid dedup does NOT apply — tool_uses
    aren't keyed on records.uuid; the natural boundary is per-file."""
    delta = _parse_range(rng)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    args: list[Any] = [since]
    sql = _tool_error_rate_sql(bucket_s, project, model, args)

    with db.viz_conn() as c:
        rows = c.execute(db.sql_text(sql), args).fetchall()

    return {
        "range": rng,
        "project": project,
        "bucket_s": bucket_s,
        "buckets": [
            {"ts": _iso(b), "model": m, "tool": t,
             "n_total": int(nt or 0), "n_error": int(ne or 0)}
            for (b, m, t, nt, ne) in rows
        ],
    }
