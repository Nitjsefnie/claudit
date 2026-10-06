"""The ingest's listing scan: wires, sidecars and lane markers.

Split out of backend.ingest for size: the lock-loss guard and the
shutdown-cancel classification add net lines there, and the module-size
baseline for ingest.py is exact. The scan is one listing pass over
`r2.list_keys()`: transcripts paired with their meta.json sidecars
(`_Wire`), and the lane markers collected for their own fetch round.

The marker GET resolves `ingest._fetch_with_retry` at call time, so the
monkeypatch seam the tests set on backend.ingest keeps reaching marker
fetches exactly as when the code lived there.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import NamedTuple

from backend import key_layout, lane_markers, r2
from backend.ingest_resolve import (
    VanishedObject, record_failure as _record_failure, resolve as _resolve,
)
from backend.ingest_timing import _timed_step


class _Wire(NamedTuple):
    """A listed transcript and its meta.json sidecar. `etag` joins the
    sidecar's to the transcript's: the sidecar can decide agent_type, so
    one landing after its transcript (the archiver uploads it second),
    changing or going away reparses the file, at no extra request. A main
    transcript has none, so /api/sessions still serves the object's own.
    """

    key: str
    etag: str
    size: int
    last_modified: datetime
    sidecar_key: str | None


def _marker_fetch(key: str) -> bytes:
    """The marker GET, through ingest's `_fetch_with_retry` seam.

    Resolved at call time via a late import: backend.ingest imports this
    module, so a module-level import would be a cycle, and the tests'
    `monkeypatch.setattr(ingest, "_fetch_with_retry", ...)` must keep
    reaching marker GETs.
    """
    from backend import ingest  # pylint: disable=import-outside-toplevel,cyclic-import
    # The seam is ingest's own name; the tests patch it there.
    return ingest._fetch_with_retry(key)  # pylint: disable=protected-access


def _fetch_marker(project_id: str, key: str) -> tuple[str, str] | None:
    """Fetch and parse a sessions/<project>/project.json marker.

    Returns (project_id, path) — the path the project's sessions were
    run from, which becomes the project's display_name. Runs on a pool
    thread (via _resolve) and touches no DB connection.

    The GET is retried and its failure PROPAGATES, so the caller books
    it as a per-object failure like any transcript fetch. Only decode
    and shape problems are swallowed here: a malformed marker means that
    project shows its id instead of its path, which is a degrade, not a
    failed fetch, and no retry would change it.
    """
    blob = _marker_fetch(key)
    try:
        data = json.loads(blob.decode("utf-8"))
        path = data.get("path")
        if isinstance(path, str) and path:
            return (project_id, path)
    except (ValueError, AttributeError):
        # ValueError covers UnicodeDecodeError and json.JSONDecodeError;
        # AttributeError covers a marker whose top level is not an object.
        pass
    return None


def _resolve_project_paths(marker_items: list[tuple[str, str, str]],
                           workers: int,
                           failed: list[tuple[str, str]]) -> dict[str, str]:
    """Resolve every listed marker's path before the todo loop starts:
    stored rows for unchanged etags, a GET on the pool for the rest.

    A marker GET is as droppable as a transcript GET, so its failures
    land in the same `failed` summary; a failed or vanished marker gives
    no path this run and is not stored, so the next run fetches it again.
    """
    project_paths, stale = lane_markers.cached_paths(marker_items)
    read: dict[str, tuple[str, str | None]] = {}
    for item, res, exc in _resolve(
        stale, lambda it: _fetch_marker(it[0], it[1]), workers
    ):
        if isinstance(exc, VanishedObject):
            continue
        if exc is not None:
            _record_failure(failed, item[1], exc)
            continue
        read[item[1]] = (item[2], res[1] if res is not None else None)
        if res is not None:
            project_paths[res[0]] = res[1]
    lane_markers.save_markers(read, {key for _, key, _ in marker_items})
    return project_paths


def _scan_objects() -> tuple[list[_Wire], list[tuple[str, str, str]]]:
    """One listing pass: transcripts, each paired with the meta.json
    sidecar listed beside it (_Wire), and lane marker items.

    Markers are fetched afterwards by _resolve_project_paths, sidecars by
    _fetch_and_parse; the keys the layout rules skip (non-wire files
    inside sessions/, non-jsonl keys outside) are dropped here.
    """
    wire_objs: list = []
    marker_items: list[tuple[str, str, str]] = []
    sidecars: dict[tuple[str, str | None], r2.R2Object] = {}
    with _timed_step("list"):
        for obj in r2.list_keys():
            bucket, object_key = r2.split_key(obj.key)
            marker_project = key_layout.project_marker(object_key)
            if marker_project is not None:
                marker_items.append((marker_project, obj.key, obj.etag))
            elif (stem := key_layout.sidecar_stem(object_key)) is not None:
                sidecars[(bucket, stem)] = obj
            elif key_layout.classify(object_key) is not None:
                wire_objs.append(obj)
        wires = []
        for obj in wire_objs:
            bucket, object_key = r2.split_key(obj.key)
            side = sidecars.get((bucket, key_layout.transcript_stem(object_key)))
            wires.append(_Wire(obj.key,
                               obj.etag if side is None else f"{obj.etag}+{side.etag}",
                               obj.size, obj.last_modified,
                               side.key if side else None))
    return wires, marker_items
