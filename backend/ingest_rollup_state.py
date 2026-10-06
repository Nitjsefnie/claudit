"""Suppression, cross-file canonical flags, replayed-model adoption and
teammate role resolution."""
from __future__ import annotations

import logging
from datetime import datetime

from backend import db
from backend.constants import DEFAULT_AGENT_TYPE
from backend.ingest_scope import Scope, current_scope

log = logging.getLogger("claudit.ingest")

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
_UNATTRIBUTED = ("(COALESCE(model, '')"
                 " IN ('', 'unknown', '(unknown)', '<synthetic>'))")

# A replayed copy loses to an original of the same identity, whatever the
# key order (issue #687): a forked Codex rollout re-journals its parent's
# requests in its replayed prefix — attributed, after #653, to the fork's
# first declared model — so with both copies attributed the attribution
# rank tied and the fork's `subagents/…` key won, counting the parent's
# history as the fork's. NULL (every non-Codex row, and every row parsed
# before #687) ranks as an original.
_REPLAY_LAST = "(is_replay IS TRUE)"


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
        filter_sql = " AND tu.file_key = ANY(%(file_keys)s)" if incremental else ""
        params = {"file_keys": file_keys} if incremental else None
        cur = conn.execute(
            f"""
            DELETE FROM tool_uses tu
             WHERE EXISTS (SELECT 1 FROM suppressed_models s
                            WHERE tu.model ILIKE s.pattern){filter_sql}
            RETURNING 1
            """, params)
        deleted += len(cur.fetchall())
        cur = conn.execute(
            f"""
            DELETE FROM tool_uses tu
             USING records r
             WHERE tu.model IS NULL
               AND tu.file_key = r.file_key
               AND tu.line_num = r.line_num
               AND EXISTS (SELECT 1 FROM suppressed_models s
                            WHERE r.model ILIKE s.pattern){filter_sql}
            RETURNING 1
            """, params)
        deleted += len(cur.fetchall())
        filter_sql = " AND r.file_key = ANY(%(file_keys)s)" if incremental else ""
        cur = conn.execute(
            f"""
            DELETE FROM records r
             WHERE EXISTS (SELECT 1 FROM suppressed_models s
                            WHERE r.model ILIKE s.pattern){filter_sql}
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
                     COALESCE(tu.agent_type, %(default)s) AS agent_type
                FROM files f
                JOIN files p ON p.project_id = f.project_id
                            AND p.session_id = f.session_id
                JOIN tool_uses tu ON tu.file_key = p.file_key
               WHERE f.teammate_name IS NOT NULL
                 AND tu.dispatch_name = f.teammate_name
                 AND tu.is_error IS NOT TRUE
                 -- SV-CANONICAL-FLAG: a compaction sidecar replays the lead's dispatch; the original and twin share the role and timestamp.
                 AND tu.is_canonical
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
            RETURNING f.file_key
            """, {"default": DEFAULT_AGENT_TYPE})
        changed_files = {row[0] for row in cur.fetchall()}
        if scope is not None and not scope.full and changed_files:
            for project_id, hour in conn.execute(
                """
                SELECT f.project_id, date_trunc('hour', r.ts)
                  FROM records r JOIN files f ON f.file_key = r.file_key
                 WHERE r.file_key = ANY(%s)
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
            cur = conn.execute(
                f"""
                UPDATE records r SET is_canonical = w.canon
                  FROM (
                    SELECT file_key, line_num,
                           (uuid IS NULL OR ROW_NUMBER() OVER (
                              PARTITION BY uuid
                              ORDER BY {_REPLAY_LAST}, {_UNATTRIBUTED}, file_key,
                              line_num
                            ) = 1) AS canon
                      FROM records
                  ) w
                 WHERE r.file_key = w.file_key AND r.line_num = w.line_num
                   AND r.is_canonical IS DISTINCT FROM w.canon
                """
            )
            changed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            cur = conn.execute(
                f"""
                UPDATE tool_uses t SET is_canonical = w.canon
                  FROM (
                    SELECT file_key, line_num, idx,
                           (tool_use_id IS NULL OR ROW_NUMBER() OVER (
                              PARTITION BY tool_use_id
                              ORDER BY {_REPLAY_LAST}, {_UNATTRIBUTED}, file_key,
                              line_num, idx
                            ) = 1) AS canon
                      FROM tool_uses
                  ) w
                 WHERE t.file_key = w.file_key AND t.line_num = w.line_num
                   AND t.idx = w.idx
                   AND t.is_canonical IS DISTINCT FROM w.canon
                """
            )
            changed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        else:
            assert incremental_scope is not None
            if incremental_scope.affected_uuids:
                rows = conn.execute(
                    f"""
                    WITH winners AS (
                      SELECT file_key, line_num,
                             ROW_NUMBER() OVER (
                               PARTITION BY uuid
                               ORDER BY {_REPLAY_LAST}, {_UNATTRIBUTED},
                                      file_key, line_num
                             ) = 1 AS canon
                        FROM records WHERE uuid = ANY(%s)
                    ), changed AS (
                      UPDATE records r SET is_canonical = w.canon
                        FROM winners w JOIN files f ON f.file_key = w.file_key
                       WHERE r.file_key = w.file_key AND r.line_num = w.line_num
                         AND r.is_canonical IS DISTINCT FROM w.canon
                      RETURNING f.project_id, date_trunc('hour', r.ts) AS hour,
                                (r.reply_latency_s IS NOT NULL AND r.ts IS NULL)
                                  AS latency_null
                    ) SELECT project_id, hour, latency_null FROM changed
                    """, (sorted(incremental_scope.affected_uuids),),
                ).fetchall()
                _updated_record_rows(incremental_scope, rows)
                changed += len(rows)
            if incremental_scope.dirty_files:
                rows = conn.execute(
                    """
                    UPDATE records r SET is_canonical = TRUE
                      FROM files f
                     WHERE r.file_key = f.file_key
                       AND r.file_key = ANY(%s) AND r.uuid IS NULL
                       AND r.is_canonical IS DISTINCT FROM TRUE
                    RETURNING f.project_id, date_trunc('hour', r.ts) AS hour,
                              (r.reply_latency_s IS NOT NULL AND r.ts IS NULL)
                                AS latency_null
                    """, (sorted(incremental_scope.dirty_files),),
                ).fetchall()
                _updated_record_rows(incremental_scope, rows)
                changed += len(rows)

            if incremental_scope.affected_tool_use_ids:
                rows = conn.execute(
                    f"""
                    WITH winners AS (
                      SELECT file_key, line_num, idx,
                             ROW_NUMBER() OVER (
                               PARTITION BY tool_use_id
                               ORDER BY {_REPLAY_LAST}, {_UNATTRIBUTED},
                                      file_key, line_num, idx
                             ) = 1 AS canon
                        FROM tool_uses WHERE tool_use_id = ANY(%s)
                    ), changed AS (
                      UPDATE tool_uses t SET is_canonical = w.canon
                        FROM winners w JOIN files f ON f.file_key = w.file_key
                       WHERE t.file_key = w.file_key
                         AND t.line_num = w.line_num AND t.idx = w.idx
                         AND t.is_canonical IS DISTINCT FROM w.canon
                      RETURNING f.project_id,
                                date_trunc('hour', t.ts) AS hour
                    ) SELECT project_id, hour FROM changed
                    """, (sorted(incremental_scope.affected_tool_use_ids),),
                ).fetchall()
                _updated_tool_hours(incremental_scope, rows)
                changed += len(rows)
            if incremental_scope.dirty_files:
                rows = conn.execute(
                    """
                    UPDATE tool_uses t SET is_canonical = TRUE
                      FROM files f
                     WHERE t.file_key = f.file_key
                       AND t.file_key = ANY(%s) AND t.tool_use_id IS NULL
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
    files.models with the adopted models, since the fork file journals
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
            "uuids": (sorted(incremental_scope.affected_uuids)
                      if incremental else None),
        }
        tool_params = {
            "tool_ids": (sorted(incremental_scope.affected_tool_use_ids)
                         if incremental else None),
        }
        # records: a losing replayed copy takes its uuid's original's
        # model, and files.models gains it — one statement, one snapshot,
        # so `added` is derived before the rows move.
        cur = conn.execute(
            """
            WITH winners AS (
              SELECT uuid, model FROM records
               WHERE is_canonical AND is_replay IS NOT TRUE
                 AND uuid IS NOT NULL AND model IS NOT NULL
            ), adopt AS (
              SELECT r.file_key, r.line_num, w.model AS winner_model
                FROM records r JOIN winners w ON w.uuid = r.uuid
               WHERE r.is_replay IS TRUE AND r.is_canonical IS FALSE
                 AND r.model IS DISTINCT FROM w.model
                 AND (%(uuids)s::text[] IS NULL OR r.uuid = ANY(%(uuids)s))
            ), upd AS (
              UPDATE records r SET model = a.winner_model
                FROM adopt a
               WHERE r.file_key = a.file_key AND r.line_num = a.line_num
              RETURNING r.file_key
            ), added AS (
              SELECT file_key, array_agg(DISTINCT winner_model) AS added
                FROM adopt GROUP BY file_key
            ), files_upd AS (
              UPDATE files f
                 SET models = (SELECT array(SELECT DISTINCT m
                                              FROM unnest(f.models || added.added)
                                             AS m ORDER BY m))
                FROM added
               WHERE f.file_key = added.file_key
            )
            SELECT count(*) FROM upd
            """, uuid_params)
        row = cur.fetchone()
        changed += row[0] if row and row[0] else 0

        # tool_uses: the same adoption keyed on tool_use_id.
        cur = conn.execute(
            """
            WITH winners AS (
              SELECT tool_use_id, model FROM tool_uses
               WHERE is_canonical AND is_replay IS NOT TRUE
                 AND tool_use_id IS NOT NULL AND model IS NOT NULL
            ), adopt AS (
              SELECT t.file_key, t.line_num, t.idx, w.model AS winner_model
                FROM tool_uses t JOIN winners w ON w.tool_use_id = t.tool_use_id
               WHERE t.is_replay IS TRUE AND t.is_canonical IS FALSE
                 AND t.model IS DISTINCT FROM w.model
                 AND (%(tool_ids)s::text[] IS NULL
                      OR t.tool_use_id = ANY(%(tool_ids)s))
            )
            UPDATE tool_uses t SET model = a.winner_model
              FROM adopt a
             WHERE t.file_key = a.file_key AND t.line_num = a.line_num
               AND t.idx = a.idx
            """, tool_params)
        changed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
    if changed:
        log.info("adopt_original_models: %d rows adopted", changed)
    return changed
