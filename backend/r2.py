"""R2 (S3 API) client with a file:// mode for the local mirror.

When R2_ENDPOINT starts with 'file://', the client walks the local
directory tree at the path. Otherwise it uses boto3 against R2's
S3-compatible endpoint.

Several buckets per deploy: R2_BUCKET may name SEVERAL buckets joined by
'+' (e.g. 'claude', or 'claude+codex+kimi'); every stored file key is
qualified with its bucket as `<bucket>/<object-key>`. list_keys() yields
qualified keys, and get_object()/get_stream() split that first segment
back off (refusing a bucket not in buckets()); key_layout keeps working
on the object key without it.

API surface:
- buckets() -> configured bucket names
- split_key(key) -> (bucket, object-key)
- list_keys(prefix='') -> iterator of R2Object, `.key` bucket-qualified
- get_object(key) -> bytes
- get_stream(key) -> file-like (for line-streaming large transcripts)
"""
from __future__ import annotations

import hashlib
import lzma
import os
import re
import threading
from datetime import datetime, timezone
from typing import Iterator, NamedTuple
from urllib.parse import urlparse

import boto3
from botocore.config import Config

# S3 bucket-name grammar: 3-63 chars of lowercase letters, digits and
# hyphens, starting and ending letter/digit. R2 follows the same rules.
_BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_BUCKET_ENV = "R2_BUCKET"
_BUCKET_SEP = "+"
_DEFAULT_BUCKET = "claude"


class R2Object(NamedTuple):
    key: str
    etag: str
    size: int
    last_modified: datetime


def buckets() -> list[str]:
    """The configured bucket names, in R2_BUCKET order, deduped.

    R2_BUCKET carries one or more bucket names joined by '+' — 'claude',
    or 'claude+codex+kimi' to serve several buckets from one deploy.
    Whitespace around a name is stripped, and every name is validated
    against the S3 bucket-name grammar: an invalid one raises ValueError
    naming it. Lifespan calls this before serving (app.
    validate_bucket_config), so a bad R2_BUCKET aborts startup rather
    than half-serving.
    """
    raw = os.environ.get(_BUCKET_ENV) or _DEFAULT_BUCKET
    names: list[str] = []
    for piece in raw.split(_BUCKET_SEP):
        name = piece.strip()
        if not _BUCKET_NAME_RE.match(name):
            raise ValueError(
                f"R2_BUCKET entry {name!r} is not a valid S3 bucket "
                + "name (3-63 chars of a-z, 0-9 and '-')"
            )
        if name not in names:
            names.append(name)
    return names


def split_key(key: str) -> tuple[str, str]:
    """Split a stored file key `<bucket>/<object-key>` into its two parts.

    Stored file identity is bucket-qualified (several buckets per
    deploy). The bucket is a routing segment this module owns:
    key_layout.classify() and project_marker() keep taking the OBJECT key
    and must be handed `split_key(...)[1]`.
    """
    bucket, sep, object_key = key.partition("/")
    if not sep or not object_key:
        raise ValueError(f"file key has no bucket segment: {key!r}")
    return bucket, object_key


def public_key(key: str | None) -> str | None:
    """The PUBLIC form of a stored file key, for anything that leaves the
    server in a response body: the object key with the leading bucket
    segment removed. The bucket segment is infrastructure (SV-FILES-
    RECORDS) — it routes reads to the right bucket and never reaches a
    client, so a response cannot disclose which buckets a deploy reads.

    A key whose first segment is not a configured bucket — a legacy bare
    object key, or an already-public key — is returned unchanged, and so
    is a None/empty input: this is a presentation helper, total over
    everything a response might carry. Stored keys, DB rows and internal
    lookups stay bucket-qualified; call this only at the boundary where
    a file_key enters a response body.
    """
    if not key:
        return key
    bucket, sep, object_key = key.partition("/")
    if sep and bucket in buckets():
        return object_key
    return key


def redact(text: str | None) -> str | None:
    """Best-effort scrub of infrastructure names from free TEXT bound for
    a response body or the error column a public endpoint serves: the
    file-mode mirror root, and every configured bucket name in the forms
    it actually takes — a path segment (`/claude/`), a quoted !r value
    ('claude'), a list element after a separator (` claude/`), or a key
    prefix (claude/...). Unlike public_key this is a filter over free
    text, not a structural strip, so it is defence in depth behind
    keeping such strings out of response bodies by construction."""
    if not text:
        return text
    out = text
    file_mode, root = _is_file_mode()
    if file_mode and root:
        # Root arrives WITH its trailing slash (urlparse of file://host/path/),
        # so keep one slash after the placeholder; the stripped form covers
        # a mention without the trailing slash.
        out = out.replace(root, "<mirror>/")
        out = out.replace(root.rstrip("/"), "<mirror>")
    for bucket in buckets():
        out = out.replace(f"/{bucket}/", "/<bucket>/")
        out = out.replace(f" {bucket}/", " <bucket>/")
        out = out.replace(f"'{bucket}'", "'<bucket>'")
        if out.startswith(f"{bucket}/"):
            out = out.replace(f"{bucket}/", "<bucket>/", 1)
    return out


def _configured(key: str) -> tuple[str, str]:
    """split_key() plus the refusal: a bucket not named in R2_BUCKET is
    not ours to serve. Every read path goes through here."""
    bucket, object_key = split_key(key)
    if bucket not in buckets():
        raise ValueError(
            f"bucket {bucket!r} is not configured in R2_BUCKET"
        )
    return bucket, object_key


def _is_file_mode() -> tuple[bool, str]:
    """Return (in_file_mode, root_path). root_path is '' when not in file mode."""
    endpoint = os.environ.get("R2_ENDPOINT", "")
    if endpoint.startswith("file://"):
        parsed = urlparse(endpoint)
        path = parsed.path
        # urlparse keeps the leading slash of a proper Windows file URL
        # (file:///C:/mirror/ -> /C:/mirror/); ntpath would read that as
        # an unnamed drive's \C:\mirror. Strip it when a drive letter
        # follows; no POSIX path starts /X:/.
        if len(path) >= 3 and path[0] == "/" and path[2] == ":" \
                and path[1].isalpha():
            path = path[1:]
        return True, path
    return False, ""


def _safe_join(root: str, key: str) -> str:
    """Join root + key, refuse keys that escape the bucket root.

    Path-traversal defense: a malicious sidecar request like
    '?path=../../../etc/passwd' must not escape the mirror root.
    """
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, key))
    if not (full == base or full.startswith(base + os.sep)):
        raise PermissionError(f"key escapes bucket root: {key!r}")
    return full


def _scan_root(root: str, bucket: str, multi: bool) -> str | None:
    """File-mode bucket root: `<root>/<bucket>` when it exists, else root.

    The fallback to the endpoint root exists for a single-bucket deploy
    whose mirror predates per-bucket directories: with no <bucket>/
    directory under the root, EVERY top-level directory there counts as
    belonging to that one bucket (root holding alpha/ and beta/ with
    R2_BUCKET=claude stores keys as claude/alpha/... and claude/beta/...).
    With several buckets configured the fallback is OFF: a shared root
    cannot tell two buckets' objects apart — each would list (and each
    read would serve) the same tree. multi carries `len(buckets()) > 1`
    and is what makes this function return None: a configured bucket with
    no mirror directory, which the LISTING path must refuse (raise) just
    as the read path does, because a silently empty bucket would let the
    orphan sweep delete its whole history.

    The bucket segment is resolved against buckets() HERE, not taken from
    the caller: get_object/get_stream reach this with the first segment
    of a stored file_key, which a request can name, so the path below is
    built from the configured list element (an R2_BUCKET value) and a
    bucket outside the list raises the same ValueError _configured
    raises. The join still goes through _safe_join (realpath + containment
    check) like every other key-derived path here: list membership is not
    a path check, so the join itself refuses a segment that would escape
    the root.
    """
    names = buckets()
    if bucket not in names:
        raise ValueError(
            f"bucket {bucket!r} is not configured in R2_BUCKET"
        )
    # Rebind to the allow-list element: the path segment below is built
    # from the configured name, never from the caller-derived string.
    bucket = names[names.index(bucket)]
    candidate = _safe_join(root, bucket)
    if os.path.isdir(candidate):
        return candidate
    return None if multi else root


def _rethrow_walk_error(err: OSError) -> None:
    """Abort the listing on a failed subtree.

    os.walk's default onerror=None SWALLOWS the error, which turns an
    unreadable subtree into a silent PARTIAL listing — and the ingest
    orphan sweep deletes every row for keys the listing did not show,
    so a partial walk could sweep live history. Re-raising aborts the
    whole ingest run before the sweep, same as the missing-bucket
    refusal in _list_keys_file.
    """
    raise err


def _list_keys_file(root: str, bucket: str, prefix: str,
                    multi: bool) -> Iterator[R2Object]:
    scan_root = _scan_root(root, bucket, multi)
    if scan_root is None:
        # Only reachable in multi-bucket mode (single-bucket falls back
        # to the root above). Raise, never yield-empty: the ingest's
        # orphan sweep deletes every row for keys the listing did not
        # show, so a silently missing bucket directory would sweep that
        # bucket's entire history as "orphans". Same condition, same
        # spelling as the read path's refusal.
        raise FileNotFoundError(
            f"no mirror directory for configured bucket {bucket!r}; "
            "listing refused rather than report a possibly-partial walk"
        )
    if not os.path.isdir(scan_root):
        # Single-bucket mode can land here with the endpoint root itself
        # missing or not yet mounted: an empty listing would sweep the
        # whole bucket's history as "orphans", exactly like the missing
        # mirror directory above. Refuse the listing instead.
        raise FileNotFoundError(
            f"bucket root {scan_root!r} is not a directory; listing "
            "refused rather than report a possibly-partial walk"
        )
    prefix_path = _safe_join(scan_root, prefix) if prefix else scan_root
    if not os.path.isdir(prefix_path):
        return

    for dp, _dirs, fns in os.walk(prefix_path, followlinks=True,
                                  onerror=_rethrow_walk_error):
        for fn in fns:
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, scan_root).replace(os.sep, "/")
            try:
                st = os.stat(full)
            except FileNotFoundError:
                # Listed, then vanished mid-walk: legitimately gone, and
                # the sweep will see it gone too.
                continue
            except OSError as err:
                # A file the walk can list but not stat (a directory with
                # r but no x, a symlink whose target denies) must not
                # silently drop out of the listing — the orphan sweep
                # would delete the row of a file that is still there.
                raise OSError(
                    f"cannot stat {full!r} while listing bucket "
                    f"{bucket!r}"
                ) from err
            etag = hashlib.sha1(
                f"{int(st.st_mtime_ns)}:{st.st_size}".encode()
            ).hexdigest()
            yield R2Object(
                key=f"{bucket}/{rel}",
                etag=etag,
                size=st.st_size,
                last_modified=datetime.fromtimestamp(
                    st.st_mtime, tz=timezone.utc
                ),
            )


def _list_keys_s3(bucket: str, prefix: str) -> Iterator[R2Object]:
    s3 = _boto_client()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            yield R2Object(
                key=f"{bucket}/{o['Key']}",
                etag=str(o["ETag"]).strip('"'),
                size=int(o["Size"]),
                last_modified=o["LastModified"],
            )


def list_keys(prefix: str = "") -> Iterator[R2Object]:
    """List every configured bucket, keys qualified with their bucket.

    `prefix`, when given, applies to the QUALIFIED key and must name the
    bucket segment (e.g. 'claude/projA/'): a bucket the prefix does not
    name is not listed at all, so a shared-prefix listing stays exact
    rather than over-listing that bucket.
    """
    file_mode, root = _is_file_mode()
    names = buckets()
    multi = len(names) > 1
    for bucket in names:
        oprefix = ""
        if prefix:
            if not prefix.startswith(bucket + "/"):
                continue
            oprefix = prefix[len(bucket) + 1:]
        if file_mode:
            yield from _list_keys_file(root, bucket, oprefix, multi)
        else:
            yield from _list_keys_s3(bucket, oprefix)


def get_object(key: str) -> bytes:
    """Fetch one object by its stored, bucket-qualified key."""
    file_mode, root = _is_file_mode()
    bucket, object_key = _configured(key)
    if file_mode:
        scan_root = _scan_root(root, bucket, len(buckets()) > 1)
        if scan_root is None:
            raise FileNotFoundError(
                f"no mirror directory for bucket {bucket!r}"
            )
        full = _safe_join(scan_root, object_key)
        with open(full, "rb") as f:
            data = f.read()
    else:
        s3 = _boto_client()
        data = s3.get_object(Bucket=bucket, Key=object_key)["Body"].read()
    # Bucket objects may be stored per-object xz-compressed (`*.jsonl.xz`).
    # Inflate transparently so callers (ingest, transcript serving) always
    # see the plain JSONL bytes. xz is stdlib (`lzma`) — no extra dependency.
    if key.endswith(".xz"):
        data = lzma.decompress(data)
    return data


def get_stream(key: str):
    """Open a streaming reader. Caller is responsible for closing it.

    For `*.jsonl.xz` keys the returned stream transparently inflates xz on
    read (stdlib `lzma`), so callers line-iterate plain JSONL either way.
    """
    file_mode, root = _is_file_mode()
    bucket, object_key = _configured(key)
    if file_mode:
        scan_root = _scan_root(root, bucket, len(buckets()) > 1)
        if scan_root is None:
            raise FileNotFoundError(
                f"no mirror directory for bucket {bucket!r}"
            )
        full = _safe_join(scan_root, object_key)
        if key.endswith(".xz"):
            return lzma.LZMAFile(full)
        # Ownership passes to the caller (see docstring).
        return open(full, "rb")
    s3 = _boto_client()
    raw = s3.get_object(Bucket=bucket, Key=object_key)["Body"]
    if key.endswith(".xz"):
        return lzma.LZMAFile(raw)
    return raw


_tls = threading.local()


def _boto_client():
    """Per-thread cached S3 client.

    Rebuilding this per object cost ~5ms of botocore setup each time AND
    threw away the underlying connection pool, so every GET paid a fresh
    TLS handshake. Caching per thread keeps keep-alive alive; it is
    thread-local rather than module-global because botocore clients are
    only documented thread-safe for method calls, and a private client per
    worker also gives each its own connection pool.
    """
    client = getattr(_tls, "client", None)
    if client is not None:
        return client

    endpoint = os.environ["R2_ENDPOINT"]
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(max_pool_connections=32, retries={"max_attempts": 3}),
    )
    _tls.client = client
    return client
