"""Dimension/id helpers for tests that seed fact rows directly.
Resolve dimension values through one upsert per value, returning the id.
Import as `tests.dims_seed` only; imports draw nothing and hold no state."""
from __future__ import annotations

from psycopg.types.json import Jsonb  # noqa: F401  (re-exported for seeds)

__all__ = ["Jsonb"]


def dim_id(conn, table: str, value: str) -> int:
    """The dim row's id for `value`, creating it if needed."""
    row = conn.execute(
        f"INSERT INTO {table} (value) VALUES (%s) "  # noqa: S608
        "ON CONFLICT (value) DO UPDATE SET value = EXCLUDED.value "
        "RETURNING id",
        (value,),
    ).fetchone()
    assert row is not None, f"dim upsert returned no id: {table} {value!r}"
    return row[0]


def file_id(conn, file_key: str) -> int:
    """The `files.file_id` for a stored key."""
    row = conn.execute(
        "SELECT file_id FROM files WHERE file_key = %s", (file_key,)
    ).fetchone()
    assert row is not None, f"no files row for {file_key!r}"
    return row[0]


def proj_id(conn, project_id: str) -> int:
    """The `projects.proj_id` for a stored project id."""
    row = conn.execute(
        "SELECT proj_id FROM projects WHERE project_id = %s", (project_id,)
    ).fetchone()
    assert row is not None, f"no projects row for {project_id!r}"
    return row[0]


def ses_id(conn, project_id: str, session_id: str) -> int:
    """The `sessions.ses_id` for (project, session), creating it if needed."""
    row = conn.execute(
        "INSERT INTO sessions (proj_id, session_id) "
        "SELECT p.proj_id, %s FROM projects p WHERE p.project_id = %s "
        "ON CONFLICT (proj_id, session_id) DO UPDATE "
        "SET session_id = EXCLUDED.session_id "
        "RETURNING ses_id",
        (session_id, project_id),
    ).fetchone()
    assert row is not None, f"no projects row for {project_id!r}"
    return row[0]


def seed_project(conn, project_id: str) -> int:
    """Ensure a `projects` row exists (rollup FKs need it); its proj_id."""
    conn.execute(
        "INSERT INTO projects (project_id, display_name, "
        "first_seen_at, last_seen_at) VALUES (%s, %s, now(), now()) "
        "ON CONFLICT (project_id) DO NOTHING",
        (project_id, project_id),
    )
    return proj_id(conn, project_id)


def seed_usage_rollup(conn, project_id: str, session_id: str, hour, *,
                      model: str = "m", provider: str = "",
                      requests: int = 1, fresh_tokens: int = 0,
                      cost_usd: float = 0) -> None:
    """One usage_rollup row with its project/session/dim parents seeded."""
    pid = seed_project(conn, project_id)
    sid = ses_id(conn, project_id, session_id)
    mid = dim_id(conn, "dim_model", model)
    provid = dim_id(conn, "dim_provider", provider)
    conn.execute(
        "INSERT INTO usage_rollup (ses_id, proj_id, hour, model_id, "
        "provider_id, is_main, first_ts, last_ts, requests, fresh_tokens, "
        "output_tokens, cost_usd) VALUES (%s, %s, %s, %s, %s, TRUE, %s, "
        "%s, %s, %s, 0, %s)",
        (sid, pid, hour, mid, provid, hour, hour, requests,
         fresh_tokens, cost_usd),
    )


def seed_latency_rollup(conn, project_id: str, bucket_s: int, bucket, *,
                        model: str, n: int, outliers=None,
                        p10=None, p50=None, p90=None) -> None:
    """One latency_rollup row with its project/model parents seeded.
    project_id '' is the reserved all-projects grain (R8)."""
    if project_id:
        pid = seed_project(conn, project_id)
    else:
        pid = 0
    mid = dim_id(conn, "dim_model", model)
    dots = outliers or []
    conn.execute(
        "INSERT INTO latency_rollup (bucket_s, bucket, proj_id, model_id, "
        "n, p10, p50, p90, out_ts, out_latency_s, out_file_id, out_line_num, out_high) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (bucket_s, bucket, pid, mid, n, p10, p50, p90,
         [o["ts"] for o in dots] or None,
         [o["latency_s"] for o in dots] or None,
         [file_id(conn, o["file_key"]) for o in dots] or None,
         [o["line_num"] for o in dots] or None,
         [o.get("kind") == "high" for o in dots] or None),
    )
