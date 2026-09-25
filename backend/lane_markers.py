"""The stored lane project markers: what each sessions/<project>/project.json
said when a GET last read it, with the etag and marker reader version of
that read. A listed marker is fetched only when it has no row, or its row
holds another etag or reader version; every other marker's path comes
from its row.
"""
from __future__ import annotations

from backend import constants, db


def stored_markers() -> dict[str, tuple[str, str, str | None]]:
    """marker key -> (etag, reader version, path) for every stored marker."""
    with db.viz_conn() as c:
        return {
            key: (etag, version, path)
            for key, etag, version, path in c.execute(
                "SELECT marker_key, r2_etag, reader_version, path "
                "FROM lane_markers"
            ).fetchall()
        }


def cached_paths(marker_items: list[tuple[str, str, str]]
                 ) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Split the listed (project_id, key, etag) markers: project_id ->
    stored path for each whose row still matches, and the markers that
    need a GET because they have no row, or a row holding a different
    etag or reader version."""
    stored = stored_markers()
    paths: dict[str, str] = {}
    stale = []
    for project_id, key, etag in marker_items:
        row = stored.get(key)
        if row is None or row[:2] != (etag, constants.MARKER_READER_VERSION):
            stale.append((project_id, key, etag))
        elif row[2] is not None:
            paths[project_id] = row[2]
    return paths, stale


def save_markers(read: dict[str, tuple[str, str | None]],
                 listed: set[str]) -> None:
    """Store the markers this run read, under the current reader version,
    and drop the rows of keys the listing no longer shows. `read` maps
    key -> (etag, path) and holds only markers whose GET succeeded."""
    version = constants.MARKER_READER_VERSION
    with db.viz_conn() as c, c.cursor() as cur:
        cur.executemany(
            "INSERT INTO lane_markers "
            "(marker_key, r2_etag, reader_version, path) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (marker_key) DO UPDATE SET "
            "r2_etag = EXCLUDED.r2_etag, "
            "reader_version = EXCLUDED.reader_version, path = EXCLUDED.path",
            [(key, etag, version, path)
             for key, (etag, path) in read.items()],
        )
        cur.execute(
            "DELETE FROM lane_markers WHERE marker_key != ALL(%s)",
            (list(listed),),
        )
        c.commit()
