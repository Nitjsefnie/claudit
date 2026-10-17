"""Stable INT4 flag bits: append in declaration order, never move stored bits.

turn_flags owns names; parsers retain name lists and storage encodes/decodes
masks. Signed INT4 permits bits 0..30: widen/migrate before a 32nd flag.
Reject out-of-range masks and unknown bits loudly."""
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
