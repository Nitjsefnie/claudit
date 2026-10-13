"""Reply-latency rollups, refreshed by whole affected display buckets."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from math import floor

from backend import db
from backend.constants import LATENCY_BUCKETS
from backend.ingest_scope import Scope, current_scope

log = logging.getLogger("claudit.ingest")


def _overlapping_bucket_starts(hour: datetime, width: int) -> list[datetime]:
    """Return epoch buckets intersecting the widened dirty-hour interval."""
    start = hour.astimezone(timezone.utc)
    end = start + timedelta(hours=2)
    bucket_s = floor(start.timestamp() / width) * width
    end_s = end.timestamp()
    buckets = []
    while bucket_s < end_s:
        buckets.append(datetime.fromtimestamp(bucket_s, timezone.utc))
        bucket_s += width
    return buckets


def _bucket_keys(scope: Scope, width: int
                 ) -> tuple[list[str], list[datetime], list[datetime]]:
    """Expand dirty local hours to every overlapping epoch bucket key."""
    project_buckets = {
        (project_id, bucket)
        for project_id, hour in scope.dirty_hours
        if hour is not None
        for bucket in _overlapping_bucket_starts(hour, width)
    }
    pairs = sorted(project_buckets)
    projects = [project_id for project_id, _ in pairs]
    buckets = [bucket for _, bucket in pairs]
    all_buckets = sorted({bucket for _, bucket in pairs})
    return projects, buckets, all_buckets


def rebuild_latency_rollup(scope: Scope | None = None) -> int:
    """Replace all latency buckets, or complete buckets touched by this run."""
    scope = scope if scope is not None else current_scope()
    incremental = scope is not None and not scope.full
    if scope is not None and not scope.full:
        if not any(hour is not None for _, hour in scope.dirty_hours):
            return 0
    written = 0
    with db.viz_conn() as conn:
        conn.execute("SET LOCAL work_mem = '128MB'")
        # The reserved all-projects row (project_id '', #959's R8) is
        # schema state, but a wholesale DELETE FROM projects takes it —
        # re-seed before any proj_id = 0 insert can violate the FK.
        # WHERE NOT EXISTS: when the row stands (every rebuild but the
        # one after such a wipe) the SELECT yields no row, so no insert
        # is ever attempted; ON CONFLICT stays for the concurrent race.
        conn.execute(
            "INSERT INTO projects (proj_id, project_id, display_name, "
            "first_seen_at, last_seen_at) "
            "SELECT 0, '', '', now(), now() "
            "WHERE NOT EXISTS (SELECT 1 FROM projects WHERE proj_id = 0) "
            "ON CONFLICT DO NOTHING")
        if not incremental:
            # DELETE keeps concurrent readers on the committed old rows; TRUNCATE
            # takes ACCESS EXCLUSIVE for the full percentile rebuild.
            conn.execute("DELETE FROM latency_rollup")
        for bucket_width in LATENCY_BUCKETS:
            if incremental:
                assert scope is not None
                projects, project_buckets, all_buckets = _bucket_keys(
                    scope, bucket_width)
                params = {
                    "projects": projects,
                    "project_buckets": project_buckets,
                    "buckets": all_buckets,
                }
                conn.execute(
                    db.sql_text(f"""
                    DELETE FROM latency_rollup l
                     USING (
                       SELECT p.proj_id, d.bucket_start
                         FROM unnest(%(projects)s::text[],
                                     %(project_buckets)s::timestamptz[])
                              AS d(project_id, bucket_start)
                         JOIN projects p ON p.project_id = d.project_id
                     ) d
                     WHERE l.bucket_s = {bucket_width}
                       AND l.proj_id = d.proj_id
                       AND l.bucket = d.bucket_start
                             + interval '{bucket_width // 2} seconds'
                    """), params)
                conn.execute(
                    db.sql_text(f"""
                    DELETE FROM latency_rollup l
                     USING (
                       SELECT DISTINCT bucket_start
                         FROM unnest(%(buckets)s::timestamptz[])
                              AS d(bucket_start)
                     ) d
                     WHERE l.bucket_s = {bucket_width} AND l.proj_id = 0
                       AND l.bucket = d.bucket_start
                             + interval '{bucket_width // 2} seconds'
                    """), {"buckets": all_buckets})

            for project_scope in (True, False):
                if not incremental:
                    project_expr = "p.proj_id" if project_scope else "0"
                    scope_join = (
                        "JOIN files f ON f.file_id = r.file_id "
                        "JOIN projects p ON p.project_id = f.project_id"
                        if project_scope else
                        "JOIN files f ON f.file_id = r.file_id"
                    )
                    scope_filter = ""
                    query_params = None
                elif project_scope:
                    project_expr = "p.proj_id"
                    scope_join = db.sql_text(f"""
                      JOIN files f ON f.file_id = r.file_id
                      JOIN projects p ON p.project_id = f.project_id
                      JOIN (
                        SELECT DISTINCT project_id, bucket_start
                          FROM unnest(%(projects)s::text[],
                                      %(project_buckets)s::timestamptz[])
                               AS d(project_id, bucket_start)
                      ) d ON f.project_id = d.project_id
                         AND r.ts >= d.bucket_start
                         AND r.ts < d.bucket_start
                               + interval '{bucket_width} seconds'
                    """)
                    scope_filter = ""
                    query_params = params
                else:
                    project_expr = "0"
                    scope_join = db.sql_text(f"""
                      JOIN files f ON f.file_id = r.file_id
                      JOIN (
                        SELECT DISTINCT bucket_start
                          FROM unnest(%(buckets)s::timestamptz[])
                               AS d(bucket_start)
                      ) d ON r.ts >= d.bucket_start
                         AND r.ts < d.bucket_start
                               + interval '{bucket_width} seconds'
                    """)
                    scope_filter = ""
                    query_params = {"buckets": all_buckets}
                cur = conn.execute(
                    db.sql_text(f"""
                    WITH src AS (
                      SELECT to_timestamp(
                               floor(EXTRACT(EPOCH FROM r.ts) / {bucket_width})
                               * {bucket_width} + {bucket_width} / 2
                             ) AS bucket,
                             {project_expr} AS proj_id,
                             r.model_id AS model_id,
                             r.ts, f.file_key, r.file_id, r.line_num,
                             r.reply_latency_s AS latency_s
                        FROM records r
                        {scope_join}
                       WHERE r.reply_latency_s IS NOT NULL AND r.is_canonical
                         {scope_filter}
                    ),
                    bands AS (
                      SELECT bucket, proj_id, model_id, COUNT(*) AS n,
                             PERCENTILE_CONT(0.10) WITHIN GROUP
                               (ORDER BY latency_s) AS p10,
                             PERCENTILE_CONT(0.50) WITHIN GROUP
                               (ORDER BY latency_s) AS p50,
                             PERCENTILE_CONT(0.90) WITHIN GROUP
                               (ORDER BY latency_s) AS p90
                        FROM src GROUP BY 1, 2, 3
                    ),
                    ranked AS (
                      SELECT s.*, b.n AS bucket_n,
                             ROW_NUMBER() OVER (
                               PARTITION BY s.bucket, s.proj_id, s.model_id
                               ORDER BY s.latency_s DESC, s.file_key, s.line_num
                             ) AS rn_high,
                             ROW_NUMBER() OVER (
                               PARTITION BY s.bucket, s.proj_id, s.model_id
                               ORDER BY s.latency_s ASC, s.file_key, s.line_num
                             ) AS rn_low
                        FROM src s JOIN bands b USING (bucket, proj_id, model_id)
                       WHERE b.n >= 100
                    ),
                    picked AS (
                      SELECT bucket, proj_id, model_id,
                             array_agg(ts ORDER BY latency_s DESC, file_key, line_num) AS out_ts,
                             array_agg(latency_s::double precision ORDER BY latency_s DESC, file_key, line_num) AS out_latency_s,
                             array_agg(file_id ORDER BY latency_s DESC, file_key, line_num) AS out_file_id,
                             array_agg(line_num ORDER BY latency_s DESC, file_key, line_num) AS out_line_num,
                             array_agg(rn_high <= GREATEST(1, CEIL(bucket_n * 0.01))
                                       ORDER BY latency_s DESC, file_key, line_num) AS out_high
                        FROM ranked
                       WHERE rn_high <= GREATEST(1, CEIL(bucket_n * 0.01))
                          OR rn_low <= GREATEST(1, CEIL(bucket_n * 0.01))
                       GROUP BY 1, 2, 3
                    )
                    INSERT INTO latency_rollup
                      (bucket_s, bucket, proj_id, model_id, n, p10, p50, p90,
                       out_ts, out_latency_s, out_file_id, out_line_num, out_high)
                    SELECT {bucket_width}, b.bucket, b.proj_id, b.model_id,
                           b.n, b.p10, b.p50, b.p90,
                           p.out_ts, p.out_latency_s, p.out_file_id, p.out_line_num, p.out_high
                      FROM bands b
                      LEFT JOIN picked p USING (bucket, proj_id, model_id)
                    """), query_params)
                written += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
    log.info("rebuild_latency_rollup: %d rows", written)
    return written
