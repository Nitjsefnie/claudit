"""The stored lane project markers: what each sessions/<project>/project.json
said the last time a GET read it, keyed by the etag that GET saw.

Without it every run fetched every marker again, because the paths lived
only in the run's memory. ingest._resolve_project_paths asks
cached_paths which listed markers need a GET, and save_markers stores
what those GETs read.
"""
from __future__ import annotations

from backend import db


def stored_markers() -> dict[str, tuple[str, str | None]]:
    """marker key -> (etag, path) for every stored marker."""
    with db.viz_conn() as c:
        return {
            key: (etag, path)
            for key, etag, path in c.execute(
                "SELECT marker_key, r2_etag, path FROM lane_markers"
            ).fetchall()
        }


def cached_paths(marker_items: list[tuple[str, str, str]]
                 ) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Split the listed (project_id, key, etag) markers: project_id ->
    stored path for each whose stored etag still matches, and the markers
    that need a GET because they have no row or a different etag."""
    stored = stored_markers()
    paths: dict[str, str] = {}
    stale = []
    for project_id, key, etag in marker_items:
        row = stored.get(key)
        if row is None or row[0] != etag:
            stale.append((project_id, key, etag))
        elif row[1] is not None:
            paths[project_id] = row[1]
    return paths, stale


def save_markers(read: dict[str, tuple[str, str | None]],
                 listed: set[str]) -> None:
    """Store the markers this run read, and drop the rows of keys the
    listing no longer shows. `read` maps key -> (etag, path) and holds
    only markers whose GET succeeded."""
    with db.viz_conn() as c, c.cursor() as cur:
        cur.executemany(
            "INSERT INTO lane_markers (marker_key, r2_etag, path) "
            "VALUES (%s, %s, %s) ON CONFLICT (marker_key) DO UPDATE SET "
            "r2_etag = EXCLUDED.r2_etag, path = EXCLUDED.path",
            [(key, etag, path) for key, (etag, path) in read.items()],
        )
        cur.execute(
            "DELETE FROM lane_markers WHERE marker_key != ALL(%s)",
            (list(listed),),
        )
        c.commit()
