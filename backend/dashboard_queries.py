"""Dashboard SQL queries under the effective startup policy."""
from __future__ import annotations

import time

from backend import api_ctx_growth, flags
from backend.api_common import Phases
from backend.feature_payloads import hourly_enabled


def _dashboard_queries(c, ph: Phases, bucket_s: int, src: dict) -> dict:
    hourly_rows = []
    if hourly_enabled(flags.CONFIG):
        hourly_rows = ph.execute(
            "hourly", c, f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM u.hour) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS hour,
                   u.model,
                   COALESCE(u.long_context, FALSE) AS long_context,
                   u.provider,
                   SUM(u.fresh_tokens)      AS input_tokens,
                   SUM(u.output_tokens)     AS output_tokens,
                   SUM(u.eph5_tokens)       AS cache_5m_tokens,
                   SUM(u.eph1h_tokens)      AS cache_1h_tokens,
                   SUM(u.cache_read_tokens) AS cache_read_tokens,
                   SUM(u.thinking_tokens)   AS thinking_tokens,
                   SUM(u.cost_usd)          AS cost_usd,
                   SUM(u.requests)          AS requests,
                   COUNT(DISTINCT u.session_id) AS session_count
            {src["roll_src"]}
            GROUP BY 1, 2, 3, 4
            ORDER BY 1, 2, 3, 4
            """,
            src["roll_args"],
        ).fetchall()

    # Percentiles are the one thing that cannot be rolled up — p50/p90
    # of a union of hours is not derivable from per-hour p50/p90 — so
    # this stays a live pass, narrowed to the text-bearing rows.
    response_sizes_rows = []
    if flags.CONFIG.features["response_sizes"]:
        response_sizes_rows = ph.execute(
            "response_sizes", c, f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM d.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS bucket,
                   dmm.value AS model,
                   COUNT(*) AS n,
                   PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY d.text_chars) AS p50,
                   PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY d.text_chars) AS p90
            {src["canon_src"]}
              AND d.text_chars > 0
            GROUP BY 1, 2
            ORDER BY 1, 2
            """,
            src["canon_args"],
        ).fetchall()

    # Line churn per bucket, folded onto the hourly panel entries below.
    # Live pass over tool_uses (~313k rows, but the >0 predicate keeps
    # only edit/write calls — ~11% of them).
    churn_rows = []
    if flags.CONFIG.features["line_churn"]:
        churn_rows = ph.execute(
            "churn", c, f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM {src["churn_ts"]}) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS hour,
                   SUM(tu.lines_added)   AS lines_added,
                   SUM(tu.lines_deleted) AS lines_deleted
            {src["churn_src"]}
            GROUP BY 1
            ORDER BY 1
            """,
            src["churn_args"],
        ).fetchall()

    total_sessions_row = None
    if flags.CONFIG.features["summary"]:
        total_sessions_row = ph.execute(
            "total_sessions", c, f"""
            SELECT COUNT(DISTINCT u.session_id) AS n
            {src["roll_src"]}
            """,
            src["roll_args"],
        ).fetchone()

    # Per-project range cost for the "Cost by Project" panel, folded
    # out of the same rollup source as cost_by_model (range-, project-
    # and model-filtered alike). Sorted DESC here so the top-10/Other
    # fold below is a plain slice.
    cost_by_project_rows = []
    if flags.CONFIG.features["project_comparison"]:
        cost_by_project_rows = ph.execute(
            "cost_by_project", c, f"""
            SELECT u.project_id, SUM(u.cost_usd) AS cost_usd
            {src["roll_src"]}
            GROUP BY 1
            ORDER BY 2 DESC
            """,
            src["roll_args"],
        ).fetchall()

    # Per-project range tokens for the "Tokens by Project" panel — the
    # billed partition Tokens by Model sums (fresh + cache_creation +
    # cache_read + output; thinking_tokens is a SUBSET of output and is
    # never added, SV-SUBSET-TOKENS), from the same rollup/live source as
    # cost_by_project (the >=1h rollup or the same-named live subquery),
    # under the same range/project/model filters. Sorted DESC so the
    # top-10/Other fold below is a plain slice.
    tokens_by_project_rows = []
    if flags.CONFIG.features["project_comparison"]:
        tokens_by_project_rows = ph.execute(
            "tokens_by_project", c, f"""
            SELECT u.project_id,
                   SUM(u.fresh_tokens + u.output_tokens
                       + u.cache_creation_tokens + u.cache_read_tokens)
                     AS total_tokens
            {src["roll_src"]}
            GROUP BY 1
            ORDER BY 2 DESC
            """,
            src["roll_args"],
        ).fetchall()

    file_counts_row = None
    if flags.CONFIG.features["summary"]:
        file_counts_row = ph.execute(
            "file_counts", c, f"""
            -- The two EXISTS predicates were correlated subqueries evaluated once per file row (four of them, ~9.2k files); LEFT JOIN the sets instead.
            WITH files_with_records AS (SELECT file_id FROM records GROUP BY file_id),
            sessions_with_main AS (SELECT session_id FROM files WHERE is_main GROUP BY session_id)
            SELECT
              COUNT(*) FILTER (WHERE f.is_main AND fr.file_id IS NOT NULL) AS main_w_usage,
              COUNT(*) FILTER (WHERE f.is_main AND fr.file_id IS NULL) AS main_empty,
              COUNT(*) FILTER (WHERE NOT f.is_main) AS subagent_files,
              COUNT(DISTINCT f.session_id) FILTER (WHERE sm.session_id IS NULL AND fr.file_id IS NOT NULL) AS subagent_only_sessions,
              -- A prompt is substantive user text (instrumentation and
              -- interrupts excluded at parse time); a turn is a ctx_turn
              -- boundary with a usage-bearing reply. Both are counted PER
              -- OWN TIMESTAMP across every file in scope (main + subagent):
              -- a file modified in range can carry prompts sent before it
              -- (issue #214), and a missing or junk ts cannot be proven
              -- out of range, so it counts (all-range totals stay exactly
              -- the old SUMs). Typed NULLs include migrated junk prompt
              -- timestamps; in-range tests bind FIRST.
              COALESCE(SUM((SELECT COUNT(*) FILTER (
                  WHERE p IS NULL OR p >= %s)
                FROM unnest(f.prompt_ts) AS p)), 0) AS total_prompts,
              COALESCE(SUM((SELECT COUNT(*) FILTER (
                  WHERE t IS NULL OR t >= %s)
                FROM unnest(f.ctx_ts) AS t)), 0) AS total_turns
            FROM files f
            LEFT JOIN files_with_records fr ON fr.file_id = f.file_id
            LEFT JOIN sessions_with_main   sm ON sm.session_id = f.session_id
            WHERE f.r2_last_modified >= %s {src["proj_filter"]}
            """,
            [src["file_args"][0]] * 2 + list(src["file_args"]),
        ).fetchone()

    # The rollup already carries per-(session, hour, model) sums, so the
    # dominant model is argmax(requests) over the in-range rows — exactly
    # what MODE() WITHIN GROUP computed from the raw records, without
    # re-sorting them.
    sessions_rows = []
    if flags.CONFIG.features["sessions"] or flags.CONFIG.features["burn_rate"]:
        sessions_rows = ph.execute(
            "sessions", c, f"""
            WITH per_session_model AS (
              SELECT u.session_id, u.model,
                     SUM(u.requests)              AS requests,
                     SUM(u.fresh_tokens)          AS input_tokens,
                     SUM(u.output_tokens)         AS output_tokens,
                     SUM(u.cache_creation_tokens) AS cache_create_tokens,
                     SUM(u.cache_read_tokens)     AS cache_read_tokens,
                     SUM(u.cost_usd)              AS cost_usd,
                     MIN(u.first_ts)              AS first_ts,
                     MAX(u.last_ts)               AS last_ts
              {src["roll_src"]}
              GROUP BY 1, 2
            )
            SELECT session_id,
                   EXTRACT(EPOCH FROM MIN(first_ts))::float AS start_ts,
                   EXTRACT(EPOCH FROM MAX(last_ts))::float  AS end_ts,
                   SUM(requests)            AS requests,
                   SUM(input_tokens)        AS input_tokens,
                   SUM(output_tokens)       AS output_tokens,
                   SUM(cache_create_tokens) AS cache_create_tokens,
                   SUM(cache_read_tokens)   AS cache_read_tokens,
                   SUM(cost_usd)            AS cost_usd,
                   -- Prefer the most-used REAL model; a session whose only
                   -- rows are sub-agent '<synthetic>' still gets a label
                   -- rather than going blank.
                   (ARRAY_AGG(model ORDER BY
                      (model NOT IN ('unknown', '<synthetic>')) DESC,
                      requests DESC))[1] AS model,
                   -- Every distinct real model the session used, so
                   -- per-model panels include a session even when the
                   -- model isn't the dominant one.
                   ARRAY_REMOVE(
                     ARRAY_AGG(DISTINCT model) FILTER (
                       WHERE model NOT IN ('unknown', '<synthetic>')
                     ),
                     NULL
                   ) AS models_used
            FROM per_session_model
            GROUP BY session_id
            ORDER BY SUM(cost_usd) DESC NULLS LAST
            LIMIT 500
            """,
            src["roll_args"],
        ).fetchall()

    # Per-session context-at-end for the session fold below. This is NOT
    # the panel's trace query: the panel fetches
    # /api/context-growth/traces itself, so the dashboard
    # runs only the last turn of each main file — all ctx_at_end needs —
    # instead of the full per-file trace SQL.
    _t_ctx = time.perf_counter()
    ctx_end = (api_ctx_growth.ctx_end_rows(c, *src["file_args"])
               if flags.CONFIG.features["burn_rate"] else {})
    ph.mark("ctx_end", time.perf_counter() - _t_ctx)

    rl_rows = []
    if flags.CONFIG.features["rate_limits"]:
        rl_rows = ph.execute(
            "rate_limit_hits", c, f"""
            SELECT f.session_id, hit
            FROM files f, jsonb_array_elements(f.rate_limit_hits) AS hit
            WHERE f.r2_last_modified >= %s {src["proj_filter"]}
              AND jsonb_array_length(f.rate_limit_hits) > 0
              -- r2_last_modified is the file's mtime, not the hit's time:
              -- a file touched within range can still carry hits older
              -- than `since`, so filter on each hit's own ts too.
              --
              -- The cast is guarded, because casting a malformed ts RAISES
              -- (`invalid input syntax for type timestamp with time zone`)
              -- and the traceback would take out the WHOLE dashboard, not
              -- just this panel. Two halves to the guard:
              --   * pg_input_is_valid, not a regex — a shape test admits
              --     '2026-13-45T99:99:99Z' and '2026-02-30T00:00:00Z',
              --     which look like timestamps and still raise on cast.
              --     (PG16+; production is 17 and CI runs postgres:16.)
              --   * CASE, not `AND valid AND cast` — the planner may
              --     evaluate a bare cast before the test protecting it,
              --     while CASE's evaluation order is guaranteed.
              -- A hit that fails the test yields NULL, which fails the >=
              -- and is dropped, which is what we want for junk.
              AND (CASE WHEN pg_input_is_valid(hit->>'ts', 'timestamptz')
                        THEN (hit->>'ts')::timestamptz END) >= %s
            """,
            list(src["file_args"]) + [src["file_args"][0]],
        ).fetchall()

    return {
        "hourly": hourly_rows,
        "response_sizes": response_sizes_rows,
        "churn": churn_rows,
        "total_sessions_row": total_sessions_row,
        "cost_by_project": cost_by_project_rows,
        "tokens_by_project": tokens_by_project_rows,
        "file_counts_row": file_counts_row,
        "sessions": sessions_rows,
        "ctx_end": ctx_end,
        "rate_limits": rl_rows,
    }
