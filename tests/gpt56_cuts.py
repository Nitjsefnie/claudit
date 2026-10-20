"""Synthetic lane histories for dated-rate and transcript-cost boundary tests."""
from __future__ import annotations

from datetime import datetime

from tests.refresh_fixture_builders import seed_doc

JUL30_CUT = datetime.fromisoformat("2026-07-30T18:12:00+00:00")
AUG21_CUT = datetime.fromisoformat("2026-08-21T19:40:00+00:00")


def document() -> dict:
    histories = {}
    for key, cut, before, after in (
            ("gpt-acme-sol-9", AUG21_CUT, (5.0, 6.25, 0.5, 30.0), (4.0, 5.0, 0.4, 20.0)),
            ("gpt-acme-terra-9", JUL30_CUT, (2.5, 3.125, 0.25, 15.0), (2.0, 2.5, 0.2, 12.0)),
            ("gpt-acme-luna-9", JUL30_CUT, (1.0, 1.25, 0.1, 6.0), (0.2, 0.25, 0.02, 1.2))):
        histories[key] = []
        for stamp, (fresh, create, read, output) in ((None, before), (cut.isoformat(), after)):
            histories[key].append({"from": stamp, "fresh": fresh, "create_5m": create,
                                   "create_1h": create, "read": read, "output": output})
    return seed_doc(models=histories)
