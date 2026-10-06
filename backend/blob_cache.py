"""The disk blob cache for unchanged R2 objects (issue #684).

A PARSER_VERSION bump changes the parser, not the objects, yet every
full reparse re-fetched the whole bucket one GET at a time per parse
child — and the GET round-trips were ~70% of a run's parse-process
time. Enabled per deploy by `R2_BLOB_CACHE` (a directory), the cache
lets a reparse read those bytes from the meter's own disk instead.
Entries are keyed by the bucket-qualified key and the listing etag —
the same identity the reparse decision already trusts (SV-FILES-
RECORDS) — and hold the object's RAW (compressed) bytes, so a hit
still pays the xz inflate the GET would have: the r2 inflate stamps
it, and the TIMING child stages stay truthful.

Every failure is best-effort: an OSError anywhere reads as a miss, a
failed store is logged and dropped, and prune() never raises. A cache
that cannot work must cost nothing beyond one hash per fetch.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile

log = logging.getLogger("claudit.blob_cache")

#: The cache directory; unset or blank disables the cache entirely.
_ENV = "R2_BLOB_CACHE"
#: Size cap prune() enforces; invalid values fall back to the default.
_CAP_ENV = "R2_BLOB_CACHE_MAX_BYTES"
#: Holds a bucket of the corpus's size twice over, so a full population
#: of a ~3 GB bucket never prunes.
DEFAULT_CAP_BYTES = 4 * 1024 ** 3


def enabled() -> bool:
    """Whether the deploy named a cache directory."""
    return bool(os.environ.get(_ENV, "").strip())


def _root() -> str:
    return os.environ[_ENV].strip()


def _entry_path(key: str, etag: str) -> str:
    """The entry file for (key, etag): a 2-hex fan-out under the root.

    The digest is the cache key, never the object key itself, so no
    request-shaped string ever becomes a path component here.
    """
    digest = hashlib.sha256(f"{key}\0{etag}".encode()).hexdigest()
    return os.path.join(_root(), digest[:2], digest)


def lookup(key: str, etag: str, size: int | None) -> bytes | None:
    """The cached raw bytes for (key, etag), or None.

    `size`, when given, is the listing's byte length: an entry whose
    length disagrees reads as a miss — the one torn-write shape an
    atomic replace cannot prevent is refused, never served.
    """
    if not enabled():
        return None
    try:
        with open(_entry_path(key, etag), "rb") as f:
            data = f.read()
    except (OSError, ValueError):
        return None
    if size is not None and len(data) != size:
        return None
    return data


def store(key: str, etag: str, data: bytes) -> bool:
    """Write one entry atomically (temp in the entry's fan-out dir,
    then replace), best-effort: False on any failure, never a raise.
    """
    if not enabled():
        return False
    try:
        path = _entry_path(key, etag)
        fanout = os.path.dirname(path)
        os.makedirs(fanout, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=fanout, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, ValueError) as exc:
        log.debug("blob cache: store of %s failed: %s", key, exc)
        return False
    return True


def prune(cap_bytes: int | None = None) -> int:
    """Drop the oldest entries (mtime) until the cache fits the cap.

    `cap_bytes` defaults to R2_BLOB_CACHE_MAX_BYTES, then to
    DEFAULT_CAP_BYTES. In-flight temps count as entries: a crashed
    writer's temp is the oldest thing in the cache, so it leaves
    first, and a live writer whose temp is taken has its store turn
    False — best-effort, exactly like every other cache failure.
    Returns the bytes removed; 0 when disabled or on any failure.
    """
    if not enabled():
        return 0
    if cap_bytes is None:
        cap_bytes = _cap()
    try:
        entries: list[tuple[float, int, str]] = []
        total = 0
        for dirpath, _dirs, files in os.walk(_root()):
            for name in files:
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path)
                except FileNotFoundError:
                    continue
                entries.append((st.st_mtime, st.st_size, path))
                total += st.st_size
        removed = 0
        if total > cap_bytes:
            entries.sort()
            for _mtime, size, path in entries:
                if total <= cap_bytes:
                    break
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    continue
                removed += size
                total -= size
        return removed
    except (OSError, ValueError):
        return 0


def _cap() -> int:
    try:
        return int(os.environ.get(_CAP_ENV, ""))
    except ValueError:
        return DEFAULT_CAP_BYTES
