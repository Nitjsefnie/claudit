#!/usr/bin/env python3
"""Perturb the tree's test data (the second half of SV-TEST-DATA).

The perturbed-data CI leg runs the suite against a tree that simulates
what the automated refresh does: every rate row in src/pricing.json
gains one appended entry — the newest entry's five rates, doubled (a
zero rate becomes one, so every field differs) — stamped `from` the
current UTC, and the three version constants in backend/constants.py
move up one. A test that pins repository-managed data then fails as a
test failure on this tree, never as a broken refresh. The CI leg calls
this script before pytest; the guard test
tests/test_no_pinned_version_literals.py is the other half.

The pricing document is rewritten in the exact layout
json.dumps(doc, indent=2, sort_keys=True) writes (SV-RATE-DATA's
canonical layout), and the perturbed document is validated through the
same loader the backend uses — pricing.load_tables refuses a broken
file before anything is written. Schedules, the openrouter section and
provider_rates_fetched are untouched; a second run appends a further
entry and moves the constants again.

    python3 scripts/ci/perturb_test_data.py [--pricing PATH] [--constants PATH]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# pylint: disable=wrong-import-position
from backend import pricing  # noqa: E402

PRICING_JSON = REPO_ROOT / "src" / "pricing.json"
CONSTANTS_PY = REPO_ROOT / "backend" / "constants.py"
VERSION_NAMES = ("PARSER_VERSION", "PRICING_VERSION", "MARKER_READER_VERSION")
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
NOTE = ("sv-test-data perturbation: rates doubled, "
        "a zero rate becomes one")


def perturb_pricing(path: Path, now: datetime | None = None) -> int:
    """Append one doubled entry to every rate row; return the row count.

    The new entry's `from` is the current UTC (strictly after a
    predecessor stamp equal to it, and one second past a newest stamp
    that is somehow already in the future), and its rates are the
    previous newest's, doubled — except a zero rate, which becomes one,
    so every field differs whatever the row prices. The document is
    validated through the backend's own loader before it is written.
    """
    doc = json.loads(path.read_text(encoding="utf-8"))
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    all_rows = list(_rows(doc))
    if not all_rows:
        # A zero-row perturbation would let the leg pass constants-only,
        # pricing nothing — a partial-vacuous pass that proves nothing
        # about the rate half.
        raise ValueError(f"{path}: no rate rows under models/providers; "
                         "perturbing nothing would price nothing")
    rows = 0
    for entries in all_rows:
        previous = entries[-1]
        entries.append({
            "from": _stamp_after(previous.get("from"), stamp),
            **{field: _double(previous[field]) for field in pricing.RATE_FIELDS},
            "note": NOTE,
        })
        rows += 1
    pricing.load_tables(doc)
    path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return rows


def _rows(doc: dict):
    """Every rate row's entry list: each model's, then each host's."""
    yield from doc["models"].values()
    for hosts in doc["providers"].values():
        yield from hosts.values()


def _double(rate):
    """The rate doubled; zero becomes one, so every field differs."""
    return rate + (rate or 1)


def _stamp_after(previous: str | None, now: datetime) -> str:
    """The perturbation's `from`: now, never at or before the predecessor."""
    if previous is not None:
        earlier = datetime.fromisoformat(previous.replace("Z", "+00:00"))
        if earlier >= now:
            return (earlier + timedelta(seconds=1)).strftime(STAMP_FORMAT)
    return now.strftime(STAMP_FORMAT)


def bump_constants(path: Path) -> None:
    """Move each version constant up one, in place, same quoted style."""
    text = path.read_text(encoding="utf-8")
    for name in VERSION_NAMES:
        pattern = re.compile(rf'(?m)^({name}) = "(\d+)"$')
        found = pattern.findall(text)
        if len(found) != 1:
            raise ValueError(f"{path}: expected exactly one {name} line, "
                             f"found {len(found)}")
        text = pattern.sub(rf'\1 = "{int(found[0][1]) + 1}"', text)
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """The CI leg's entry point: perturb both files, print what moved."""
    parser = argparse.ArgumentParser(
        prog="perturb_test_data",
        description="Perturb the tree's test data: append a doubled entry "
                    "to every rate row in src/pricing.json and bump the "
                    "three version constants, so the suite runs against "
                    "data the repository changed by design.")
    parser.add_argument("--pricing", type=Path, default=PRICING_JSON,
                        help="path to the pricing document "
                             "(default: src/pricing.json)")
    parser.add_argument("--constants", type=Path, default=CONSTANTS_PY,
                        help="path to the constants module "
                             "(default: backend/constants.py)")
    args = parser.parse_args(argv)
    rows = perturb_pricing(args.pricing)
    bump_constants(args.constants)
    print(f"perturbed {rows} rate rows and bumped "
          f"{len(VERSION_NAMES)} version constants")
    return 0


if __name__ == "__main__":
    sys.exit(main())
