"""Lane project marker -> stored id -> hash resolution and stale-id rekey.

The stored mapping preserves project identity when a marker fetch fails.
"""
from __future__ import annotations

import logging

from backend import db, key_layout, project_aliases, r2
from backend.ingest_persist import _strip_nul

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
    """Resolve a lane hash by current marker path, then stored mapping, then hash.

    Canonicalize marker and stored ids through project_slug, including
    Windows case folding. Legacy Kimi has no marker; a never-stored hash
    has no mapping to preserve."""
    if marker_path is not None:
        return key_layout.canonical_project_id(
            key_layout.project_slug(marker_path))
    return key_layout.canonical_project_id(
        stored_lane.get(lane_hash, lane_hash))


def rekey_stale_lane_projects(project_paths: dict[str, str],
                              stored_lane: dict[str, str]) -> int:
    """Re-key files when a lane hash's marker mapping changes independently of etag.

    Without this pass, a successful marker read after a failed one would
    leave unchanged files under the hash and reparsed files under the
    slug. Insert the destination project first for its FK, preserving
    marker display path and moved files' first/last seen. Move every
    sessions/<hash>/ row, then drop the source only if unreferenced.

    Resolve the marker slug through first-match aliases, stopping at an
    unmatched id, before a revisit, or after eight hops. A marker-failed
    run has no project_paths entry and re-keys nothing. Return moved files."""
    moved = 0
    with db.viz_conn() as c, c.cursor() as cur:
        for lane_hash, raw_marker_path in project_paths.items():
            # The path is marker content: strip NUL before it derives the
            # slug AND the display_name, so neither text column sees the
            # byte.
            marker_path = _strip_nul(raw_marker_path)
            # Windows marker paths canonicalize to lowercase. A stored mixed-case
            # id compares unequal and must re-key to the folded destination.
            slug = key_layout.canonical_project_id(
                key_layout.project_slug(marker_path))
            target_id = project_aliases.resolve_chain(c, slug)
            stored_id = stored_lane.get(lane_hash)
            if stored_id is None or stored_id == target_id:
                continue
            pattern = _LIKE_FMT.format(lane_hash)
            cur.execute(
                """
                INSERT INTO projects (project_id, display_name,
                  first_seen_at, last_seen_at)
                SELECT %s, %s, MIN(r2_last_modified), MAX(r2_last_modified)
                  FROM files
                 WHERE file_key LIKE %s
                   AND NOT EXISTS (SELECT 1 FROM projects p
                                    WHERE p.project_id = %s)
                HAVING COUNT(*) > 0
                ON CONFLICT (project_id) DO NOTHING
                """,
                (target_id, marker_path, pattern, target_id),
            )
            cur.execute(
                "UPDATE files SET project_id = %s WHERE file_key LIKE %s",
                (target_id, pattern),
            )
            moved += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            cur.execute(
                "DELETE FROM projects WHERE project_id = %s "
                "AND NOT EXISTS (SELECT 1 FROM files WHERE project_id = %s)",
                (stored_id, stored_id),
            )
        c.commit()
    log.info("ingest: re-keyed %d file(s) onto their folded marker project",
             moved)
    return moved
