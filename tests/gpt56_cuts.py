"""The GPT-5.6 repricing instants, derived from the loaded rate tables.

These lived in ``backend/pricing.py`` as import-time constants until the
suite-cost bench's document seam (issue #840): the assert behind them
refused every import whose loaded tables lacked the two rows — a live-data
dependency no prod module carries, and one the bench's bounded document
has no reason to satisfy. Only tests price around these instants, so the
instants live with the tests. They are still derived through the merged
dated-views accessor, so the boundary keeps moving with the table, and a
rowless key fails loudly here instead of mispricing through.
"""
from __future__ import annotations

from datetime import datetime

from backend import pricing


def _cut(key: str) -> datetime:
    windows = pricing._key_windows(key)  # pylint: disable=protected-access
    assert windows is not None, f"{key}: no committed history to cut from"
    return windows[0][0]


JUL30_CUT = _cut("gpt-5-6-terra")
AUG21_CUT = _cut("gpt-5-6-sol")
