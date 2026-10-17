"""Lossless records.uuid storage split.

Canonical lowercase UUIDs use uuid_col; canonical UUID:INT4 counters use uuid_col
plus seq (no leading zeros). Every other spelling stays verbatim in fallback;
NULL stays NULL in all three (SV-CANONICAL-FLAG). uuid_text_sql rebuilds the
original text; a stored GENERATED text would undo the heap saving.

Classification is deterministic and schema_dims.sql CHECKs keep the slots'
text sets disjoint, so grouping/joining on ALL parts equals grouping on text.
uuid_col alone merges U with U:n and must never define the partition.
"""
from __future__ import annotations

import re
from typing import LiteralString, NamedTuple, cast

# The INT4 ceiling for the seq slot: a larger counter stores verbatim in
# the fallback rather than narrowing or failing (tested both ways).
INT4_MAX = 2_147_483_647

# Lowercase canonical 8-4-4-4-12 — the exact text PostgreSQL renders for
# uuid::text, so a match round-trips byte-identically through the uuid
# column. Uppercase, unhyphenated and braced forms all CAST but re-render
# differently, so they are not this shape and fall through to fallback.
# Both patterns anchor with `\Z`, NOT `$`: Python's `$` also matches
# BEFORE a trailing newline, which would let `"U\n"` into the uuid slot
# (its uuid.UUID() parse then crashes the file's persist) and `"U:5\n"` into the seq slot (reconstruct would
# drop the newline, breaking byte identity and merging with a genuine
# `"U:5"` in the winner rule). `\Z` is end-of-string, no exceptions.
# The SQL mirrors of these patterns are schema_dims.sql's third CHECK;
# PostgreSQL's POSIX `$` there does not match before a trailing newline
# (verified: `'U\n' ~ '^U$'` is false), and the fallback-accepts-newline
# shapes are pinned by the CHECK test either way.
_CANONICAL_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")

# A canonical uuid plus a decimal counter: the Codex lane's identity.
_SPLIT_RE = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{12}):([0-9]+)\Z")


class UuidParts(NamedTuple):
    """The three storage slots for one identity. Exactly one store is
    active: uuid_col (seq optional), fallback, or none at all."""

    uuid_col: str | None
    seq: int | None
    fallback: str | None


def split_uuid(value: str | None) -> UuidParts:
    """Classify one identity text into its slot — total, deterministic.

    Every input has exactly one destination and :func:`reconstruct`
    returns it byte-identically; the classification happens at persist
    (ingest_persist), after ``_strip_nul`` has cleaned the parse output,
    so the parser's own record shape is untouched.
    """
    if value is None:
        return UuidParts(None, None, None)
    if _CANONICAL_RE.match(value):
        return UuidParts(value, None, None)
    match = _SPLIT_RE.match(value)
    if match:
        counter = match.group(2)
        number = int(counter)
        # str() round-trip rejects leading zeros (':007' is fallback
        # text, not the sequence 7); the range check keeps seq INT4.
        if str(number) == counter and number <= INT4_MAX:
            return UuidParts(match.group(1), number, None)
    return UuidParts(None, None, value)


def reconstruct(parts: UuidParts) -> str | None:
    """The Python mirror of :func:`uuid_text_sql`.

    Only tests and reports call it — the database rebuilds text in SQL —
    and a test pins the two against each other so the mirrors cannot
    drift.
    """
    if parts.fallback is not None:
        return parts.fallback
    if parts.uuid_col is None:
        return None
    if parts.seq is None:
        return parts.uuid_col
    return f"{parts.uuid_col}:{parts.seq}"


# The four builders return LiteralString for the same reason
# db.sql_text does: psycopg types Cursor.execute() as taking a
# LiteralString (3.3's QueryNoTemplate), so a fragment typed as a bare
# str fails pyright the moment it is interpolated into a query. The
# alias interpolation is OUR OWN literal at every call site — never user
# input — so the cast states the guarantee the type wants.


def uuid_text_sql(alias: str = "r") -> LiteralString:
    """Rebuild original identity text byte-identically, including NULL.

    For a pure uuid, NULL concatenation would swallow uuid_col::text;
    COALESCE removes only the absent counter separator. All-NULL slots
    rebuild to NULL, preserving uuid IS NULL semantics."""
    return cast(LiteralString,
                f"COALESCE({alias}.fallback, {alias}.uuid_col::text || "
                f"COALESCE(':' || {alias}.seq::text, ''))")


def uuid_present_sql(alias: str = "r") -> LiteralString:
    """``records.uuid IS NOT NULL`` over the split: parenthesized, so it
    can be dropped into a larger boolean expression safely."""
    return cast(LiteralString,
                f"({alias}.uuid_col IS NOT NULL OR "
                f"{alias}.fallback IS NOT NULL)")


def uuid_absent_sql(alias: str = "r") -> LiteralString:
    """``records.uuid IS NULL`` over the split (a seq without a uuid_col
    is refused by CHECK, so uuid_col alone answers for uuid-bearing)."""
    return cast(LiteralString,
                f"({alias}.uuid_col IS NULL AND "
                f"{alias}.fallback IS NULL)")


def uuid_partition_sql(alias: str = "r") -> LiteralString:
    """Group winner identities by split parts, equal iff text is equal.

    Schema CHECK disjointness preserves PARTITION BY uuid semantics while
    comparing 16-byte uuids instead of rebuilding text per row per pass."""
    return cast(LiteralString,
                f"({alias}.uuid_col, {alias}.seq, {alias}.fallback)")
