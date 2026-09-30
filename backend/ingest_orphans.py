"""Orphan sweeps: file rows whose R2 key is gone, and unreferenced projects."""
from __future__ import annotations

import logging

from backend import cache, db
from backend.ingest_progress import _set_progress
from backend.ingest_scope import capture_and_add, current_scope

log = logging.getLogger("claudit.ingest")


def _delete_orphans(seen_keys: set[str]) -> int:
    """Drop files rows whose R2 key is gone. CASCADE drops records.

    Each deleted row's cached transcript bytes are evicted after the
    delete commits (issue #269): the cache is keyed by
    cache.transcript_key(file_key, r2_etag) (issue #375), so a deleted
    transcript must not stay readable from the cache.
    """
    _set_progress(phase="orphans")
    with db.viz_conn() as c, c.cursor() as cur:
        scope = current_scope()
        if scope is not None and not scope.full:
            if seen_keys:
                doomed = {
                    row[0] for row in c.execute(
                        "SELECT file_key FROM files "
                        "WHERE file_key != ALL(%s)", (list(seen_keys),)
                    ).fetchall()
                }
            else:
                doomed = {
                    row[0] for row in c.execute(
                        "SELECT file_key FROM files").fetchall()
                }
            capture_and_add(scope, c, doomed)
            scope.check_latency_null()
        if seen_keys:
            cur.execute(
                "DELETE FROM files WHERE file_key != ALL(%s) "
                "RETURNING file_key, r2_etag",
                (list(seen_keys),),
            )
        else:
            cur.execute("DELETE FROM files RETURNING file_key, r2_etag")
        doomed_rows = cur.fetchall()
        deleted = len(doomed_rows)
        c.commit()
    for file_key, etag in doomed_rows:
        cache.transcript_cache.evict(cache.transcript_key(file_key, etag))
    evicted = len(doomed_rows)
    if evicted:
        log.info(
            "ingest: evicted %d orphaned transcript(s) from the cache",
            evicted)
    return deleted


def _delete_orphan_projects() -> int:
    """Drop project rows no file references any more.

    A project id that moved under its files leaves the old row behind
    with nothing pointing at it — the Windows case-fold re-keys every
    pre-53 mixed-case row onto the folded id, the lane slug rekey moves
    a hash's files onto its marker slug, and a wiped subtree cascades
    its files away. /api/projects already hides a usage-less project;
    deleting keeps the table itself honest instead of only the read.
    Runs every ingest, after the orphan-file sweep.
    """
    _set_progress(phase="orphan_projects")
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "DELETE FROM projects p WHERE NOT EXISTS "
            "(SELECT 1 FROM files f WHERE f.project_id = p.project_id) "
            "RETURNING 1",
        )
        deleted = len(cur.fetchall())
        c.commit()
    if deleted:
        log.info("ingest: dropped %d orphan project row(s)", deleted)
    return deleted
