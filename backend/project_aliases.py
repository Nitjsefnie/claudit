"""User-managed project grouping: the project_aliases fold pass.

One deploy's project list is dominated by worktree/scratch ids — one
repository's cost is split across hundreds of ids a `-tmp-%`-shaped
pattern could name. The fix is DATA, not code: a per-deploy
`project_aliases` table (pattern → target project id, plus note),
shipped EMPTY in schema.sql, applied at ingest so every stored row and
rollup sees the target id. Matching is SQL LIKE, case-sensitive — POSIX
project slugs are case-sensitive, and the Windows ones are already
case-folded by key_layout.canonical_project_id. In each pass, a stored
project id is resolved EXACTLY ONCE against the current alias list: the
first pattern in lexicographic `pattern` order that matches it names the
target, and aliases do not chain within that pass. A target that itself
matches another alias can advance one hop on a later pass. Repeated
passes converge without duplicating or losing files while the alias set
is acyclic on the ids it matches. A cyclic alias set is an operator
error: folded ids advance around the cycle on successive ingests, so fix
the table. A pass with no matching source ids moves nothing. A project
whose own id equals its matched target is never moved and never deleted
(the pass drops emptied source project rows, and files FK-cascade on
project delete).

Marker-backed lane reconciliation uses `resolve_chain` to find the
marker slug's bounded alias-chain destination before moving files: it
repeats the same first-match lookup and stops at an unmatched id, before
a revisited id, or at eight hops, so a stopped walk may return an id
that still matches an alias. The fold pass above remains one hop per
ingest pass.

Deleting a row stops folding new files. Already-folded rows stay at the
target only until another identity pass re-keys them: a reparse derives
the raw id from the object key, while marker-backed lane files follow the
reconciliation described above on the next ingest.
"""
from __future__ import annotations

import logging

import psycopg

from backend import db

log = logging.getLogger("claudit.ingest")


def resolve(c: psycopg.Connection, project_id: str) -> str:
    """Resolve one id through the first matching alias, or keep it.

    Uses the same case-sensitive LIKE and lexicographic first-match rule
    as the fold pass. This is one lookup only: aliases do not chain here.
    """
    row = c.execute(
        """
        SELECT a.project_id
          FROM project_aliases a
         WHERE %s LIKE a.pattern
         ORDER BY a.pattern
         LIMIT 1
        """,
        (project_id,)).fetchone()
    return row[0] if row else project_id


def resolve_chain(c: psycopg.Connection, project_id: str) -> str:
    """Resolve successive first-match aliases to a lane marker's fixed point.

    Each hop uses the same case-sensitive LIKE and lexicographic
    first-match rule as `resolve`. Stop when no pattern matches or when
    the next id was already visited, returning the last unseen id.
    Acyclic chains stop at their fixed point earlier; the eight-hop cap
    bounds pathological or cyclic chains.
    """
    seen = {project_id}
    current = project_id
    for _ in range(8):
        target = resolve(c, current)
        if target == current or target in seen:
            return current
        seen.add(target)
        current = target
    return current


def folded_pairs(c: psycopg.Connection) -> dict[str, str]:
    """The moves this pass would make, resolved once per stored id.

    Every DISTINCT `files.project_id` is matched against the alias list
    IN SQL — the first pattern, in the PRIMARY KEY's own lexicographic
    order, that the id matches with a case-sensitive LIKE names the
    target. An id that matches no pattern, or whose matched target is
    the id itself, is absent from the map. Resolving the PRE-fold id
    set in one query is what makes this pass chain-free and independent
    of application order; a target can advance on a later pass.
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
    deleting one stops folding NEW files. Already-folded rows stay at
    the target only until another identity pass re-keys them: a reparse
    derives the raw id from the object key, while marker-backed lane
    files re-converge on the next ingest to the marker slug's bounded
    alias-chain destination (after deletion, the chain resolves under the
    remaining aliases — with all alias rows gone, that is the marker
    slug); the walk stops at an unmatched id, before a revisited id, or
    at eight hops, so a stopped walk may return an id that still matches
    an alias. Repeated passes converge one alias hop at a time without
    duplicating or losing files while the alias set is acyclic
    on the ids it matches. A cyclic alias set is an operator error:
    folded ids advance around the cycle on successive ingests, so fix
    the table. A pass that changes nothing moves 0. Needs no reparse,
    no R2 fetch, and no PARSER_VERSION bump:
    only stored identity moves; token columns and costs are untouched.
    Returns the number of project ids re-keyed.
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
