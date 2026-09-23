"""Lane-project identity: the stored hash->project_id mapping, the
resolution rule, and the migration-stall rekey.

Split out of ingest.py when that module crossed pylint's 1000-line
budget; the functions move as one concern: the stored mapping a
marker-failed run falls back to, the marker->stored->hash resolution
the walk uses, and the rekey that moves stored rows when a run's
marker resolution disagrees with what is stored.
"""
from __future__ import annotations

import logging

from backend import db, key_layout, r2

log = logging.getLogger("claudit.ingest")

_LIKE_FMT = "%/sessions/{}/%"


def stored_lane_ids() -> dict[str, str]:
    """Lane hash -> the project id its files are currently stored under.

    The mapping a run consults when a project's marker was NOT read
    (missing, malformed, or its GET failed): the stored mapping keeps
    the slug a marker run chose, so a transient marker-fetch failure
    cannot flip a slug-keyed project back to its hash and split one
    project into two ids per hash. The mapping is read back from the
    files rows themselves - a lane file_key's object part is
    sessions/<hash>/... - so no extra table is needed; a hash with no
    stored rows (first sight) falls back to the hash.

    If a crash ever left one hash's files split across two ids, the
    choice is deterministic: prefer the slug form, then the side holding
    more files, then the id itself - so a marker-failed run consolidates
    onto the same side every time.
    """
    counts: dict[str, dict[str, int]] = {}
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT file_key, project_id FROM files "
            "WHERE file_key LIKE '%/sessions/%'"
        ).fetchall()
    for file_key, project_id in rows:
        parts = r2.split_key(file_key)[1].split("/")
        if len(parts) < 2 or parts[0] != key_layout.LANE_ROOT:
            continue
        ids = counts.setdefault(parts[1], {})
        ids[project_id] = ids.get(project_id, 0) + 1
    return {
        lane_hash: preferred_stored_id(ids)
        for lane_hash, ids in counts.items()
    }


def preferred_stored_id(ids: dict[str, int]) -> str:
    """Slug form first, then the side holding more files, then the id.

    A module-level function rather than a closure over the dict
    comprehension's loop variable: a lambda capturing a loop variable
    trips pylint's cell-var-from-loop, and the preference rule is a
    policy worth naming anyway. Windows slugs (a drive letter + '--')
    rank like hash ids here — none starts with '-' — and whichever side
    wins, resolve_lane_project folds it to the canonical form, so two
    sides differing only in case still resolve onto one id.
    """
    return min(
        ids,
        key=lambda pid: (not pid.startswith("-"), -ids[pid], pid),
    )


def resolve_lane_project(lane_hash: str, marker_path: str | None,
                         stored_lane: dict[str, str]) -> str:
    """The id a file classified under lane hash `lane_hash` lands under
    this run: a marker path read THIS RUN wins (the path's canonical
    project id — key_layout.project_slug, case-folded when it names a
    Windows directory), else the stored mapping keeps the id a previous
    marker run chose, folded through the same canonical form so a
    mixed-case id stored by a previous parser version resolves onto the
    folded id this run, else the bare hash (legacy Kimi has no marker; a
    never-stored hash has nothing to keep)."""
    if marker_path is not None:
        return key_layout.canonical_project_id(
            key_layout.project_slug(marker_path))
    return key_layout.canonical_project_id(
        stored_lane.get(lane_hash, lane_hash))


def rekey_stale_lane_projects(project_paths: dict[str, str],
                              stored_lane: dict[str, str]) -> int:
    """Re-key stored files whose hash->project mapping moved under them.

    The reparse gate keys on etag+parser_version, so a project whose
    slug id was chosen on a run LATER than its files' last reparse (a
    marker failure on the migration run, then a marker-OK run with no
    new bytes) would sit hash-keyed until each file's etag changed, and
    the first per-file reparse after that would list the directory under
    TWO project ids. Files are stored per key with a project_id column,
    so the move is a direct UPDATE: upsert the slug project row first
    (the FK needs it, carrying the marker path as display_name and the
    moved files' real first/last seen), move every sessions/<hash>/ row,
    then drop the old id's project row unless other files still
    reference it (a split hash keeps its other side). A marker-failed
    run re-keys nothing - it has no project_paths entry. Returns the
    number of files moved.
    """
    moved = 0
    with db.viz_conn() as c, c.cursor() as cur:
        for lane_hash, marker_path in project_paths.items():
            # The folded canonical id: a marker path naming a Windows
            # directory keys the project lowercase, and a stored
            # mixed-case id from a previous parser version compares
            # unequal to it — which is what triggers the rekey below.
            slug = key_layout.canonical_project_id(
                key_layout.project_slug(marker_path))
            stored_id = stored_lane.get(lane_hash)
            if stored_id is None or stored_id == slug:
                continue
            pattern = _LIKE_FMT.format(lane_hash)
            cur.execute(
                """
                INSERT INTO projects (project_id, display_name,
                  first_seen_at, last_seen_at)
                SELECT %s, %s, MIN(r2_last_modified), MAX(r2_last_modified)
                  FROM files WHERE file_key LIKE %s
                HAVING COUNT(*) > 0
                ON CONFLICT (project_id) DO NOTHING
                """,
                (slug, marker_path, pattern),
            )
            cur.execute(
                "UPDATE files SET project_id = %s WHERE file_key LIKE %s",
                (slug, pattern),
            )
            moved += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            cur.execute(
                "DELETE FROM projects WHERE project_id = %s "
                "AND NOT EXISTS (SELECT 1 FROM files WHERE project_id = %s)",
                (stored_id, stored_id),
            )
        c.commit()
    log.info("ingest: re-keyed %d file(s) onto their slug project", moved)
    return moved
