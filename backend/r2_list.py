"""Concurrent S3 bucket and first-level prefix listings, gathered before delivery."""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from backend.ingest_resolve import resolve_futures

LIST_WORKERS = 8  # Eight independent streams overlap I/O within the client's 32-connection limit.
ListedObject = tuple[str, str, int, datetime]


def _list_stream(client_factory: Callable[[], Any], bucket: str,
                 prefix: str, root: bool) -> tuple[list[ListedObject], list[str]]:
    paginator = client_factory().get_paginator("list_objects_v2")
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    if root:
        kwargs["Delimiter"] = "/"
    objects: list[ListedObject] = []
    prefixes: list[str] = []
    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []):
            objects.append((f"{bucket}/{obj['Key']}", str(obj["ETag"]).strip('"'),
                            int(obj["Size"]), obj["LastModified"]))
        prefixes.extend(p["Prefix"] for p in page.get("CommonPrefixes", []))
    return objects, prefixes


def list_keys(names: list[str], client_factory: Callable[[], Any]) -> list[ListedObject]:
    objects: list[tuple[int, ListedObject]] = []
    with ThreadPoolExecutor(max_workers=LIST_WORKERS) as pool:
        futures = {pool.submit(_list_stream, client_factory, bucket, "", True): (i, bucket)
                   for i, bucket in enumerate(names)}
        while futures:
            following = {}
            for (i, bucket), result, exc in resolve_futures(futures):
                if exc is not None:
                    raise exc
                rows, prefixes = result
                objects.extend((i, row) for row in rows)
                for prefix in prefixes:
                    future = pool.submit(_list_stream, client_factory, bucket, prefix, False)
                    following[future] = (i, bucket)
            futures = following
    # Restore serial bucket/key order only after every listing unit succeeds.
    objects.sort(key=lambda item: (item[0], item[1][0]))
    return [row for _, row in objects]
