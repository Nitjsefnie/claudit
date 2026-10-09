"""GET /api/cache — per-model and per-session prompt-cache totals.

Split out of api.py (issue #8 module split). The endpoint body is broken
into _cache_canon_source (shared WHERE fragment), _cache_queries (the
four queries) and _session_total (the cross-model fold) so no single
function trips the locals gate. Behaviour is unchanged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, NamedTuple

from fastapi import APIRouter, Query, Request

from backend import db, r2
from backend.api_common import (Phases, _parse_range, fold_per_model,
                                fold_per_model_provider, rate_epoch_sql)
from backend.cache import cache_response
from backend.rate_boundaries import rate_boundaries

router = APIRouter()


class _RatePairInputs(NamedTuple):
    models: list[str]
    providers: list[str]
    boundary_lists: list[str]
    pair_bounds: dict[tuple[str, str], list[datetime]]


class _CacheQueryResults(NamedTuple):
    per_model_rows: list
    top_output: list
    top_create: list
    top_read: list
    pair_bounds: dict[tuple[str, str], list[datetime]]


def _cache_canon_source(project: str | None, model: str | None,
                        since: datetime) -> tuple[str, list]:
    """The shared FROM/WHERE fragment for the four cache queries.

    Dedup used to be a CTE prefixed onto each query, so Postgres re-ran
    the whole DISTINCT ON — the single most expensive step — FOUR times
    per request (measured: 19s at range=all). It is now resolved at
    ingest into records.is_canonical (ingest.recompute_canonical), so
    each query just filters a boolean.
    """
    proj_filter = ""
    model_filter = ""
    canon_args: list[Any] = [since]
    if project:
        # Semi-join rather than a JOIN: the top-N queries below select a
        # bare `file_key`, which both tables have — joining `files` in
        # would make that reference ambiguous.
        proj_filter = (
            "AND file_key IN (SELECT file_key FROM files WHERE project_id = %s)"
        )
        canon_args.append(project)
    if model:
        model_filter = "AND model LIKE %s"
        canon_args.append(f"%{model}%")
    canon_src = f"""
        FROM records
        WHERE (ts >= %s OR ts IS NULL) AND is_canonical {proj_filter} {model_filter}
    """
    return canon_src, canon_args


def _rate_pair_inputs(c, ph: Phases) -> _RatePairInputs:
    """Read the rollup's distinct pairs and encode their own boundaries."""
    pairs = ph.execute(
        "rate_pairs", c,
        "SELECT DISTINCT model, provider FROM usage_rollup",
    ).fetchall()
    # NULL is not masked as `unknown` (issue #653): ingest refuses a file
    # whose rows name no model, so a NULL here would mean that refusal
    # was bypassed; resolve() prices an empty id at the fallback rates.
    pairs = [(model, provider or "") for model, provider in pairs]
    pair_bounds = {
        pair: rate_boundaries(*pair) for pair in pairs
    }
    pair_models = [model for model, _ in pairs]
    pair_providers = [provider for _, provider in pairs]
    boundary_lists = [
        ",".join(boundary.isoformat() for boundary in pair_bounds[pair])
        for pair in pairs
    ]
    return _RatePairInputs(pair_models, pair_providers, boundary_lists,
                           pair_bounds)


def _cache_queries(c, ph: Phases, canon_src: str, canon_args: list) \
        -> _CacheQueryResults:
    """Read pair boundaries, then run the per-model and top-10 queries."""
    pair_inputs = _rate_pair_inputs(c, ph)

    epoch_expr, epoch_params = rate_epoch_sql("ts")
    per_model_source = canon_src.replace(
        "FROM records",
        "FROM records LEFT JOIN rate_boundaries AS rate_bounds "
        "ON rate_bounds.pair_model = records.model "
        "AND rate_bounds.pair_provider = COALESCE(records.provider, '')",
        1,
    )
    per_model_rows = ph.execute(
        "per_model", c, f"""
        WITH rate_boundaries AS (
            SELECT supplied.model AS pair_model,
                   supplied.provider AS pair_provider,
                   CASE WHEN supplied.boundary_list = ''
                        THEN '{{}}'::timestamptz[]
                        ELSE string_to_array(supplied.boundary_list, ',')
                             ::timestamptz[]
                   END AS boundaries
            FROM unnest(%s::text[], %s::text[], %s::text[])
                 AS supplied(model, provider, boundary_list)
        )
        SELECT model,
               provider,
               ({epoch_expr})              AS rate_epoch,
               COALESCE(long_context, FALSE) AS long_context,
               COUNT(*)                    AS turns,
               SUM(fresh_tokens)           AS fresh,
               SUM(cache_creation_tokens)  AS cache_create,
               SUM(cache_read_tokens)      AS cache_read,
               SUM(output_tokens)          AS output,
               SUM(eph5_tokens)            AS eph5,
               SUM(eph1h_tokens)           AS eph1h,
               SUM(cost_usd)               AS cost_total,
               long_context_input_mult,
               long_context_output_mult
        {per_model_source}
        GROUP BY model, provider, rate_epoch, COALESCE(long_context, FALSE),
                 long_context_input_mult, long_context_output_mult
        ORDER BY cost_total DESC
        """,
        [pair_inputs.models, pair_inputs.providers,
         pair_inputs.boundary_lists] + epoch_params + canon_args,
    ).fetchall()

    top_output = ph.execute(
        "top_output", c, f"""
        SELECT ts, line_num, request_id, model,
               output_tokens, cache_read_tokens,
               eph1h_tokens, eph5_tokens, fresh_tokens,
               cost_usd, file_key
        {canon_src}
        ORDER BY output_tokens DESC
        LIMIT 10
        """,
        canon_args,
    ).fetchall()

    top_create = ph.execute(
        "top_create", c, f"""
        SELECT ts, line_num, request_id, model,
               cache_creation_tokens, eph1h_tokens, eph5_tokens,
               cache_read_tokens, output_tokens, fresh_tokens,
               cost_usd, file_key
        {canon_src}
          AND cache_creation_tokens > 0
        ORDER BY cache_creation_tokens DESC
        LIMIT 10
        """,
        canon_args,
    ).fetchall()

    top_read = ph.execute(
        "top_read", c, f"""
        SELECT ts, line_num, request_id, model,
               cache_read_tokens, eph1h_tokens, eph5_tokens,
               output_tokens, fresh_tokens,
               cost_usd, file_key
        {canon_src}
          AND cache_read_tokens > 0
        ORDER BY cache_read_tokens DESC
        LIMIT 10
        """,
        canon_args,
    ).fetchall()

    return _CacheQueryResults(
        per_model_rows, top_output, top_create, top_read,
        pair_inputs.pair_bounds)


def _session_total(per_model: list) -> dict:
    """The per_model shape, summed across models."""
    session_total = {
        "turns": sum(m["turns"] for m in per_model),
        "fresh": sum(m["fresh"] for m in per_model),
        "cache_create": sum(m["cache_create"] for m in per_model),
        "cache_read": sum(m["cache_read"] for m in per_model),
        "output": sum(m["output"] for m in per_model),
        "eph5": sum(m["eph5"] for m in per_model),
        "eph1h": sum(m["eph1h"] for m in per_model),
        "cost_total": round(sum(m["cost_total"] for m in per_model), 4),
        "cost_buckets": {
            k: round(sum(m["cost_buckets"][k] for m in per_model), 4)
            for k in ("fresh", "create_5m", "create_1h", "read", "output")
        },
        "estimated_rate": any(m["estimated_rate"] for m in per_model),
    }
    total_in = (
        session_total["fresh"]
        + session_total["cache_create"]
        + session_total["cache_read"]
    )
    session_total["hit_rate_pct"] = round(
        (session_total["cache_read"] / total_in * 100.0) if total_in else 0.0, 1
    )
    return session_total


def _top_rows(rows, columns):
    out = []
    for row in rows:
        d = {}
        for col, v in zip(columns, row):
            if hasattr(v, "isoformat"):
                d[col] = v.isoformat()
            elif col == "cost":
                d[col] = float(v) if v is not None else 0.0
            elif col == "file_key":
                # Public form: the bucket segment never leaves the server
                # (SV-FILES-RECORDS).
                d[col] = r2.public_key(v)
            elif col in ("ts", "request_id", "model"):
                d[col] = v
            else:
                d[col] = int(v or 0)
        out.append(d)
    return out


@cache_response
def cache_view(
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Prompt-cache totals per model and per session, with the top requests.

    Returns:
      {
        range, project,
        per_model: [{model, turns, fresh, cache_create, cache_read, output,
                     eph5, eph1h, hit_rate_pct, cost_total, cost_buckets}],
        per_model_provider: [{same shape, plus provider}] -- one entry per
                     (model, provider); provider is null for a record
                     that named no serving host,
        session_total: {same shape, summed across per_model},
        top_output: [{ts, line, request_id, model, output, c_read,
                      c_create_1h, c_create_5m, fresh, cost, file_key}],
        top_cache_create: [...],
        top_cache_read:   [...]
      }

    Cross-file uuid dedup is resolved at ingest into records.is_canonical
    (ingest.recompute_canonical); each query just filters a boolean.
    Records with NULL uuid (legacy) are kept verbatim and always
    canonical.
    """
    delta = _parse_range(rng)
    since = datetime.now(timezone.utc) - delta
    canon_src, canon_args = _cache_canon_source(project, model, since)

    ph = Phases("cache_view")
    with db.viz_conn() as c:
        query_results = _cache_queries(c, ph, canon_src, canon_args)

    per_model = fold_per_model(
        query_results.per_model_rows, pair_bounds=query_results.pair_bounds)
    per_model_provider = fold_per_model_provider(
        query_results.per_model_rows, pair_bounds=query_results.pair_bounds)
    ph.done(models=len(per_model))

    return {
        "range": rng,
        "project": project,
        "per_model": per_model,
        "per_model_provider": per_model_provider,
        "session_total": _session_total(per_model),
        "top_output": _top_rows(query_results.top_output, [
            "ts", "line", "request_id", "model",
            "output", "c_read", "c_create_1h", "c_create_5m", "fresh",
            "cost", "file_key",
        ]),
        "top_cache_create": _top_rows(query_results.top_create, [
            "ts", "line", "request_id", "model",
            "c_create", "c_create_1h", "c_create_5m", "c_read",
            "output", "fresh", "cost", "file_key",
        ]),
        "top_cache_read": _top_rows(query_results.top_read, [
            "ts", "line", "request_id", "model",
            "c_read", "c_create_1h", "c_create_5m",
            "output", "fresh", "cost", "file_key",
        ]),
    }


@router.get("/cache")
def cache_view_route(
    request: Request,
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Route wrapper around the cached cache payload.

    The cached body always carries file_key on every top-request row,
    shared by all callers. Guests get a copied payload with file_key
    removed from those rows; the dict comprehensions preserve the cached
    object intact for later non-guest hits (issue #244)."""
    payload = cache_view(rng=rng, project=project, model=model)
    if bool(getattr(request.state, "is_guest", False)):
        row_lists = ("top_output", "top_cache_create", "top_cache_read")
        payload = {
            **payload,
            **{
                name: [{k: v for k, v in row.items() if k != "file_key"}
                       for row in payload[name]]
                for name in row_lists
            },
        }
    return payload
