"""Replay model adoption and its atomic rate-state transition."""
from __future__ import annotations

import logging

from backend import constants, db, flags, ingest_vacuum, uuid_split
from backend.ingest_rate_state import reprice_adopted, stored_pricing_version_is_newer
from backend.ingest_scope import Scope, current_scope

log = logging.getLogger("claudit.ingest")
_UUID_TEXT = uuid_split.uuid_text_sql("r")
_WINNER_UUID_TEXT = uuid_split.uuid_text_sql("w")
_UUID_PRESENT = uuid_split.uuid_present_sql("r")


def adopt_original_models(scope: Scope | None = None) -> int:
    """A replayed copy adopts the model the parent had in force.

    A forked rollout stores its replayed copies under the fork's FIRST
    declared model — the model at the fork point, which a
    mid-history switch makes wrong for the earlier requests. The parent
    is a different file, so no parse can resolve this; the canonical
    winner of the uuid can, because while the parent's copy is present it
    IS the winner: an original beats a replay whatever the key order
    (SV-CANONICAL-FLAG). Every losing replayed copy therefore adopts its
    winner's model, records by uuid and tool calls by tool_use_id; with
    no original present (the replay is itself canonical, or every copy is
    a replay) the first-declared-model fallback stands. A NULL winner model is not
    adopted — the copy keeps its attribution rather than losing it.

    Runs at recompute_canonical's tail, on the flags it has just
    settled. Adopted rows are non-canonical, so no rollup
    reads them and the pass adds no hours to the scope; it extends
    collected files.models with the adopted models, since the fork file journals
    its parent's requests. Model and rate state move in one transaction;
    rows priced by a newer binary defer adoption to preserve both together.
    """
    scope = scope if scope is not None else current_scope()
    incremental_scope = (
        scope if scope is not None and not scope.full else None)
    incremental = incremental_scope is not None
    if incremental and not (incremental_scope.affected_uuids
                            or incremental_scope.affected_tool_use_ids):
        return 0
    changed = 0
    with db.viz_conn() as conn:
        conn.execute("SET LOCAL work_mem = '64MB'")
        # Preserve both model and rate state written by a newer binary: defer
        # adoption until a binary that understands that pricing version runs.
        newer_ids = [key for key, value in conn.execute(
            "SELECT id, value FROM dim_pricing_version").fetchall()
            if stored_pricing_version_is_newer(value, constants.PRICING_VERSION)]
        uuid_params = {
            "newer_ids": newer_ids,
            "file_extras": flags.CONFIG.collection.enabled("file_extras"),
            "uuids": (sorted(incremental_scope.affected_uuids)
                      if incremental else None),
        }
        tool_params = {
            "tool_ids": (sorted(incremental_scope.affected_tool_use_ids)
                         if incremental else None),
        }
        # Adopt each losing replay's original model and extend files.models
        # in one statement/snapshot, deriving added before rows change. Carry
        # the dim id for SET and its text for files.models, which stays TEXT[].
        #
        # Join REBUILT uuid text: equal text iff equal parts by uuid_split and
        # schema CHECKs. Winners carry uuids, so absent identities never join.
        # Use EQUALITY: IS NOT DISTINCT FROM over parts has no hash/merge join
        # path and can force quadratic full-table scans per replayed copy.
        cur = conn.execute(
            f"""
            WITH winners AS (
              SELECT r.uuid_col, r.seq, r.fallback,
                     r.model_id, dm.value AS model
                FROM records r
                JOIN dim_model dm ON dm.id = r.model_id
               WHERE r.is_canonical AND r.is_replay IS NOT TRUE
                 AND {_UUID_PRESENT}
            ), adopt AS (
              SELECT r.file_id, r.line_num,
                     w.model_id AS winner_model_id, w.model AS winner_model
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
            ), upd AS (
              UPDATE records r SET model_id = a.winner_model_id
                FROM adopt a
               WHERE r.file_id = a.file_id AND r.line_num = a.line_num
              RETURNING r.file_id, r.line_num
            ), added AS (
              SELECT file_id, array_agg(DISTINCT winner_model) AS added
                FROM adopt GROUP BY file_id
            ), files_upd AS (
              UPDATE files f
                 SET models = (SELECT array(SELECT DISTINCT m
                                              FROM unnest(f.models || added.added)
                                             AS m ORDER BY m))
                FROM added
               WHERE f.file_id = added.file_id AND %(file_extras)s
                 AND NOT ('file_extras' = ANY(
                       COALESCE(f.collection_flags, '{{}}'::text[])))
            )
            SELECT file_id, line_num FROM upd
            """, uuid_params)
        adopted_keys = cur.fetchall()
        changed += len(adopted_keys)
        repriced = reprice_adopted(conn, adopted_keys)

        # tool_uses: the same adoption keyed on tool_use_id.
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
    # These non-canonical sidecar writes do not drive full rebuild promotion;
    # vacuum still needs their actual write count, separately from adoption.
    ingest_vacuum.note_phase("reprice", repriced)
    if changed:
        log.info("adopt_original_models: %d rows adopted", changed)
    return changed
