"""Suppression, cross-file canonical flags and teammate role resolution."""
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
# attributes as `unknown`. NULL is unattributed too. `unknown` is the lane
# parsers' fallback; `(unknown)` is the Claude path's; `<synthetic>` is
# Claude's harness-fabricated stub model, which names no model at all
# (issue #563, kept in lockstep with the browser's isAttributed).
_UNATTRIBUTED = ("(COALESCE(model, '')"
                 " IN ('', 'unknown', '(unknown)', '<synthetic>'))")


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
    """Set canonical flags globally, or only for identities a run touched."""
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
                              ORDER BY {_UNATTRIBUTED}, file_key, line_num
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
                              ORDER BY {_UNATTRIBUTED}, file_key, line_num, idx
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
                               ORDER BY {_UNATTRIBUTED}, file_key, line_num
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
                               ORDER BY {_UNATTRIBUTED}, file_key, line_num, idx
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
    return changed
