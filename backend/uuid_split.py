"""records.uuid as a split column set (issue #959 task 8).

One identity text lands in exactly one of three storage slots on
``records``:

* ``uuid_col`` — a canonical lowercase uuid: that is exactly the form
  PostgreSQL's ``uuid::text`` renders, so the value round-trips
  byte-identically;
* ``seq`` — the INT4 counter stored beside ``uuid_col`` for the Codex
  lane's ``<uuid>:<n>`` identity, decimal with no leading zeros and at
  most 2^31-1;
* ``fallback`` — everything else, verbatim: kimi ``<file_key>:<line>``
  ids, legacy message ids, non-canonical uuid forms (uppercase,
  unhyphenated, braced) and counters that are not a canonical INT4
  decimal (``:007``, an overflowing ``:2147483648``).

Nothing is ever coerced: a value no slot claims still stores verbatim in
``fallback``, and a NULL identity stays NULL in all three — NULL uuid
means always-canonical in the winner rule, so a silent NULL would be a
silent dedup break (SV-CANONICAL-FLAG).

Readers rebuild the original text with :func:`uuid_text_sql` —
``COALESCE(fallback, uuid_col::text || COALESCE(':' || seq::text, ''))``
— so canonical grouping, winner parity and API output stay byte-identical
to the old TEXT column. A stored GENERATED column was rejected: it would
re-store the same ~40-byte text on every row and hand back the heap
saving the split exists to make.

Why readers may group by the PARTS instead of the rebuilt text
(:func:`uuid_partition_sql`): the classification is one total,
deterministic function of the text, and the CHECK constraints in
``schema_dims.sql`` enforce that the stores' text sets are pairwise
disjoint — ``uuid_col::text`` never contains a colon (PostgreSQL's
rendering), and a fallback never holds a form another slot owns. Equal
parts therefore mean equal texts and vice versa, so PARTITION BY and the
winner join on the parts pick exactly the winners the old TEXT column
did, without rebuilding 1.79M strings per pass. A partition on
``uuid_col`` ALONE would NOT: it merges a pure ``U`` row with its
``U:n`` split variants, which are different identities — pinned by
``tests/test_uuid_split.py::test_pure_and_split_rows_of_one_uuid_keep_
separate_winners``.
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
# (its uuid.UUID() parse then crashes the file's persist — the old TEXT
# column stored it) and `"U:5\n"` into the seq slot (reconstruct would
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
    """The byte-identical rebuild of the original identity text.

    ``uuid_col::text || ':' || seq`` would be NULL for a pure uuid (NULL
    concatenation), so the counter's separator is COALESCEd away first;
    a NULL identity (all three slots NULL) rebuilds to NULL, exactly as
    the old ``uuid IS NULL`` read did.
    """
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
    """The winner rule's grouping key: the parts, not the rebuilt text.

    Equal here if and only equal as text (see the module docstring), so
    this partitions exactly like the old ``PARTITION BY uuid`` — while
    comparing 16-byte uuids instead of building 40-byte strings for
    every row of every pass.
    """
    return cast(LiteralString,
                f"({alias}.uuid_col, {alias}.seq, {alias}.fallback)")
