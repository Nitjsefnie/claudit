"""Atomic replay attribution (SV-CANONICAL-FLAG)."""
from __future__ import annotations

import logging
from collections.abc import Callable

from backend import constants, db, flags, ingest_reprice, ingest_vacuum, uuid_split
from backend.ingest_abort import IngestAborted
from backend.ingest_rate_state import (
    RateRow, SELECT_ADOPTED_RATE_STATE, reprice_adopted, stored_pricing_version_is_newer,
)
from backend.ingest_scope import Scope, current_scope

log = logging.getLogger("claudit.ingest")
_UUID_TEXT = uuid_split.uuid_text_sql("r")
_WINNER_UUID_TEXT = uuid_split.uuid_text_sql("w")
_UUID_PRESENT = uuid_split.uuid_present_sql("r")


def adopt_original_models(
        scope: Scope | None = None,
        should_stop: Callable[[], bool | None] | None = None) -> int:
    """Adopt originals' models and reprice losing replays in bounded transactions.

    Newer-priced records defer adoption to preserve model and price together.
    Non-canonical writes need no rollup hours; extend collected files.models.
    """
    scope = scope if scope is not None else current_scope()
    incremental_scope = (
        scope if scope is not None and not scope.full else None)
    incremental = incremental_scope is not None
    if incremental and not (incremental_scope.affected_uuids
                            or incremental_scope.affected_tool_use_ids):
        return 0
    changed = 0
    uuids = sorted(incremental_scope.affected_uuids) if incremental else None
    with db.viz_conn() as conn:
        after_key: tuple[int, int] = (-1, 0)
        staged = False
        try:
            while True:
                if should_stop is not None and should_stop():
                    raise IngestAborted("shutdown requested")
                if not staged:
                    conn.execute("SET LOCAL work_mem = '64MB'")
                    newer_ids = [key for key, value in conn.execute(
                        "SELECT id, value FROM dim_pricing_version").fetchall()
                        if stored_pricing_version_is_newer(value, constants.PRICING_VERSION)]
                    # Stage the identity join once, without an ordered LIMIT's row-goal plan.
                    # The temp relation stays on this checked-out connection across commits.
                    conn.execute(
                        f"""
                        CREATE TEMP TABLE model_adoption_candidates ON COMMIT PRESERVE ROWS AS
                        WITH winners AS (
                          SELECT r.uuid_col, r.seq, r.fallback, r.model_id
                            FROM records r
                           WHERE r.is_canonical AND r.is_replay IS NOT TRUE
                             AND r.model_id IS NOT NULL
                             AND {_UUID_PRESENT}
                        ), adopt AS (
                          SELECT r.file_id, r.line_num,
                                 w.model_id AS winner_model_id
                            FROM records r JOIN winners w
                              ON {_WINNER_UUID_TEXT} = {_UUID_TEXT}
                            LEFT JOIN records_price rp
                              ON rp.file_id = r.file_id AND rp.line_num = r.line_num
                           WHERE r.is_replay IS TRUE AND r.is_canonical IS FALSE
                             AND r.model_id IS DISTINCT FROM w.model_id
                             AND (rp.pricing_version_id IS NULL
                                  OR rp.pricing_version_id <> ALL(%(newer_ids)s::smallint[]))
                             AND (%(uuids)s::text[] IS NULL
                                  OR {_UUID_TEXT} = ANY(%(uuids)s))
                        ) SELECT * FROM adopt
                        """, {"newer_ids": newer_ids, "uuids": uuids})
                    conn.execute("CREATE UNIQUE INDEX ON model_adoption_candidates (file_id, line_num)")
                    conn.execute("ANALYZE model_adoption_candidates")
                    conn.commit()
                    staged = True
                conn.execute("SET LOCAL work_mem = '64MB'")
                cur = conn.execute(
                    """
                    WITH adopt AS (
                      SELECT * FROM model_adoption_candidates
                       WHERE (file_id, line_num) > (%(after_file)s, %(after_line)s)
                       ORDER BY file_id, line_num LIMIT %(batch_size)s
                    ), upd AS (
                      UPDATE records r SET model_id = a.winner_model_id
                        FROM adopt a
                       WHERE r.file_id = a.file_id AND r.line_num = a.line_num
                      RETURNING r.file_id, r.line_num, r.model_id, r.fresh_tokens,
                                r.cache_creation_tokens, r.cache_read_tokens,
                                r.output_tokens, r.eph5_tokens, r.eph1h_tokens,
                                r.ts, r.provider_id, r.web_search_requests
                    ), added AS (
                      SELECT a.file_id, array_agg(dm.value) AS added
                        FROM (SELECT DISTINCT file_id, winner_model_id FROM adopt) a
                        JOIN dim_model dm ON dm.id = a.winner_model_id
                       GROUP BY a.file_id
                    ), files_upd AS (
                      UPDATE files f
                         SET models = (SELECT array(SELECT DISTINCT m
                                                      FROM unnest(f.models || added.added)
                                                     AS m ORDER BY m))
                        FROM added
                       WHERE f.file_id = added.file_id AND %(file_extras)s
                         AND NOT ('file_extras' = ANY(
                               COALESCE(f.collection_flags, '{}'::text[])))
                    )
                    """ + SELECT_ADOPTED_RATE_STATE, {
                        "after_file": after_key[0], "after_line": after_key[1],
                        "batch_size": ingest_reprice.REPRICE_BATCH,
                        "file_extras": flags.CONFIG.collection.enabled("file_extras"),
                    })
                adopted_rows = [RateRow(*raw) for raw in cur.fetchall()]
                adopted_keys = [(row.file_id, row.line_num) for row in adopted_rows]
                if not adopted_keys:
                    break
                # A NULL cost denotes a missing sidecar: stored costs are NOT NULL.
                repriced = reprice_adopted(conn, adopted_keys, rows=[
                    row for row in adopted_rows if row.cost_usd is not None])
                conn.commit()
                changed += len(adopted_keys)
                after_key = max(adopted_keys)
                ingest_vacuum.note_phase("reprice", repriced)
        finally:
            # Roll back a failing batch before removing session-local staging.
            conn.rollback()
            if staged:
                conn.execute("DROP TABLE model_adoption_candidates")
                conn.commit()

    with db.viz_conn() as conn:
        conn.execute("SET LOCAL work_mem = '64MB'")
        tool_params = {
            "tool_ids": (sorted(incremental_scope.affected_tool_use_ids)
                         if incremental else None),
        }
        cur = conn.execute(
            """
            WITH winners AS (
              SELECT t.tool_use_id, t.model_id
                FROM tool_uses t
               WHERE t.is_canonical AND t.is_replay IS NOT TRUE
                 AND t.tool_use_id IS NOT NULL AND t.model_id IS NOT NULL
            ), adopt AS (
              SELECT t.file_id, t.line_num, t.idx,
                     w.model_id AS winner_model_id
                FROM tool_uses t JOIN winners w ON w.tool_use_id = t.tool_use_id
               WHERE t.is_replay IS TRUE AND t.is_canonical IS FALSE
                 AND t.model_id IS DISTINCT FROM w.model_id
                 AND (%(tool_ids)s::text[] IS NULL
                      OR t.tool_use_id = ANY(%(tool_ids)s))
            )
            UPDATE tool_uses t SET model_id = a.winner_model_id
              FROM adopt a
             WHERE t.file_id = a.file_id AND t.line_num = a.line_num
               AND t.idx = a.idx
            """, tool_params)
        changed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
    if changed:
        log.info("adopt_original_models: %d rows adopted", changed)
    return changed
