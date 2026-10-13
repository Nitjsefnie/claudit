"""Typed latency-rollup reads, projected to the public outlier shape.

Outlier timestamps use PostgreSQL to_json in the reading session's TimeZone
instead of the rebuilding session's spelling served by the JSONB reader;
the spellings agree when rebuilds and reads share one TimeZone, as production's
single pool does.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from backend import db, r2
from backend.api_common import _iso


def latency_from_rollup(rng: str, project: str | None, model: str | None,
                        bucket_s: int, since: datetime) -> dict:
    """Percentiles cannot be summed across buckets, so unlike the other
    rollups this one is precomputed PER display bucket width — the
    widths are epoch-aligned and there are only a handful
    (constants.LATENCY_BUCKETS). A range filter then just selects
    buckets. project_id='' is the stored all-projects row: a project
    filter changes the population inside each (bucket, model) group,
    so it cannot be derived from the per-project rows."""
    roll_args: list[Any] = [bucket_s, project or "", since]
    roll_model = ""
    if model:
        roll_model = "AND dm.value LIKE %s"
        roll_args.append(f"%{model}%")
    with db.viz_conn() as c:
        rows = c.execute(
            db.sql_text(f"""
            SELECT bucket, dm.value AS model, n, p10, p50, p90,
                   ARRAY(SELECT to_json(t)#>>'{{}}' FROM unnest(l.out_ts) AS t),
                   l.out_latency_s,
                   ARRAY(SELECT f.file_key
                           FROM unnest(l.out_file_id) WITH ORDINALITY AS ids(id, pos)
                           LEFT JOIN files f ON f.file_id = ids.id ORDER BY pos),
                   l.out_line_num
            FROM latency_rollup l JOIN dim_model dm ON dm.id = l.model_id JOIN projects pj ON pj.proj_id = l.proj_id
            WHERE bucket_s = %s
              AND pj.project_id = %s
              AND bucket >= to_timestamp(
                    floor(EXTRACT(EPOCH FROM %s::timestamptz) / {bucket_s})
                    * {bucket_s} + {bucket_s} / 2.0)
              {roll_model}
            ORDER BY bucket, dm.value
            """),
            roll_args,
        ).fetchall()
    return {
        "range": rng,
        "project": project,
        "model": model,
        "bucket_s": bucket_s,
        "bands": [
            {
                "ts": _iso(b), "model": m, "n": int(n or 0),
                "p10": float(p10 or 0), "p50": float(p50 or 0), "p90": float(p90 or 0),
            }
            for (b, m, n, p10, p50, p90, *_o) in rows
        ],
        "outliers": [
            {
                "ts": ts, "model": m,
                "latency_s": float(latency or 0),
                "file_key": r2.public_key(file_key),
                "line": int(line or 0),
            }
            for (_b, m, _n, _a, _c2, _d, times, latencies, keys, lines) in rows
            for ts, latency, file_key, line in zip(
                times, latencies or [], keys, lines or [], strict=True)
        ],
    }
