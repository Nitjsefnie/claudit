"""User-managed project grouping: the project_aliases fold pass.

One deploy's project list is dominated by worktree/scratch ids — one
repository's cost is split across hundreds of ids a `-tmp-%`-shaped
pattern could name. The fix is DATA, not code: a per-deploy
`project_aliases` table (pattern → target project id, plus note),
shipped EMPTY in schema.sql, applied at ingest so every stored row and
rollup sees the target id. Matching is SQL LIKE, case-sensitive — POSIX
project slugs are case-sensitive, and the Windows ones are already
case-folded by key_layout.canonical_project_id. Each stored project id
is resolved EXACTLY ONCE against the current alias list: the first
pattern in lexicographic `pattern` order that matches it names the
target, and no alias chains — a target that itself matches other
patterns is not re-resolved. A project whose own id equals its matched
target is never moved and never deleted (the pass drops emptied source
project rows, and files FK-cascade on project delete).
"""
from __future__ import annotations

import logging

import psycopg

from backend import db

log = logging.getLogger("claudit.ingest")


def folded_pairs(c: psycopg.Connection) -> dict[str, str]:
    """The moves this pass would make, resolved once per stored id.

    Every DISTINCT `files.project_id` is matched against the alias list
    IN SQL — the first pattern, in the PRIMARY KEY's own lexicographic
    order, that the id matches with a case-sensitive LIKE names the
    target. An id that matches no pattern, or whose matched target is
    the id itself, is absent from the map. Resolving the PRE-fold id
    set in one query is what makes the pass chain-free and independent
    of application order.
    """
    rows = c.execute(
        """
        SELECT f.project_id,
               (SELECT a.project_id
                  FROM project_aliases a
                 WHERE f.project_id LIKE a.pattern
                 ORDER BY a.pattern
                 LIMIT 1)
          FROM (SELECT DISTINCT project_id FROM files) f
        """
    ).fetchall()
    return {src: tgt for src, tgt in rows
            if tgt is not None and tgt != src}


def rekey_folded_projects() -> int:
    """Fold aliased project ids onto their targets across stored state.

    Runs in _rebuild_derived_state after "reprice" and before
    "canonical" — identity before any derived state, so the rollups
    rebuilt after it see only folded ids. Per move: upsert the target
    `projects` row (display_name = the target id only when the row is
    newly created; an existing row is never overwritten), move every
    file in ONE statement evaluated against the pre-fold ids, then drop
    each emptied source row — the NOT EXISTS guard keeps a project that
    is another move's target (its row freshly repopulated by this same
    pass) from being dropped.

    Adding or editing a row re-keys stored rows on the next ingest;
    deleting one stops folding NEW files only — already-folded rows
    keep the target id, and the raw id is not retained, so an unfold is
    not possible. Idempotent: a second pass moves 0. Needs no reparse,
    no R2 fetch, and no PARSER_VERSION bump: only stored identity
    moves; token columns and costs are untouched. Returns the number of
    project ids re-keyed.
    """
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT EXISTS (SELECT 1 FROM project_aliases)").fetchone()
        if not row or not row[0]:
            return 0
        moves = folded_pairs(c)
        if not moves:
            return 0
        with c.cursor() as cur:
            for src in sorted(moves):
                tgt = moves[src]
                cur.execute(
                    """
                    INSERT INTO projects (project_id, display_name,
                      first_seen_at, last_seen_at)
                    SELECT %s, %s, MIN(r2_last_modified), MAX(r2_last_modified)
                      FROM files WHERE project_id = %s
                    HAVING COUNT(*) > 0
                    ON CONFLICT (project_id) DO NOTHING
                    """,
                    (tgt, tgt, src))
            # One statement over the whole moves map, not one UPDATE per
            # source: the join reads the pre-statement ids and each row
            # is updated at most once, so rows folded onto a project
            # that is itself aliased stop there instead of moving on.
            cur.execute(
                """
                UPDATE files f
                   SET project_id = m.tgt
                  FROM unnest(%(srcs)s::text[], %(tgts)s::text[])
                       AS m(src, tgt)
                 WHERE f.project_id = m.src
                """,
                {"srcs": sorted(moves),
                 "tgts": [moves[s] for s in sorted(moves)]})
            cur.execute(
                """
                DELETE FROM projects p
                 WHERE p.project_id = ANY(%s)
                   AND NOT EXISTS (SELECT 1 FROM files f
                                    WHERE f.project_id = p.project_id)
                """,
                (sorted(moves),))
        c.commit()
    log.info("rekey_folded_projects: %d project id(s) folded", len(moves))
    return len(moves)
