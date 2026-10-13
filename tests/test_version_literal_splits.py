"""Pins for the version-literal guard's `_splits` candidate enumerator.

`_splits` runs once per string constant of every module the guard's
rate-call clause reaches, so its cost is paid per character of string
constants under tests/. These cases pin its exact output — the
candidates and their order — against a reference copy of the
per-character loop, and pin that its bytecode cost does not grow with
the length of a string that has no split point.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from backend import pricing
from scripts.ci.suite_phases import InstructionCounter
from tests.version_literal_guard import _splits


def _reference_splits(norm: str) -> Iterator[str]:
    """The per-character reference: `norm`, then `norm[:i]` for every
    index whose tail opens with ``[``/``@`` or is a snapshot suffix."""
    yield norm
    for i in range(1, len(norm)):
        rest = norm[i:]
        # pylint: disable-next=protected-access
        if rest[0] in "[@" or pricing._SNAPSHOT_SUFFIX.match(rest):
            yield norm[:i]


_PAD = "model-name-" * 2

_CASES = (
    "",
    "a",
    "[",
    "@",
    "[a",
    "@a",
    "a[",
    "a@",
    "a[@b",
    "a@[b",
    "a[[b",
    "a@@b",
    "[[",
    "@@@",
    "a[b[c@d",
    "x-12345",
    "x12345",
    "x-123456",
    "x123456",
    "x-12345678",
    "x12345678",
    "x-123456789",
    "x123456789",
    "x-12345678\n",
    "x12345678\n",
    "x-123456\n",
    "x-12345\n",
    "x-12345678\n\n",
    "x-\u0663\u0663\u0663\u0663\u0663\u0663",
    "x\u0663\u0663\u0663\u0663\u0663\u0663\u0663\u0663",
    "x-\u0663\u0663\u0663\u0663\u0663",
    "\u0663\u0663\u0663\u0663\u0663\u0663\u0663",
    _PAD + "-12345678",
    _PAD + "12345678\n",
    _PAD + "-12345678\n",
    _PAD + "-12345678" + "abc",
    _PAD + "-12345678" + "a" * 12,
    "-12345678",
    "12345678",
    "a@12345678",
    "a[-12345678",
    "a-1234[5678",
    "a@-1234567",
    "a-123[45678",
    "a[12345678\n",
    "vendor-model-4-8[1m]",
    "model-5@2026-07-31",
    "m@v-20260731",
    _PAD + "[" + _PAD + "@" + _PAD,
)


@pytest.mark.parametrize("norm", _CASES)
def test_splits_matches_the_per_character_reference(norm):
    assert list(_splits(norm)) == list(_reference_splits(norm))


def _instructions(norm: str) -> int:
    counter = InstructionCounter("splits-cost")
    assert counter.available, counter.reason
    try:
        with counter.window("run"):
            list(_splits(norm))
    finally:
        counter.close()
    return counter.total


def test_splits_cost_does_not_grow_with_a_split_free_string():
    # The per-character loop retires bytecodes per character, so 4,000
    # characters cost about 4x what 1,000 do; a scan that finds split
    # points in C and tries the snapshot suffix only over the last ten
    # positions costs the same at both lengths. 1.5x separates the two
    # with room either side.
    short = _instructions("a" * 1_000)
    long = _instructions("a" * 4_000)
    assert long < short * 1.5, (short, long)
