"""Flag name <-> INT4 bit for ``records.turn_flags`` (issue #959, R5).

``backend/turn_flags.py`` is the vocabulary's source of truth — the
``FLAG_*`` constants it declares are the names a transcript window can
produce. This module owns the one thing that module must not: the bit
ASSIGNMENT stored in the database. The parser keeps emitting the sorted
name list (fixtures pin that record shape); ``ingest_persist`` converts
it with :func:`flags_to_mask` on the way into the INT4 column, and any
reader decodes with :func:`mask_to_flags`. ``tests/test_turn_flag_bits.py``
fails if a vocabulary flag has no bit or a bit has no flag.

Why a mask: one INT4 per row instead of a TEXT[] of names — the column
compacts like the rest of the row, and no query filters on it (the GIN
index went out with #958; there is no named query needing a replacement).

EXPANSION PATH (>31 flags): a signed INT4 carries bits 0..30, so
:data:`MAX_FLAGS` is 31 and the vocabulary has headroom below it. Adding
a flag means appending its bit HERE, in declaration order (bits already
stored must never move), and nothing else — the vocabulary test above
forces it. The 32nd flag cannot be represented: before the vocabulary
can grow past 31, ``records.turn_flags`` needs a widening migration
(widen the column to BIGINT and move the bit table with it, or replace
the column with a per-flag join table). :func:`flags_to_mask` refuses to
encode a mask the column could not hold, and :func:`mask_to_flags`
refuses to decode bits no flag claims, so neither direction degrades
silently if that migration is skipped.
"""
from __future__ import annotations

from collections.abc import Iterable

from backend import turn_flags

# Declaration order IS bit order: never reorder or renumber — bits are
# stored data. New flags append at the next free bit.
FLAG_BITS: dict[str, int] = {
    turn_flags.FLAG_STOP_HOOK_BLOCK: 1 << 0,
    turn_flags.FLAG_INTERRUPT: 1 << 1,
    turn_flags.FLAG_COMPACT: 1 << 2,
    turn_flags.FLAG_API_ERROR: 1 << 3,
    turn_flags.FLAG_MODEL_SWITCH: 1 << 4,
    turn_flags.FLAG_EFFORT_SWITCH: 1 << 5,
    turn_flags.FLAG_ADVISOR_SWITCH: 1 << 6,
    turn_flags.FLAG_VERSION_SWITCH: 1 << 7,
    turn_flags.FLAG_TOOLS_DELTA: 1 << 8,
    turn_flags.FLAG_IMAGE_RESULT: 1 << 9,
    turn_flags.FLAG_SLASH_COMMAND: 1 << 10,
    turn_flags.FLAG_USER_PROMPT: 1 << 11,
    turn_flags.FLAG_DATE_CHANGE: 1 << 12,
    turn_flags.FLAG_USER_REJECTED: 1 << 13,
    turn_flags.FLAG_CWD_SWITCH: 1 << 14,
    turn_flags.FLAG_AWAY_SUMMARY: 1 << 15,
    turn_flags.FLAG_TOOLS_CHANGE: 1 << 16,
    turn_flags.FLAG_SYSTEM_CHANGE: 1 << 17,
    turn_flags.FLAG_PROMPT_RERENDER: 1 << 18,
    turn_flags.FLAG_RESUME: 1 << 19,
    turn_flags.FLAG_CWD_REBUILD: 1 << 20,
}

# Bits 0..30 of a signed INT4; bit 30 is the last sign-safe one.
MAX_FLAGS = 31

_ALL_CLAIMED = 0
for _bit in FLAG_BITS.values():
    _ALL_CLAIMED |= _bit


def flags_to_mask(flags: Iterable[str] | None) -> int:
    """Encode the parser's flag-name list as the stored INT4 mask.

    An empty or absent window is 0. An unknown name raises: a vocabulary
    typo must fail the file's persist, not quietly drop a flag the
    reader would then never see.
    """
    mask = 0
    for flag in flags or ():
        bit = FLAG_BITS.get(flag)
        if bit is None:
            raise ValueError(f"unknown turn flag: {flag!r}")
        mask |= bit
    return mask


def mask_to_flags(mask: int) -> list[str]:
    """Decode a stored INT4 mask back to names, in bit (declaration) order.

    A bit no flag claims raises — a mask written by a newer build or a
    corrupted value must not decode to a plausible-looking subset.
    """
    if not isinstance(mask, int) or mask < 0:
        raise ValueError(f"not a turn_flags mask: {mask!r}")
    if mask & ~_ALL_CLAIMED:
        raise ValueError(
            f"turn_flags mask {mask:#x} carries bits no flag claims")
    return [flag for flag, bit in FLAG_BITS.items() if mask & bit]
