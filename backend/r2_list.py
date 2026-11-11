"""Concurrent S3 listing: each bucket as key ranges between split points."""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from backend.ingest_resolve import resolve_futures
from backend.r2_list_timing import ListTiming

LIST_WORKERS = 8  # Eight independent streams overlap I/O within the client's 32-connection limit.
ListedObject = tuple[str, str, int, datetime]


def _list_range(client_factory: Callable[[], Any], bucket: str, lower: str | None,
                upper: str | None, timer: ListTiming | None) -> list[ListedObject]:
    timed = timer.range(bucket, lower, upper) if timer is not None else None
    kwargs = {"Bucket": bucket}
    if lower is not None:
        kwargs["StartAfter"] = lower
    objects: list[ListedObject] = []
    pages = client_factory().get_paginator("list_objects_v2").paginate(**kwargs)
    for page in pages if timed is None else timed.pages(pages):
        contents = page.get("Contents", [])
        # R2 can return a key <= StartAfter: filter both bounds.
        objects.extend((f"{bucket}/{obj['Key']}", str(obj["ETag"]).strip('"'),
                        int(obj["Size"]), obj["LastModified"])
                       for obj in contents
                       if (lower is None or obj["Key"] > lower)
                       and (upper is None or obj["Key"] <= upper))
        if upper is not None and any(obj["Key"] > upper for obj in contents):
            break
    if timed is not None:
        timed.done(len(objects))
    return objects


def list_keys(names: list[str], client_factory: Callable[[], Any],
              split_points: dict[str, list[str]],
              timer: ListTiming | None = None) -> list[ListedObject]:
    objects: list[tuple[int, ListedObject]] = []
    with ThreadPoolExecutor(max_workers=LIST_WORKERS) as pool:
        futures = {}
        for i, bucket in enumerate(names):
            # str order is UTF-8 byte order.
            bounds: list[str | None] = [None, *sorted(set(split_points.get(bucket, ())))]
            for lower, upper in zip(bounds, [*bounds[1:], None]):
                futures[pool.submit(_list_range, client_factory, bucket, lower, upper, timer)] = i
        for i, rows, exc in resolve_futures(futures):
            if exc is not None:
                raise exc
            objects.extend((i, row) for row in rows)
    # Deliver only after every range succeeded.
    objects.sort(key=lambda item: (item[0], item[1][0]))
    if timer is not None:
        timer.done()
    return [row for _, row in objects]
