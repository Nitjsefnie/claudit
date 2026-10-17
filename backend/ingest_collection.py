"""Stored parse policy and listing decisions, independent of worker environment."""
from __future__ import annotations

from collections.abc import Callable

from backend import db, key_layout, r2
from backend.flags import CollectionPolicy
from backend.ingest_walk import _stored_version_is_newer


def existing_files() -> dict:
    with db.viz_conn() as conn:
        return {
            key: (etag, version, tuple(stamp or ()))
            for key, etag, version, stamp in conn.execute(
                'SELECT file_key, r2_etag, parser_version, collection_flags '
                'FROM files').fetchall()
        }


def needs_reparse(stored: tuple | None, etag: str, parser_version: str,
                  collection: CollectionPolicy) -> bool:
    """Missing/changed source, code or policy queues, except newer stored code."""
    if _stored_version_is_newer(stored, parser_version):
        return False
    return (stored is None or stored[0] != etag or stored[1] != parser_version
            or tuple(stored[2] or ()) != collection.stamp())


def collect_wires(wires: list, existing: dict, parser_version: str,
                  collection: CollectionPolicy, track: Callable) -> tuple:
    listed = newer = 0
    seen_keys: set[str] = set()
    seen_projects: dict[str, dict] = {}
    todo: list[tuple] = []
    for obj in wires:
        info = key_layout.classify(r2.split_key(obj.key)[1])
        if info is None:  # pragma: no cover - scan only supplies transcripts
            continue
        listed += 1
        seen_keys.add(obj.key)
        tracked = track(seen_projects, info, obj)
        stored = existing.get(obj.key)
        if _stored_version_is_newer(stored, parser_version):
            newer += 1
        elif needs_reparse(stored, obj.etag, parser_version, collection):
            todo.append((obj, tracked, stored))
    return listed, todo, seen_keys, newer
