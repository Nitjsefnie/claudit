"""/api/sessions* endpoints: paginated list, detail, transcript, sidecar."""
from __future__ import annotations

import lzma
from datetime import datetime
from typing import Any

from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, Query
from starlette.responses import Response

from backend import cache, db, r2
from backend.api_common import _iso

router = APIRouter()


@router.get("/sessions/{session_id}/transcript")
def get_transcript(session_id: str) -> Response:
    """Stream raw jsonl from R2 via 20-min idle LRU. The MAIN file of the
    session is what's returned (the agent peers are visible only via the
    Inspector's per-file dropdown, future work)."""
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT file_key, r2_etag FROM files "
            "WHERE session_id = %s AND is_main = TRUE LIMIT 1",
            (session_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "session not found")
    file_key, etag = row
    # Keyed by the object, not the validator alone: a
    # file:// mirror derives the etag from size+mtime, so two
    # transcripts of equal size and mtime share one etag.
    key = cache.transcript_key(file_key, etag)
    body = cache.transcript_cache.get(key)
    if body is None:
        body = r2.get_object(file_key)
        cache.transcript_cache.put(key, body)
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={"ETag": etag, "Cache-Control": "no-cache"},
    )


@router.get("/sessions/{session_id}/sidecar")
def get_sidecar(
    session_id: str,
    path: str = Query(..., min_length=1),
) -> Response:
    """Path-validated sidecar fetch from R2 under the session's prefix."""
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT file_key FROM files "
            "WHERE session_id = %s AND is_main = TRUE LIMIT 1",
            (session_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "session not found")
    file_key = row[0]
    session_prefix = file_key.rsplit("/", 1)[0] + "/"
    components = path.split("/")
    if (path.startswith("/") or ".." in components or "." in components
            or "\x00" in path):
        # NUL blows up open() with an embedded-null ValueError and a `.`
        # component makes the key a directory (file mode) or a key no S3
        # object can have — neither reaches the fetch below.
        raise HTTPException(400, f"bad path: {path!r}")
    full_key = session_prefix + path
    # Data files may be stored xz-compressed (`<name>.xz`); try the plain key
    # first, then the compressed one. r2.get_object inflates `.xz` transparently,
    # so the response body is the original bytes either way and the media type
    # still keys off the un-suffixed `path`.
    body = None
    for candidate in (full_key, full_key + ".xz"):
        try:
            body = r2.get_object(candidate)
            break
        except (FileNotFoundError, ClientError, lzma.LZMAError):
            # A candidate that is simply NOT THERE (file-mode ENOENT, S3
            # NoSuchKey) or unreadable-as-stored (corrupt .xz) falls
            # through to the next candidate. Anything else — the
            # unconfigured-bucket ValueError above all — is a real error
            # and must surface, not masquerade as a missing sidecar.
            continue
    if body is None:
        raise HTTPException(404, "sidecar not found")
    media = "text/plain"
    if path.endswith(".jsonl"):
        media = "application/x-ndjson"
    elif path.endswith(".json"):
        media = "application/json"
    return Response(content=body, media_type=media)


def _models_dict(models_raw) -> dict:
    models = {}
    if models_raw:
        for entry in models_raw:
            try:
                models[entry["model"]] = int(entry["count"])
            except (KeyError, TypeError, ValueError):
                continue
    return models


def _aggregate_session_row(row) -> dict:
    (
        session_id, project_id, first_at, last_at, dur_s, req_count,
        input_t, output_t, c5, c1h, cr, cost, models_raw,
    ) = row
    return {
        "session_id": session_id,
        "project_id": project_id,
        "first_event_at": _iso(first_at),
        "last_event_at": _iso(last_at),
        "duration_s": int(dur_s or 0),
        "request_count": int(req_count or 0),
        "input_tokens": int(input_t or 0),
        "output_tokens": int(output_t or 0),
        "cache_create_5m_tokens": int(c5 or 0),
        "cache_create_1h_tokens": int(c1h or 0),
        "cache_read_tokens": int(cr or 0),
        "cost_usd": float(cost or 0),
        "models": _models_dict(models_raw),
        "limit_hits": 0,
    }


@router.get("/sessions")
def list_sessions(
    project: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = Query(None),
) -> dict:
    """Paginated main-file sessions with records-table aggregates.

    Cursor is descending first_event_at ISO timestamp; use next_cursor
    for the next page. models comes from a sub-aggregation. limit_hits is
    0 because rate-limit hits are not tracked per session."""
    proj_filter = ""
    args: list[Any] = []
    if project:
        proj_filter = "AND f.project_id = %s"
        args.append(project)

    cursor_clause = ""
    if cursor:
        try:
            cursor_dt = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, f"bad cursor: {cursor!r}") from None
        cursor_clause = "WHERE first_event_at < %s"
        cursor_arg: list[Any] = [cursor_dt]
    else:
        cursor_arg = []

    # Aggregate main and subagent files by their shared session_id, so
    # subagent usage rolls into the parent. Ingest resolves cross-file uuid
    # dedup into is_canonical (SV-CANONICAL-FLAG); NULL uuids are always
    # canonical, so this flag alone avoids a per-request table sort.
    sql = f"""
    WITH per_session AS (
      SELECT f.session_id,
             min(f.project_id) AS project_id,
             min(r.ts) AS first_event_at,
             max(r.ts) AS last_event_at,
             EXTRACT(EPOCH FROM (max(r.ts) - min(r.ts)))::bigint AS duration_s,
             COUNT(*) AS request_count,
             SUM(r.fresh_tokens)         AS input_tokens,
             SUM(r.output_tokens)        AS output_tokens,
             SUM(r.eph5_tokens)          AS cache_create_5m_tokens,
             SUM(r.eph1h_tokens)         AS cache_create_1h_tokens,
             SUM(r.cache_read_tokens)    AS cache_read_tokens,
             SUM(rp.cost_usd)            AS cost_usd,
             (SELECT json_agg(json_build_object('model', model, 'count', c))
              FROM (
                SELECT dm2.value AS model, COUNT(*) AS c
                FROM records r2
                JOIN files f2 ON f2.file_id = r2.file_id
                JOIN dim_model dm2 ON dm2.id = r2.model_id
                WHERE f2.session_id = f.session_id AND dm2.value <> ''
                  AND r2.is_canonical
                GROUP BY dm2.value
              ) sub) AS models_raw
      FROM records r
      JOIN records_price rp ON rp.file_id = r.file_id AND rp.line_num = r.line_num
      JOIN files f ON f.file_id = r.file_id
      WHERE r.is_canonical {proj_filter}
      GROUP BY f.session_id
    )
    SELECT * FROM per_session
    {cursor_clause}
    ORDER BY first_event_at DESC NULLS LAST
    LIMIT %s
    """

    with db.viz_conn() as c:
        rows = c.execute(db.sql_text(sql), args + cursor_arg + [limit + 1]).fetchall()

    items = [_aggregate_session_row(r) for r in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        # The cursor is the first_event_at of the NEXT page's first row,
        # which is the last item in `items` (we paged DESC).
        last_first = items[-1]["first_event_at"]
        next_cursor = last_first
    return {"items": items, "next_cursor": next_cursor}


def _ctx_trace(timestamps: list[datetime | None], inputs: list[int]) -> list:
    return [{"t": int(ts.timestamp()), "ctx": int(value)}
            for ts, value in zip(timestamps, inputs, strict=True) if ts is not None]


@router.get("/sessions/{session_id}")
def session_detail(session_id: str) -> dict:
    """Single-session totals, context timeline and burn rate.

    ctx_trace reshapes the typed file timeline to [{t: epoch_seconds,
    ctx: int}] for the chart. burn is {tps, model} from records; r2_key is
    the main file_key. limit_hits is 0: no per-session hit count is stored."""
    with db.viz_conn() as c:
        row = c.execute(
            """
            WITH per_session AS (
              SELECT f.session_id,
                     f.project_id,
                     f.file_key,
                     f.ctx_ts, f.ctx_input,
                     min(r.ts) AS first_event_at,
                     max(r.ts) AS last_event_at,
                     EXTRACT(EPOCH FROM (max(r.ts) - min(r.ts)))::bigint AS duration_s,
                     COUNT(r.file_id) AS request_count,
                     SUM(r.fresh_tokens)         AS input_tokens,
                     SUM(r.output_tokens)        AS output_tokens,
                     SUM(r.eph5_tokens)          AS cache_create_5m_tokens,
                     SUM(r.eph1h_tokens)         AS cache_create_1h_tokens,
                     SUM(r.cache_read_tokens)    AS cache_read_tokens,
                     SUM(rp.cost_usd)            AS cost_usd,
                     (SELECT json_agg(json_build_object('model', model, 'count', c))
                      FROM (
                        SELECT dm2.value AS model, COUNT(*) AS c
                        FROM records r2
                        JOIN dim_model dm2 ON dm2.id = r2.model_id
                        WHERE r2.file_id = f.file_id AND dm2.value <> ''
                          AND r2.is_canonical
                        GROUP BY dm2.value
                      ) sub) AS models_raw,
                     (SELECT dm3.value FROM records r3
                      JOIN dim_model dm3 ON dm3.id = r3.model_id
                      WHERE r3.file_id = f.file_id AND dm3.value <> ''
                        AND r3.is_canonical
                      GROUP BY dm3.value ORDER BY count(*) DESC LIMIT 1
                     ) AS dom_model
              FROM files f
              LEFT JOIN records r
                ON r.file_id = f.file_id AND r.is_canonical
              LEFT JOIN records_price rp
                ON rp.file_id = r.file_id AND rp.line_num = r.line_num
              WHERE f.session_id = %s AND f.is_main = TRUE
              GROUP BY f.session_id, f.project_id, f.file_key, f.file_id
              LIMIT 1
            )
            SELECT * FROM per_session
            """,
            (session_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(404, "session not found")

    base = _aggregate_session_row((row[0], row[1], *row[5:16]))

    write_tokens = (
        base["input_tokens"]
        + base["cache_create_5m_tokens"]
        + base["cache_create_1h_tokens"]
    )
    span_s = max(base["duration_s"], 1)
    burn = {
        "tps": float(write_tokens) / span_s,
        "model": row[16] or "",
        "hit_5h_limit": False,
    }

    return {
        **base,
        # Public form: the bucket segment never leaves the server
        # (SV-FILES-RECORDS).
        "r2_key": r2.public_key(row[2]),
        "ctx_trace": _ctx_trace(row[3], row[4]),
        "burn": burn,
    }
