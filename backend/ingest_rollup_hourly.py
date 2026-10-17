"""Seven hour-keyed rollups, sharing their full and scoped SELECT bodies."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from backend import db
from backend.constants import CTX_BUCKET_MAX, CTX_BUCKET_WIDTH
from backend.ingest_scope import Scope, current_scope

log = logging.getLogger("claudit.ingest")
_SOURCE_TABLES = {"r": "records", "tu": "tool_uses"}


def _phase_scope(scope: Scope | None) -> Scope | None:
    return scope if scope is not None else current_scope()


def _valid_hours(scope: Scope) -> tuple[list[str], list[datetime]]:
    pairs = sorted((project_id, hour) for project_id, hour in scope.dirty_hours
                   if hour is not None)
    return [pair[0] for pair in pairs], [pair[1] for pair in pairs]


def _merged_candidate_intervals(
        scope: Scope) -> tuple[list[str], list[datetime], list[datetime]]:
    by_project: dict[str, list[tuple[datetime, datetime]]] = {}
    for project_id, hour in scope.dirty_hours:
        if hour is None:
            continue
        utc_hour = hour.astimezone(timezone.utc)
        by_project.setdefault(project_id, []).append((
            utc_hour - timedelta(hours=1),
            utc_hour + timedelta(hours=2),
        ))

    projects: list[str] = []
    starts: list[datetime] = []
    ends: list[datetime] = []
    for project_id, intervals in sorted(by_project.items()):
        ordered = sorted(intervals)
        start, end = ordered[0]
        for next_start, next_end in ordered[1:]:
            if next_start <= end:
                end = max(end, next_end)
                continue
            projects.append(project_id)
            starts.append(start)
            ends.append(end)
            start, end = next_start, next_end
        projects.append(project_id)
        starts.append(start)
        ends.append(end)
    return projects, starts, ends


def _scope_filter(source_alias: str, scope: Scope | None) -> tuple[str, dict]:
    if scope is None or scope.full:
        return "", {}
    projects, hours = _valid_hours(scope)
    if not projects:
        return "FALSE AND ", {}
    return (
        f"EXISTS (SELECT 1 "
        f"FROM unnest(%(projects)s::text[], %(hours)s::timestamptz[]) "
        f"AS d(project_id, hour) "
        f"WHERE f.project_id = d.project_id "
        f"AND date_trunc('hour', {source_alias}.ts) = d.hour) AND ",
        {"projects": projects, "hours": hours},
    )


def _scoped_source(source_alias: str, scope: Scope | None
                   ) -> tuple[str, str, dict]:
    source_table = _SOURCE_TABLES[source_alias]
    price_join = (
        " LEFT JOIN records_price rp ON rp.file_id = r.file_id"
        " AND rp.line_num = r.line_num" if source_alias == "r" else ""
    )
    full_source = (
        f"{source_table} {source_alias} "
        f"JOIN files f ON f.file_id = {source_alias}.file_id{price_join}"
    )
    if scope is None or scope.full:
        return "", full_source, {}

    projects, starts, ends = _merged_candidate_intervals(scope)
    if not projects:
        return "", full_source, {}
    cte = (
        "WITH scoped_source AS MATERIALIZED ("
        f" SELECT candidate.source_row, f AS file_row "
        " FROM unnest(%(range_projects)s::text[], "
        "%(range_starts)s::timestamptz[], "
        "%(range_ends)s::timestamptz[]) AS d(project_id, range_start, range_end) "
        " CROSS JOIN LATERAL ("
        f"SELECT {source_alias} AS source_row "
        f"FROM {source_table} {source_alias} "
        f"WHERE {source_alias}.ts >= d.range_start "
        f"AND {source_alias}.ts < d.range_end "
        f"AND {source_alias}.is_canonical"
        ") AS candidate "
        " JOIN files f "
        "ON f.file_id = (candidate.source_row).file_id "
        "AND f.project_id = d.project_id"
        ")"
    )
    source = (
        "scoped_source candidate_rows "
        "CROSS JOIN LATERAL "
        f"(SELECT (candidate_rows.source_row).*) AS {source_alias} "
        "CROSS JOIN LATERAL "
        "(SELECT (candidate_rows.file_row).*) AS f" + price_join
    )
    return cte, source, {
        "range_projects": projects,
        "range_starts": starts,
        "range_ends": ends,
    }


def _delete_scope(conn, table: str, scope: Scope | None) -> dict:
    if scope is None or scope.full:
        conn.execute(f"DELETE FROM {table}")
        return {}
    projects, hours = _valid_hours(scope)
    if not projects:
        return {}
    params = {"projects": projects, "hours": hours}
    # Scope keys stay TEXT project ids (the scope's own vocabulary);
    # the rollups store proj_id, so the pairs resolve through
    # projects here — one join over a handful of pairs.
    conn.execute(
        f"""DELETE FROM {table} t
              USING unnest(%(projects)s::text[], %(hours)s::timestamptz[])
                    AS d(project_id, hour),
                    projects p
             WHERE p.project_id = d.project_id
               AND t.proj_id = p.proj_id AND t.hour = d.hour""",
        params,
    )
    return params


def _run_hourly(table: str, insert_sql: str, source_alias: str,
                scope: Scope | None, params: dict | None = None) -> int:
    if scope is not None and not scope.full and not _valid_hours(scope)[0]:
        return 0
    values = dict(params or {})
    with db.viz_conn() as conn:
        conn.execute("SET LOCAL work_mem = '64MB'")
        values.update(_delete_scope(conn, table, scope))
        scope_cte, source_from, source_params = _scoped_source(
            source_alias, scope)
        values.update(source_params)
        scope_filter, filter_params = _scope_filter(source_alias, scope)
        values.update(filter_params)
        cur = conn.execute(
            db.sql_text(insert_sql.format(
                scope_cte=scope_cte, source_from=source_from,
                scope_filter=scope_filter)),
            values or None)
        written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
    return written


def ensure_sessions() -> None:
    """Insert every stored file's (project, session) into `sessions`.

    usage_rollup's grain is ses_id, so the rows must exist
    before any rollup row can reference one. `files` is small, so the
    DISTINCT pass beats threading a per-file upsert through persist.

    The NOT EXISTS anti-join is what keeps a rebuild from spending a
    ses_id per session it merely re-observes: the INSERT's tuple — and
    with it the DEFAULT nextval — is formed before ON CONFLICT sees the
    conflict, so rows already stored must never reach the INSERT at all.
    DO NOTHING stays for the race against a concurrent rebuild.
    """
    with db.viz_conn() as conn:
        conn.execute(
            """
            INSERT INTO sessions (proj_id, session_id)
            SELECT DISTINCT p.proj_id, f.session_id
              FROM files f
              JOIN projects p ON p.project_id = f.project_id
             WHERE NOT EXISTS (
                     SELECT 1 FROM sessions s
                      WHERE s.proj_id = p.proj_id
                        AND s.session_id = f.session_id)
            ON CONFLICT (proj_id, session_id) DO NOTHING
            """
        )
        conn.commit()


def ensure_agent_type_dims() -> None:
    """Insert stored files.agent_type TEXT values before joining their dim ids.

    The anti-join keeps existing values away from INSERT, so DEFAULT
    nextval spends no id on a conflict. DO NOTHING protects the concurrent
    race without locking every existing row as DO UPDATE would."""
    with db.viz_conn() as conn:
        conn.execute(
            """
            INSERT INTO dim_agent_type (value)
            SELECT DISTINCT agent_type FROM files f
             WHERE NOT EXISTS (SELECT 1 FROM dim_agent_type d
                                WHERE d.value = f.agent_type)
            ON CONFLICT (value) DO NOTHING
            """
        )
        conn.commit()


def rebuild_rollup(scope: Scope | None = None) -> int:
    scope = _phase_scope(scope)
    ensure_sessions()
    written = _run_hourly(
        "usage_rollup",
        """
        INSERT INTO usage_rollup (
          ses_id, proj_id, hour, model_id, is_main, long_context,
          provider_id, first_ts, last_ts, requests,
          fresh_tokens, output_tokens, cache_creation_tokens,
          cache_read_tokens, eph5_tokens, eph1h_tokens,
          thinking_tokens, cost_usd
        )
        {scope_cte}
        SELECT s.ses_id, p.proj_id, date_trunc('hour', r.ts) AS hour,
               r.model_id, f.is_main,
               COALESCE(rp.long_context, FALSE) AS long_context,
               COALESCE(r.provider_id, 0) AS provider_id,
               MIN(r.ts), MAX(r.ts), COUNT(*),
               COALESCE(SUM(r.fresh_tokens), 0),
               COALESCE(SUM(r.output_tokens), 0),
               COALESCE(SUM(r.cache_creation_tokens), 0),
               COALESCE(SUM(r.cache_read_tokens), 0),
               COALESCE(SUM(r.eph5_tokens), 0),
               COALESCE(SUM(r.eph1h_tokens), 0),
               COALESCE(SUM(r.thinking_tokens), 0),
               COALESCE(SUM(rp.cost_usd), 0)
          FROM {source_from}
          JOIN projects p ON p.project_id = f.project_id
          JOIN sessions s ON s.proj_id = p.proj_id
                        AND s.session_id = f.session_id
         WHERE {scope_filter}r.is_canonical AND r.ts IS NOT NULL
         GROUP BY 1, 2, 3, 4, 5, 6, 7
        """,
        "r", scope,
    )
    log.info("rebuild_rollup: %d rows", written)
    return written


def rebuild_tool_rollup(scope: Scope | None = None) -> int:
    scope = _phase_scope(scope)
    written = _run_hourly(
        "tool_rollup",
        """
        INSERT INTO tool_rollup (
          hour, proj_id, model_id, tool_name_id, n_total, n_rated, n_error,
          lines_added, lines_deleted
        )
        {scope_cte}
        SELECT date_trunc('hour', tu.ts) AS hour, p.proj_id,
               COALESCE(tu.model_id, 0) AS model_id, tu.tool_name_id,
               COUNT(*) AS n_total,
               COUNT(*) FILTER (WHERE tu.is_error IS NOT NULL) AS n_rated,
               COUNT(*) FILTER (WHERE tu.is_error) AS n_error,
               SUM(tu.lines_added) AS lines_added,
               SUM(tu.lines_deleted) AS lines_deleted
          FROM {source_from}
          JOIN projects p ON p.project_id = f.project_id
         WHERE {scope_filter}tu.ts IS NOT NULL AND tu.is_canonical
         GROUP BY 1, 2, 3, 4
        """,
        "tu", scope,
    )
    log.info("rebuild_tool_rollup: %d rows", written)
    return written


def rebuild_tool_error_rollup(scope: Scope | None = None) -> int:
    scope = _phase_scope(scope)
    written = _run_hourly(
        "tool_error_rollup",
        """
        INSERT INTO tool_error_rollup (
          hour, proj_id, model_id, tool_name_id, error_kind_id, n
        )
        {scope_cte}
        SELECT date_trunc('hour', tu.ts) AS hour, p.proj_id,
               COALESCE(tu.model_id, 0) AS model_id, tu.tool_name_id,
               tu.error_kind_id, COUNT(*) AS n
          FROM {source_from}
          JOIN projects p ON p.project_id = f.project_id
         WHERE {scope_filter}tu.ts IS NOT NULL AND tu.error_kind_id IS NOT NULL
           AND tu.is_canonical
         GROUP BY 1, 2, 3, 4, 5
        """,
        "tu", scope,
    )
    log.info("rebuild_tool_error_rollup: %d rows", written)
    return written


def rebuild_dispatch_rollup(scope: Scope | None = None) -> int:
    scope = _phase_scope(scope)
    written = _run_hourly(
        "dispatch_rollup",
        """
        INSERT INTO dispatch_rollup (hour, proj_id, agent_type_id,
                                     agent_model_id, n)
        {scope_cte}
        SELECT date_trunc('hour', tu.ts) AS hour, p.proj_id,
               tu.agent_type_id,
               COALESCE(tu.agent_model_id, 0) AS agent_model_id,
               COUNT(*) AS n
          FROM {source_from}
          JOIN projects p ON p.project_id = f.project_id
         WHERE {scope_filter}tu.ts IS NOT NULL AND tu.agent_type_id IS NOT NULL
           AND tu.is_canonical
         GROUP BY 1, 2, 3, 4
        """,
        "tu", scope,
    )
    log.info("rebuild_dispatch_rollup: %d rows", written)
    return written


def rebuild_dispatch_brief_rollup(scope: Scope | None = None) -> int:
    scope = _phase_scope(scope)
    written = _run_hourly(
        "dispatch_brief_rollup",
        """
        INSERT INTO dispatch_brief_rollup (
          hour, proj_id, agent_type_id, brief_ref, n, prompt_chars
        )
        {scope_cte}
        SELECT date_trunc('hour', tu.ts) AS hour, p.proj_id,
               COALESCE(tu.agent_type_id, 0) AS agent_type_id,
               tu.dispatch_brief_ref AS brief_ref, COUNT(*) AS n,
               COALESCE(SUM(tu.dispatch_prompt_chars), 0) AS prompt_chars
          FROM {source_from}
          JOIN projects p ON p.project_id = f.project_id
         WHERE {scope_filter}tu.ts IS NOT NULL AND tu.dispatch_brief_ref IS NOT NULL
           AND tu.is_canonical
         GROUP BY 1, 2, 3, 4
        """,
        "tu", scope,
    )
    log.info("rebuild_dispatch_brief_rollup: %d rows", written)
    return written


def rebuild_ctx_cost_rollup(scope: Scope | None = None) -> int:
    scope = _phase_scope(scope)
    written = _run_hourly(
        "ctx_cost_rollup",
        """
        INSERT INTO ctx_cost_rollup (
          hour, proj_id, model_id, ctx_bucket, requests, total_tokens,
          cost_usd
        )
        {scope_cte}
        SELECT date_trunc('hour', r.ts) AS hour, p.proj_id, r.model_id,
               CASE WHEN r.fresh_tokens + r.cache_creation_tokens
                             + r.cache_read_tokens >= %(mx)s THEN %(mx)s
                    WHEN r.fresh_tokens + r.cache_creation_tokens
                             + r.cache_read_tokens <= 0 THEN 0
                    ELSE ((r.fresh_tokens + r.cache_creation_tokens
                           + r.cache_read_tokens) / %(w)s) * %(w)s
               END AS ctx_bucket,
               COUNT(*) AS requests,
               SUM(r.fresh_tokens + r.cache_creation_tokens
                   + r.cache_read_tokens + r.output_tokens) AS total_tokens,
               SUM(rp.cost_usd) AS cost_usd
          FROM {source_from}
          JOIN projects p ON p.project_id = f.project_id
         WHERE {scope_filter}r.is_canonical AND r.ts IS NOT NULL
         GROUP BY 1, 2, 3, 4
        """,
        "r", scope, {"mx": CTX_BUCKET_MAX, "w": CTX_BUCKET_WIDTH},
    )
    log.info("rebuild_ctx_cost_rollup: %d rows", written)
    return written


def rebuild_agent_rollup(scope: Scope | None = None) -> int:
    scope = _phase_scope(scope)
    ensure_agent_type_dims()
    written = _run_hourly(
        "agent_rollup",
        """
        INSERT INTO agent_rollup (
          hour, proj_id, model_id, agent_type_id, requests, output_tokens,
          total_tokens, cost_usd
        )
        {scope_cte}
        SELECT date_trunc('hour', r.ts) AS hour, p.proj_id, r.model_id,
               dat.id AS agent_type_id, COUNT(*) AS requests,
               SUM(r.output_tokens) AS output_tokens,
               SUM(r.fresh_tokens + r.cache_creation_tokens
                   + r.cache_read_tokens + r.output_tokens) AS total_tokens,
               SUM(rp.cost_usd) AS cost_usd
          FROM {source_from}
          JOIN projects p ON p.project_id = f.project_id
          JOIN dim_agent_type dat ON dat.value = f.agent_type
         WHERE {scope_filter}r.is_canonical AND r.ts IS NOT NULL
         GROUP BY 1, 2, 3, 4
        """,
        "r", scope,
    )
    log.info("rebuild_agent_rollup: %d rows", written)
    return written
