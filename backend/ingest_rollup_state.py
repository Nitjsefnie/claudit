"""Suppression, cross-file canonical flags, replayed-model adoption and
teammate role resolution."""
from __future__ import annotations

import logging
from datetime import datetime

from backend import db, flags, uuid_split
from backend.constants import DEFAULT_AGENT_TYPE
from backend.ingest_scope import Scope, current_scope

log = logging.getLogger("claudit.ingest")

# records.uuid is the split column set (issue #959 task 8). Every read
# below rebuilds or groups by it through these three fragments, so the
# winner rule and the incremental probes keep seeing the ORIGINAL text:
#   _UUID_TEXT      — the byte-identical rebuild, for equality probes;
#   _UUID_PARTITION — the grouping key, the parts rather than the text:
#                     equal parts iff equal text (uuid_split's docstring
#                     and schema_dims' CHECKs), so this picks the same
#                     winners as PARTITION BY uuid did, without building
#                     a 40-byte string per row per pass;
#   _UUID_ABSENT    — `uuid IS NULL` (always-canonical legacy rows).
_UUID_TEXT = uuid_split.uuid_text_sql("r")
_WINNER_UUID_TEXT = uuid_split.uuid_text_sql("w")
_UUID_PARTITION = uuid_split.uuid_partition_sql("r")
_UUID_ABSENT = uuid_split.uuid_absent_sql("r")
_UUID_PRESENT = uuid_split.uuid_present_sql("r")

# The canonical winner prefers an ATTRIBUTED copy (issue #529): a forked
# Codex rollout replays its parent's history with no model declaration in
# front of it, and a multi-model fork has no sole-model fallback, so its
# replayed copies store model `unknown` — and their `subagents/…` file key
# sorts before the parent's `wire.jsonl`, so a bare file_key ordering let
# them win the dedup and the Models panel showed usage every other copy
# attributes as `unknown`. NULL is unattributed too. The placeholder
# spellings are HISTORICAL rows only: since #653/#688 no parser emits
# them (a live model-less file is refused at parse); `<synthetic>` is
# Claude's harness-fabricated stub model, which names no model at all
# (issue #563, kept in lockstep with the browser's isAttributed).
# Since #959 the model is a dim FK, so the rank reads the dim's TEXT —
# through the `dm` alias every winner query joins. records.model_id is
# NOT NULL and tool_uses.model_id nullable, so COALESCE keeps the old
# NULL-is-unattributed behaviour on the tool side.
_UNATTRIBUTED_SQL = ("(COALESCE(dm.value, '')"
                     " IN ('', 'unknown', '(unknown)', '<synthetic>'))")

# A replayed copy loses to an original of the same identity, whatever the
# key order (issue #687): a forked Codex rollout re-journals its parent's
# requests in its replayed prefix — attributed, after #653, to the fork's
# first declared model — so with both copies attributed the attribution
# rank tied and the fork's `subagents/…` key won, counting the parent's
# history as the fork's. NULL (every non-Codex row, and every row parsed
# before #687) ranks as an original.


def _replay_last(fact_alias: str) -> str:
    """The replay rank as SQL over the fact table's alias."""
    return f"({fact_alias}.is_replay IS TRUE)"


def _winner_order(fact_alias: str) -> str:
    """The tie-break chain, ending in file_key TEXT order (R2).

    files.file_key is JOINED, never the fact's file_id: file_id is
    first-seen order, which would flip ties the browser's
    record-dedup.js — whose arrival order stands in for file_key —
    resolves the other way (SV-CANONICAL-FLAG lockstep).
    """
    return (f"{_replay_last(fact_alias)}, {_UNATTRIBUTED_SQL}, "
            f"f.file_key, {fact_alias}.line_num")


def _phase_scope(scope: Scope | None) -> Scope | None:
    """Use an explicit scope, or the active ingest's scope."""
    return scope if scope is not None else current_scope()


def purge_suppressed(scope: Scope | None = None) -> int:
    """Delete suppressed records and calls, restricted to dirty files if scoped."""
    scope = _phase_scope(scope)
    incremental_scope = (
        scope if scope is not None and not scope.full else None)
    incremental = incremental_scope is not None
    file_keys = (sorted(incremental_scope.dirty_files)
                 if incremental_scope is not None else None)
    if incremental_scope is not None and not file_keys:
        return 0
    with db.viz_conn() as conn:
        row = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM suppressed_models)"
        ).fetchone()
        if not row or not row[0]:
            return 0
        deleted = 0
        # Model text lives in dim_model now (#959), so the ILIKE match
        # reads the dim row for each fact's model_id; file scoping goes
        # through files.file_key -> file_id.
        filter_sql = (" AND tu.file_id IN (SELECT file_id FROM files "
                      "WHERE file_key = ANY(%(file_keys)s))") if incremental else ""
        params = {"file_keys": file_keys} if incremental else None
        cur = conn.execute(
            f"""
            DELETE FROM tool_uses tu
             WHERE EXISTS (SELECT 1 FROM dim_model dm
                            WHERE dm.id = tu.model_id
                              AND EXISTS (SELECT 1 FROM suppressed_models s
                                           WHERE dm.value ILIKE s.pattern))
               {filter_sql}
            RETURNING 1
            """, params)
        deleted += len(cur.fetchall())
        cur = conn.execute(
            f"""
            DELETE FROM tool_uses tu
             USING records r
             WHERE tu.model_id IS NULL
               AND tu.file_id = r.file_id
               AND tu.line_num = r.line_num
               AND EXISTS (SELECT 1 FROM dim_model dm
                            WHERE dm.id = r.model_id
                              AND EXISTS (SELECT 1 FROM suppressed_models s
                                           WHERE dm.value ILIKE s.pattern))
               {filter_sql}
            RETURNING 1
            """, params)
        deleted += len(cur.fetchall())
        filter_sql = (" AND r.file_id IN (SELECT file_id FROM files "
                      "WHERE file_key = ANY(%(file_keys)s))") if incremental else ""
        cur = conn.execute(
            f"""
            DELETE FROM records r
             WHERE EXISTS (SELECT 1 FROM dim_model dm
                            WHERE dm.id = r.model_id
                              AND EXISTS (SELECT 1 FROM suppressed_models s
                                           WHERE dm.value ILIKE s.pattern))
               {filter_sql}
            RETURNING 1
            """, params)
        deleted += len(cur.fetchall())
        conn.commit()
    if deleted:
        log.info("purge_suppressed: %d rows dropped", deleted)
    return deleted


def resolve_teammate_agent_types(scope: Scope | None = None) -> int:
    """Resolve named teammate roles, adding their hours to an incremental scope."""
    scope = _phase_scope(scope)
    with db.viz_conn() as conn:
        cur = conn.execute(
            """
            WITH resolved AS (
              SELECT DISTINCT ON (f.file_key) f.file_key,
                     COALESCE(dt.value, %(default)s) AS agent_type
                FROM files f
                JOIN files p ON p.project_id = f.project_id
                            AND p.session_id = f.session_id
                JOIN tool_uses tu ON tu.file_id = p.file_id
                LEFT JOIN dim_agent_type dt ON dt.id = tu.agent_type_id
               WHERE f.teammate_name IS NOT NULL
                 AND tu.dispatch_name = f.teammate_name
                 AND tu.is_error IS NOT TRUE
                 -- SV-CANONICAL-FLAG: a compaction sidecar replays the lead's dispatch; the original and twin share the role and timestamp.
                 AND tu.is_canonical
                 AND tu.ts <= COALESCE(
                       (SELECT min(r.ts) FROM records r
                         WHERE r.file_id = f.file_id),
                       'infinity')
               ORDER BY f.file_key, tu.ts DESC, tu.line_num DESC
            )
            UPDATE files f SET agent_type = resolved.agent_type
              FROM resolved
             WHERE f.file_key = resolved.file_key
               AND f.agent_type <> resolved.agent_type
            RETURNING f.file_key
            """, {"default": DEFAULT_AGENT_TYPE})
        changed_files = {row[0] for row in cur.fetchall()}
        if scope is not None and not scope.full and changed_files:
            for project_id, hour in conn.execute(
                """
                SELECT f.project_id, date_trunc('hour', r.ts)
                  FROM records r JOIN files f ON f.file_id = r.file_id
                 WHERE f.file_key = ANY(%s)
                """, (list(changed_files),),
            ).fetchall():
                scope.add_hour(project_id, hour)
        conn.commit()
    changed = len(changed_files)
    log.info("resolve_teammate_agent_types: %d rows", changed)
    return changed


def _updated_record_rows(
        scope: Scope,
        rows: list[tuple[str, datetime | None, bool]]) -> None:
    """Join flipped row keys to their project and retain their old/new bucket."""
    for project_id, hour, latency_null in rows:
        scope.add_hour(project_id, hour)
        scope.latency_null = scope.latency_null or latency_null


def _updated_tool_hours(
        scope: Scope,
        rows: list[tuple[str, datetime | None]]) -> None:
    for project_id, hour in rows:
        scope.add_hour(project_id, hour)


def recompute_canonical(scope: Scope | None = None) -> int:
    """Set canonical flags globally, or only for identities a run touched.

    The pass's tail is the winner rule's model half (issue #713): once
    the flags have settled, every losing replayed copy adopts its
    original's model (adopt_original_models), so the return value counts
    reflagged rows and adopted rows together.
    """
    scope = _phase_scope(scope)
    incremental_scope = (
        scope if scope is not None and not scope.full else None)
    incremental = incremental_scope is not None
    if incremental_scope is not None and not (
            incremental_scope.affected_uuids
            or incremental_scope.affected_tool_use_ids
            or incremental_scope.dirty_files):
        return 0
    changed = 0
    with db.viz_conn() as conn:
        conn.execute("SET LOCAL work_mem = '64MB'")
        if not incremental:
            cur = conn.execute(db.sql_text(f"""
                UPDATE records r SET is_canonical = w.canon
                  FROM (
                    SELECT r.file_id, r.line_num,
                           ({_UUID_ABSENT} OR ROW_NUMBER() OVER (
                              PARTITION BY {_UUID_PARTITION}
                              ORDER BY {_winner_order('r')}
                            ) = 1) AS canon
                      FROM records r
                      JOIN files f ON f.file_id = r.file_id
                      JOIN dim_model dm ON dm.id = r.model_id
                  ) w
                 WHERE r.file_id = w.file_id AND r.line_num = w.line_num
                   AND r.is_canonical IS DISTINCT FROM w.canon
                """))
            changed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            cur = conn.execute(db.sql_text(f"""
                UPDATE tool_uses t SET is_canonical = w.canon
                  FROM (
                    SELECT t.file_id, t.line_num, t.idx,
                           (t.tool_use_id IS NULL OR ROW_NUMBER() OVER (
                              PARTITION BY t.tool_use_id
                              ORDER BY {_winner_order('t')}, t.idx
                            ) = 1) AS canon
                      FROM tool_uses t
                      JOIN files f ON f.file_id = t.file_id
                      LEFT JOIN dim_model dm ON dm.id = t.model_id
                  ) w
                 WHERE t.file_id = w.file_id AND t.line_num = w.line_num
                   AND t.idx = w.idx
                   AND t.is_canonical IS DISTINCT FROM w.canon
                """))
            changed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        else:
            assert incremental_scope is not None
            if incremental_scope.affected_uuids:
                rows = conn.execute(db.sql_text(f"""
                    WITH winners AS (
                      SELECT r.file_id, r.line_num,
                             ROW_NUMBER() OVER (
                               PARTITION BY {_UUID_PARTITION}
                               ORDER BY {_winner_order('r')}
                             ) = 1 AS canon
                        FROM records r
                        JOIN files f ON f.file_id = r.file_id
                        JOIN dim_model dm ON dm.id = r.model_id
                       WHERE {_UUID_TEXT} = ANY(%s)
                    ), changed AS (
                      UPDATE records r SET is_canonical = w.canon
                        FROM winners w JOIN files f ON f.file_id = w.file_id
                       WHERE r.file_id = w.file_id AND r.line_num = w.line_num
                         AND r.is_canonical IS DISTINCT FROM w.canon
                      RETURNING f.project_id, date_trunc('hour', r.ts) AS hour,
                                (r.reply_latency_s IS NOT NULL AND r.ts IS NULL)
                                  AS latency_null
                    ) SELECT project_id, hour, latency_null FROM changed
                    """), (sorted(incremental_scope.affected_uuids),),
                ).fetchall()
                _updated_record_rows(incremental_scope, rows)
                changed += len(rows)
            if incremental_scope.dirty_files:
                rows = conn.execute(
                    f"""
                    UPDATE records r SET is_canonical = TRUE
                      FROM files f
                     WHERE r.file_id = f.file_id
                       AND f.file_key = ANY(%s) AND {_UUID_ABSENT}
                       AND r.is_canonical IS DISTINCT FROM TRUE
                    RETURNING f.project_id, date_trunc('hour', r.ts) AS hour,
                              (r.reply_latency_s IS NOT NULL AND r.ts IS NULL)
                                AS latency_null
                    """, (sorted(incremental_scope.dirty_files),),
                ).fetchall()
                _updated_record_rows(incremental_scope, rows)
                changed += len(rows)

            if incremental_scope.affected_tool_use_ids:
                rows = conn.execute(db.sql_text(f"""
                    WITH winners AS (
                      SELECT t.file_id, t.line_num, t.idx,
                             ROW_NUMBER() OVER (
                               PARTITION BY t.tool_use_id
                               ORDER BY {_winner_order('t')}, t.idx
                             ) = 1 AS canon
                        FROM tool_uses t
                        JOIN files f ON f.file_id = t.file_id
                        LEFT JOIN dim_model dm ON dm.id = t.model_id
                       WHERE t.tool_use_id = ANY(%s)
                    ), changed AS (
                      UPDATE tool_uses t SET is_canonical = w.canon
                        FROM winners w JOIN files f ON f.file_id = w.file_id
                       WHERE t.file_id = w.file_id
                         AND t.line_num = w.line_num AND t.idx = w.idx
                         AND t.is_canonical IS DISTINCT FROM w.canon
                      RETURNING f.project_id,
                                date_trunc('hour', t.ts) AS hour
                    ) SELECT project_id, hour FROM changed
                    """), (sorted(incremental_scope.affected_tool_use_ids),),
                ).fetchall()
                _updated_tool_hours(incremental_scope, rows)
                changed += len(rows)
            if incremental_scope.dirty_files:
                rows = conn.execute(
                    """
                    UPDATE tool_uses t SET is_canonical = TRUE
                      FROM files f
                     WHERE t.file_id = f.file_id
                       AND f.file_key = ANY(%s) AND t.tool_use_id IS NULL
                       AND t.is_canonical IS DISTINCT FROM TRUE
                    RETURNING f.project_id, date_trunc('hour', t.ts)
                    """, (sorted(incremental_scope.dirty_files),),
                ).fetchall()
                _updated_tool_hours(incremental_scope, rows)
                changed += len(rows)
        conn.commit()
    if changed:
        log.info("recompute_canonical: %d rows reflagged", changed)
    # The winner rule's model half (issue #713): now that the rank has
    # settled, a losing replayed copy adopts its original's model.
    return changed + adopt_original_models(scope)


def adopt_original_models(scope: Scope | None = None) -> int:
    """A replayed copy adopts the model the parent had in force (issue #713).

    A forked rollout stores its replayed copies under the fork's FIRST
    declared model (#653) — the model at the fork point, which a
    mid-history switch makes wrong for the earlier requests. The parent
    is a different file, so no parse can resolve this; the canonical
    winner of the uuid can, because while the parent's copy is present it
    IS the winner: an original beats a replay whatever the key order
    (SV-CANONICAL-FLAG). Every losing replayed copy therefore adopts its
    winner's model, records by uuid and tool calls by tool_use_id; with
    no original present (the replay is itself canonical, or every copy is
    a replay) the #653 fallback stands. A NULL winner model is not
    adopted — the copy keeps its attribution rather than losing it.

    Runs at recompute_canonical's tail, on the flags it has just
    settled. Adopted rows are non-canonical, so no rollup
    reads them and the pass adds no hours to the scope; it extends
    collected files.models with the adopted models, since the fork file journals
    its parent's requests.
    """
    scope = _phase_scope(scope)
    incremental_scope = (
        scope if scope is not None and not scope.full else None)
    incremental = incremental_scope is not None
    if incremental and not (incremental_scope.affected_uuids
                            or incremental_scope.affected_tool_use_ids):
        return 0
    changed = 0
    with db.viz_conn() as conn:
        conn.execute("SET LOCAL work_mem = '64MB'")
        uuid_params = {
            "file_extras": flags.CONFIG.collection.enabled("file_extras"),
            "uuids": (sorted(incremental_scope.affected_uuids)
                      if incremental else None),
        }
        tool_params = {
            "tool_ids": (sorted(incremental_scope.affected_tool_use_ids)
                         if incremental else None),
        }
        # records: a losing replayed copy takes its uuid's original's
        # model, and files.models gains it — one statement, one snapshot,
        # so `added` is derived before the rows move. The model travels
        # as its dim id (the SET) beside the dim's TEXT (files.models,
        # which stays a TEXT[] on the small files table, #959).
        # The join is on the REBUILT identity text — the one spelling
        # every other uuid read uses. Equal texts iff equal parts
        # (uuid_split's docstring + schema_dims' CHECK disjointness), so
        # this matches exactly the pairs `w.uuid = r.uuid` matched —
        # winners are always uuid-bearing, so an absent-uuid row still
        # never joins. It must be EQUALITY: `IS NOT DISTINCT FROM` over
        # the parts has no hash or merge join path, which forced the
        # planner into a nested loop whose inner side seq-scanned the
        # whole records table once per replayed copy — quadratic at
        # corpus scale (measured: 14,011 outer rows against 1.79M inner,
        # hours; the equality form plans as a parallel hash join).
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
               WHERE r.is_replay IS TRUE AND r.is_canonical IS FALSE
                 AND r.model_id IS DISTINCT FROM w.model_id
                 AND (%(uuids)s::text[] IS NULL
                      OR {_UUID_TEXT} = ANY(%(uuids)s))
            ), upd AS (
              UPDATE records r SET model_id = a.winner_model_id
                FROM adopt a
               WHERE r.file_id = a.file_id AND r.line_num = a.line_num
              RETURNING r.file_id
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
            SELECT count(*) FROM upd
            """, uuid_params)
        row = cur.fetchone()
        changed += row[0] if row and row[0] else 0

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
    if changed:
        log.info("adopt_original_models: %d rows adopted", changed)
    return changed
