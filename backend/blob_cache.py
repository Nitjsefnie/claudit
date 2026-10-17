"""Best-effort disk blob cache for unchanged R2 objects.

A parser change does not change objects: reuse their bytes across
reparses to avoid repeated GETs. CLAUDIT_R2_BLOB_CACHE selects the
directory. Bucket-qualified keys and listing etags define identity
(SV-FILES-RECORDS). Store raw compressed bytes so cache hits still pay
and stamp inflate work exactly as GETs do.

Read failures are misses; failed stores are logged and dropped; prune
never raises. An unusable cache must cost only one hash per fetch."""
from __future__ import annotations


import hashlib
import logging
import os
import tempfile

from backend.env_config import getenv, require_env

log = logging.getLogger("claudit.blob_cache")

#: The cache directory; unset or blank disables the cache entirely.
_ENV = "CLAUDIT_R2_BLOB_CACHE"
#: Size cap prune() enforces; invalid values fall back to the default.
_CAP_ENV = "CLAUDIT_R2_BLOB_CACHE_MAX_BYTES"
#: The cap holds a compressed bucket across reparses, avoiding prune
#: churn when the whole corpus fits.
DEFAULT_CAP_BYTES = 4 * 1024 ** 3


def enabled() -> bool:
    return bool(getenv(_ENV, "").strip())


def _root() -> str:
    return require_env(_ENV).strip()


def _log_safe(value: object) -> str:
    """`value` with control characters (C0, DEL, C1) dropped.

    A key can carry text from a user query param, so an
    unfiltered CR/LF in it would forge extra log lines.
    """
    return "".join(
        "" if ord(ch) < 0x20 or ord(ch) == 0x7F or 0x80 <= ord(ch) <= 0x9F
        else ch
        for ch in str(value))


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
    length disagrees reads as a miss. The atomic replace keeps a torn
    write off the entry path, so this refuses what else could land
    there — external corruption, a hand-moved file, a wrong length —
    never served as object bytes.
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
        log.debug("blob cache: store of %s failed: %s",
                  _log_safe(key), _log_safe(exc))
        return False
    return True


def prune(cap_bytes: int | None = None) -> int:
    """Drop the oldest entries (mtime) until the cache fits the cap.

    `cap_bytes` defaults to CLAUDIT_R2_BLOB_CACHE_MAX_BYTES, then to
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
        value = int(getenv(_CAP_ENV, ""))
    except ValueError:
        return DEFAULT_CAP_BYTES
    # A negative cap would wipe the cache every run; garbage or a
    # negative value falls back to the default rather than thrashing.
    return value if value >= 0 else DEFAULT_CAP_BYTES
