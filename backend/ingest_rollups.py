"""Derived-state rebuilds: what ingest recomputes from the parsed rows.

Split out of backend/ingest.py, which crossed pylint's 1000-line gate —
the same seam api.py was split along. Nothing here fetches, parses or
persists; every function reads what the ingest walk already wrote and
rewrites one derived table -- or, for resolve_teammate_agent_types, the
derived `files.agent_type` of named teammates, joined across files. They
are called in order by ingest._rebuild_derived_state(), and that order is
load-bearing: suppression removes rows the canonical pass would otherwise
rank, every rollup reads is_canonical, and agent_rollup reads the
teammates' resolved agent_type.

Re-exported from backend.ingest so existing callers keep working.
"""
from __future__ import annotations

import logging

from backend import db
from backend.constants import (CTX_BUCKET_MAX, CTX_BUCKET_WIDTH,
                               DEFAULT_AGENT_TYPE, LATENCY_BUCKETS)

log = logging.getLogger("claudit.ingest")


def purge_suppressed() -> int:
    """Delete `records` for models listed in `suppressed_models`.

    Claude Code writes every session under one tree no matter which
    endpoint served it, so resuming a session on the other lane
    interleaves that provider's assistant entries into a transcript this
    deploy's bucket already owns. Those rows are real usage but not ours,
    and pricing them against our rate table invents a cost.

    Suppression is a DELETE rather than a read-time filter because every
    read path and every rollup already reads `records` as the truth; one
    deletion keeps them all consistent without fifteen extra predicates
    that a new endpoint could forget. `tool_uses` for the same lines go
    too -- matched on the call's OWN model, since most calls sit on a line
    with no record (a Claude tool_use usually follows its requestId's
    merged record; a lane call never shares a line with one). The
    same-line join is kept ONLY for rows stored before tool_uses.model
    existed (model IS NULL until the next reparse); a call that carries a
    model is judged by it alone.

    Patterns are matched with ILIKE, so 'glm-%' covers a family and a bare
    model id still matches exactly. Runs before the canonical pass on
    EVERY ingest, not only when files changed, so adding a pattern takes
    effect on the next run. Removing one brings the rows back only on a
    reparse (bump PARSER_VERSION). Returns the number of records deleted.
    """
    with db.viz_conn() as c:
        row = c.execute("SELECT EXISTS (SELECT 1 FROM suppressed_models)"
                        ).fetchone()
        if not row or not row[0]:
            return 0
        c.execute(
            """
            DELETE FROM tool_uses tu
             WHERE EXISTS (SELECT 1 FROM suppressed_models s
                            WHERE tu.model ILIKE s.pattern)
            """
        )
        c.execute(
            """
            DELETE FROM tool_uses tu
             USING records r
             WHERE tu.model IS NULL
               AND tu.file_key = r.file_key
               AND tu.line_num = r.line_num
               AND EXISTS (SELECT 1 FROM suppressed_models s
                            WHERE r.model ILIKE s.pattern)
            """
        )
        cur = c.execute(
            """
            DELETE FROM records r
             WHERE EXISTS (SELECT 1 FROM suppressed_models s
                            WHERE r.model ILIKE s.pattern)
            """
        )
        deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    if deleted:
        log.info("purge_suppressed: %d records dropped", deleted)
    return deleted


def resolve_teammate_agent_types() -> int:
    """Set each named teammate's `files.agent_type` from its lead's dispatch.

    A teammate's sidecar names the teammate, not reliably a role, so ingest
    stores that name as `files.teammate_name` beside the role the parse
    could take (parse.apply_agent_sidecar: the sidecar's agentType when it
    differs from the name, otherwise the default). When a dispatch in the
    same session has that `dispatch_name`, its `agent_type` (subagent_type,
    or the default when the call named none) replaces it: the latest one
    that did not fail and was made at or before the teammate's first
    record, since a name can be dispatched again. With none, the parsed
    role stands.

    A join across files, so it runs on every ingest, before the rollups
    that read `files.agent_type`: a lead archived after its teammate
    resolves it on that run without a reparse. Returns rows changed.
    """
    with db.viz_conn() as c:
        cur = c.execute(
            """
            WITH resolved AS (
              SELECT DISTINCT ON (f.file_key) f.file_key,
                     COALESCE(tu.agent_type, %(default)s) AS agent_type
                FROM files f
                JOIN files p ON p.project_id = f.project_id
                            AND p.session_id = f.session_id
                JOIN tool_uses tu ON tu.file_key = p.file_key
               WHERE f.teammate_name IS NOT NULL
                 AND tu.dispatch_name = f.teammate_name
                 AND tu.is_error IS NOT TRUE
                 AND tu.ts <= COALESCE(
                       (SELECT min(r.ts) FROM records r
                         WHERE r.file_key = f.file_key),
                       'infinity')
               ORDER BY f.file_key, tu.ts DESC, tu.line_num DESC
            )
            UPDATE files f SET agent_type = resolved.agent_type
              FROM resolved
             WHERE f.file_key = resolved.file_key
               AND f.agent_type <> resolved.agent_type
            """,
            {"default": DEFAULT_AGENT_TYPE},
        )
        changed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("resolve_teammate_agent_types: %d rows", changed)
    return changed


def recompute_canonical() -> int:
    """Resolve cross-file uuid dedup into `records.is_canonical`.

    Marks exactly the row that ``DISTINCT ON (r.uuid) ... ORDER BY r.uuid,
    r.file_key`` used to pick at read time, so the read endpoints can
    filter on a boolean instead of sorting the whole table on every
    request. ``line_num`` breaks ties within a file_key, which the old
    read-time ORDER BY left arbitrary.

    Rows with a NULL uuid are legacy records kept verbatim (they were the
    UNION ALL leg), so they are always canonical.

    Runs after EVERY successful ingest, not only when files changed: a
    freshly-migrated DB has the column defaulted to TRUE across the board,
    and skipping the pass on a no-op ingest would leave duplicates
    double-counted until something happened to change. The UPDATE only
    touches rows whose flag actually flips, so a steady-state pass writes
    nothing. Returns the number of rows changed.
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        cur = c.execute(
            """
            UPDATE records r
               SET is_canonical = w.canon
              FROM (
                    SELECT file_key, line_num,
                           (uuid IS NULL OR ROW_NUMBER() OVER (
                              PARTITION BY uuid ORDER BY file_key, line_num
                            ) = 1) AS canon
                      FROM records
                   ) w
             WHERE r.file_key = w.file_key
               AND r.line_num = w.line_num
               AND r.is_canonical IS DISTINCT FROM w.canon
            """
        )
        changed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        # Same rule for tool calls, keyed on the globally unique
        # tool_use.id. A compaction sidecar (agent-acompact-*) replays the
        # main file's assistant lines, tool_use blocks included; without
        # this every rollup over tool_uses counted those calls twice.
        cur = c.execute(
            """
            UPDATE tool_uses t
               SET is_canonical = w.canon
              FROM (
                    SELECT file_key, line_num, idx,
                           (tool_use_id IS NULL OR ROW_NUMBER() OVER (
                              PARTITION BY tool_use_id
                              ORDER BY file_key, line_num, idx
                            ) = 1) AS canon
                      FROM tool_uses
                   ) w
             WHERE t.file_key = w.file_key
               AND t.line_num = w.line_num
               AND t.idx = w.idx
               AND t.is_canonical IS DISTINCT FROM w.canon
            """
        )
        changed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    if changed:
        log.info("recompute_canonical: %d rows reflagged", changed)
    return changed


def rebuild_rollup() -> int:
    """Rebuild `usage_rollup` from the canonical records.

    Full rebuild rather than an incremental merge: cross-file uuid dedup
    means a newly ingested FILE can demote a record that another file
    already contributed, so touched-files-only would leave stale sums
    behind. The whole table is ~1 row per (session, hour, model), which is
    a fraction of `records`, so rebuilding it costs one pass.

    Must run AFTER recompute_canonical() — it reads is_canonical.
    Returns the row count written.
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        # DELETE, not TRUNCATE: TRUNCATE takes an ACCESS EXCLUSIVE lock for
        # the whole rebuild transaction, so every concurrent read of this
        # table blocks until it commits — measured at 2.9s for a SELECT that
        # normally takes 0.07s. Since the rebuild runs on every ingest, that
        # stalled readers hourly. DELETE takes ROW EXCLUSIVE and MVCC keeps
        # serving the previous rows until commit, so readers never wait; at
        # a few thousand rows the extra cost is noise.
        c.execute("DELETE FROM usage_rollup")
        cur = c.execute(
            """
            INSERT INTO usage_rollup (
              session_id, project_id, hour, model, is_main, long_context,
              provider, first_ts, last_ts, requests,
              fresh_tokens, output_tokens, cache_creation_tokens,
              cache_read_tokens, eph5_tokens, eph1h_tokens,
              thinking_tokens, cost_usd
            )
            SELECT f.session_id,
                   f.project_id,
                   date_trunc('hour', r.ts)                    AS hour,
                   COALESCE(NULLIF(r.model, ''), 'unknown')    AS model,
                   f.is_main,
                   COALESCE(r.long_context, FALSE)             AS long_context,
                   COALESCE(r.provider, '')                    AS provider,
                   MIN(r.ts), MAX(r.ts), COUNT(*),
                   COALESCE(SUM(r.fresh_tokens), 0),
                   COALESCE(SUM(r.output_tokens), 0),
                   COALESCE(SUM(r.cache_creation_tokens), 0),
                   COALESCE(SUM(r.cache_read_tokens), 0),
                   COALESCE(SUM(r.eph5_tokens), 0),
                   COALESCE(SUM(r.eph1h_tokens), 0),
                   COALESCE(SUM(r.thinking_tokens), 0),
                   COALESCE(SUM(r.cost_usd), 0)
              FROM records r
              JOIN files f ON f.file_key = r.file_key
             WHERE r.is_canonical AND r.ts IS NOT NULL
             GROUP BY 1, 2, 3, 4, 5, 6, 7
            """
        )
        written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_rollup: %d rows", written)
    return written


def rebuild_tool_rollup() -> int:
    """Rebuild `tool_rollup` from tool_uses + files.

    The model is the call's own tool_uses.model, never a join to
    `records` on (file_key, line_num): most calls sit on a line with no
    record, and that join labelled them ''. model='' now marks only a row
    stored before the column existed; /api/tool-error-rate excludes it,
    /api/tool-usage still counts it.
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        # DELETE, not TRUNCATE: TRUNCATE takes an ACCESS EXCLUSIVE lock for
        # the whole rebuild transaction, so every concurrent read of this
        # table blocks until it commits — measured at 2.9s for a SELECT that
        # normally takes 0.07s. Since the rebuild runs on every ingest, that
        # stalled readers hourly. DELETE takes ROW EXCLUSIVE and MVCC keeps
        # serving the previous rows until commit, so readers never wait; at
        # a few thousand rows the extra cost is noise.
        c.execute("DELETE FROM tool_rollup")
        cur = c.execute(
            """
            INSERT INTO tool_rollup (
              hour, project_id, model, tool_name, n_total, n_rated, n_error,
              lines_added, lines_deleted
            )
            SELECT date_trunc('hour', tu.ts)              AS hour,
                   f.project_id,
                   COALESCE(tu.model, '')                 AS model,
                   tu.tool_name,
                   COUNT(*)                               AS n_total,
                   COUNT(*) FILTER (
                     WHERE tu.is_error IS NOT NULL
                   )                                      AS n_rated,
                   COUNT(*) FILTER (WHERE tu.is_error)    AS n_error,
                   SUM(tu.lines_added)                     AS lines_added,
                   SUM(tu.lines_deleted)                   AS lines_deleted
              FROM tool_uses tu
              JOIN files f    ON f.file_key = tu.file_key
             WHERE tu.ts IS NOT NULL AND tu.is_canonical
             GROUP BY 1, 2, 3, 4
            """
        )
        written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_tool_rollup: %d rows", written)
    return written


def rebuild_tool_error_rollup() -> int:
    """Rebuild `tool_error_rollup` from tool_uses + files.

    Mirrors rebuild_tool_rollup so the two agree: same model (the call's
    own tool_uses.model, '' only on a pre-column row), same hour
    truncation. Only settled failures contribute.
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        # DELETE not TRUNCATE, for the reason rebuild_tool_rollup gives.
        c.execute("DELETE FROM tool_error_rollup")
        cur = c.execute(
            """
            INSERT INTO tool_error_rollup (
              hour, project_id, model, tool_name, error_kind, n
            )
            SELECT date_trunc('hour', tu.ts)   AS hour,
                   f.project_id,
                   COALESCE(tu.model, '')      AS model,
                   tu.tool_name,
                   tu.error_kind,
                   COUNT(*)                    AS n
              FROM tool_uses tu
              JOIN files f    ON f.file_key = tu.file_key
             WHERE tu.ts IS NOT NULL AND tu.error_kind IS NOT NULL
               AND tu.is_canonical
             GROUP BY 1, 2, 3, 4, 5
            """
        )
        written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_tool_error_rollup: %d rows", written)
    return written


def rebuild_dispatch_rollup() -> int:
    """Rebuild `dispatch_rollup` from tool_uses + files.

    No join to records: a dispatch is counted from the CALL, and the
    dispatching assistant line's model is not what the subagent ran on.
    A dispatch that named no model stores '' rather than being dropped,
    so the row count still matches the number of dispatches.
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        c.execute("DELETE FROM dispatch_rollup")
        cur = c.execute(
            """
            INSERT INTO dispatch_rollup (
              hour, project_id, agent_type, agent_model, n
            )
            SELECT date_trunc('hour', tu.ts)      AS hour,
                   f.project_id,
                   tu.agent_type,
                   COALESCE(tu.agent_model, '')   AS agent_model,
                   COUNT(*)                       AS n
              FROM tool_uses tu
              JOIN files f ON f.file_key = tu.file_key
             WHERE tu.ts IS NOT NULL AND tu.agent_type IS NOT NULL
               AND tu.is_canonical
             GROUP BY 1, 2, 3, 4
            """
        )
        written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_dispatch_rollup: %d rows", written)
    return written


def rebuild_dispatch_brief_rollup() -> int:
    """Rebuild `dispatch_brief_rollup` from tool_uses + files.

    Same source as dispatch_rollup, different question: not what was
    dispatched but whether the call pointed at a written brief or
    carried its instructions inline. Only dispatches that actually
    carried a prompt have a shape to record, so the filter is on
    dispatch_brief_ref rather than agent_type -- a dispatch with a
    prompt but no subagent_type still has a briefing shape.
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        c.execute("DELETE FROM dispatch_brief_rollup")
        cur = c.execute(
            """
            INSERT INTO dispatch_brief_rollup (
              hour, project_id, agent_type, brief_ref, n, prompt_chars
            )
            SELECT date_trunc('hour', tu.ts)               AS hour,
                   f.project_id,
                   COALESCE(tu.agent_type, '')             AS agent_type,
                   tu.dispatch_brief_ref                   AS brief_ref,
                   COUNT(*)                                AS n,
                   COALESCE(SUM(tu.dispatch_prompt_chars), 0)
                                                           AS prompt_chars
              FROM tool_uses tu
              JOIN files f ON f.file_key = tu.file_key
             WHERE tu.ts IS NOT NULL AND tu.dispatch_brief_ref IS NOT NULL
               AND tu.is_canonical
             GROUP BY 1, 2, 3, 4
            """
        )
        written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_dispatch_brief_rollup: %d rows", written)
    return written


def rebuild_latency_rollup() -> int:
    """Rebuild `latency_rollup` for each display bucket width.

    Two passes per width — one grouped by project, one for the
    all-projects row (project_id = '') — because percentiles are not
    composable across a filter. Outlier dots are computed in the same
    pass and stored as JSONB.
    """
    written = 0
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '128MB'")
        # DELETE, not TRUNCATE: TRUNCATE takes an ACCESS EXCLUSIVE lock for
        # the whole rebuild transaction, so every concurrent read of this
        # table blocks until it commits — measured at 2.9s for a SELECT that
        # normally takes 0.07s. Since the rebuild runs on every ingest, that
        # stalled readers hourly. DELETE takes ROW EXCLUSIVE and MVCC keeps
        # serving the previous rows until commit, so readers never wait; at
        # a few thousand rows the extra cost is noise.
        c.execute("DELETE FROM latency_rollup")
        for bs in LATENCY_BUCKETS:
            for scope_expr, scope_join in (
                ("f.project_id", "JOIN files f ON f.file_key = r.file_key"),
                ("''", ""),
            ):
                cur = c.execute(
                    db.sql_text(f"""
                    WITH src AS (
                      SELECT to_timestamp(
                               floor(EXTRACT(EPOCH FROM r.ts) / {bs}) * {bs} + {bs} / 2
                             ) AS bucket,
                             {scope_expr} AS project_id,
                             COALESCE(NULLIF(r.model, ''), 'unknown') AS model,
                             r.ts, r.file_key, r.line_num,
                             r.reply_latency_s AS latency_s
                        FROM records r
                        {scope_join}
                       WHERE r.reply_latency_s IS NOT NULL AND r.is_canonical
                    ),
                    bands AS (
                      SELECT bucket, project_id, model, COUNT(*) AS n,
                             PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY latency_s) AS p10,
                             PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY latency_s) AS p50,
                             PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY latency_s) AS p90
                        FROM src GROUP BY 1, 2, 3
                    ),
                    ranked AS (
                      SELECT s.*,
                             b.n AS bucket_n,
                             ROW_NUMBER() OVER (PARTITION BY s.bucket, s.project_id, s.model
                                                ORDER BY s.latency_s DESC) AS rn_high,
                             ROW_NUMBER() OVER (PARTITION BY s.bucket, s.project_id, s.model
                                                ORDER BY s.latency_s ASC)  AS rn_low
                        FROM src s
                        JOIN bands b USING (bucket, project_id, model)
                       WHERE b.n >= 100
                    ),
                    picked AS (
                      SELECT bucket, project_id, model,
                             jsonb_agg(jsonb_build_object(
                               'ts', ts, 'latency_s', latency_s,
                               'file_key', file_key, 'line_num', line_num,
                               'kind', CASE WHEN rn_high <= GREATEST(1, CEIL(bucket_n * 0.01))
                                            THEN 'high' ELSE 'low' END
                             ) ORDER BY latency_s DESC) AS outliers
                        FROM ranked
                       WHERE rn_high <= GREATEST(1, CEIL(bucket_n * 0.01))
                          OR rn_low  <= GREATEST(1, CEIL(bucket_n * 0.01))
                       GROUP BY 1, 2, 3
                    )
                    INSERT INTO latency_rollup
                      (bucket_s, bucket, project_id, model, n, p10, p50, p90, outliers)
                    SELECT {bs}, b.bucket, b.project_id, b.model, b.n,
                           b.p10, b.p50, b.p90, COALESCE(p.outliers, '[]'::jsonb)
                      FROM bands b
                      LEFT JOIN picked p USING (bucket, project_id, model)
                    """),
                )
                written += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_latency_rollup: %d rows", written)
    return written


def rebuild_ctx_cost_rollup() -> int:
    """Rebuild `ctx_cost_rollup` from records + files.

    The per-call context window is fresh + cache_creation + cache_read.
    That is the BILLING sum, which equals the window for a single-
    iteration record and over-states it for the rare multi-iteration one
    (advisor fan-out / retries, where parse._usage_ctx_input takes the
    max-of-iterations instead). Those are not stored per record, and
    measured at 0 of 2192 usage records across a four-file sample, so
    the panel accepts the over-statement rather than carry another
    column and a full reparse for it.

    The bucket expression must stay in step with constants.ctx_bucket().
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        # DELETE, not TRUNCATE -- same reasoning as rebuild_tool_rollup:
        # TRUNCATE's ACCESS EXCLUSIVE lock stalls every concurrent reader
        # for the whole rebuild transaction.
        c.execute("DELETE FROM ctx_cost_rollup")
        cur = c.execute(
            """
            INSERT INTO ctx_cost_rollup (
              hour, project_id, model, ctx_bucket, requests,
              total_tokens, cost_usd
            )
            SELECT date_trunc('hour', r.ts)  AS hour,
                   f.project_id,
                   r.model,
                   CASE WHEN r.fresh_tokens + r.cache_creation_tokens
                             + r.cache_read_tokens >= %(mx)s THEN %(mx)s
                        WHEN r.fresh_tokens + r.cache_creation_tokens
                             + r.cache_read_tokens <= 0 THEN 0
                        ELSE ((r.fresh_tokens + r.cache_creation_tokens
                               + r.cache_read_tokens) / %(w)s) * %(w)s
                   END                       AS ctx_bucket,
                   COUNT(*)                  AS requests,
                   SUM(r.fresh_tokens + r.cache_creation_tokens
                       + r.cache_read_tokens + r.output_tokens)
                                             AS total_tokens,
                   SUM(r.cost_usd)           AS cost_usd
              FROM records r
              JOIN files f ON f.file_key = r.file_key
             WHERE r.is_canonical AND r.ts IS NOT NULL
             GROUP BY 1, 2, 3, 4
            """,
            {"mx": CTX_BUCKET_MAX, "w": CTX_BUCKET_WIDTH},
        )
        written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_ctx_cost_rollup: %d rows", written)
    return written


def rebuild_agent_rollup() -> int:
    """Rebuild `agent_rollup` from records + files.

    Grain is (hour, project, model, agent_type). `agent_type` lives on
    `files`, so this is a plain join -- no bucketing decision is baked in
    the way ctx_cost_rollup's edges are, and the stored columns are pure
    sums that compose across every dimension.

    The join is what `usage_rollup` cannot substitute for: its grain has
    already summed across every file in a (session, hour, model), and a
    session's main transcript shares its session_id with the subagent
    sidecars dispatched from it. Those are exactly the rows this panel
    needs kept apart.
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        # DELETE, not TRUNCATE -- same reasoning as rebuild_tool_rollup:
        # TRUNCATE's ACCESS EXCLUSIVE lock stalls every concurrent reader
        # for the whole rebuild transaction.
        c.execute("DELETE FROM agent_rollup")
        cur = c.execute(
            """
            INSERT INTO agent_rollup (
              hour, project_id, model, agent_type,
              requests, output_tokens, total_tokens, cost_usd
            )
            SELECT date_trunc('hour', r.ts) AS hour,
                   f.project_id,
                   r.model,
                   f.agent_type,
                   COUNT(*)                 AS requests,
                   SUM(r.output_tokens)     AS output_tokens,
                   SUM(r.fresh_tokens + r.cache_creation_tokens
                       + r.cache_read_tokens + r.output_tokens)
                                            AS total_tokens,
                   SUM(r.cost_usd)          AS cost_usd
              FROM records r
              JOIN files f ON f.file_key = r.file_key
             WHERE r.is_canonical AND r.ts IS NOT NULL
             GROUP BY 1, 2, 3, 4
            """
        )
        written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_agent_rollup: %d rows", written)
    return written
