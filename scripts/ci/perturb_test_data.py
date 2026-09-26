#!/usr/bin/env python3
"""Perturb the tree's test data (the second half of SV-TEST-DATA).

The perturbed-data CI leg runs the suite against a tree that simulates
what the automated refresh does: every rate row in src/pricing.json
gains THREE appended entries per run — the newest entry's five rates
scaled by ×2.0, by ×0.37, and by a per-row irregular factor in
[0.61, 1.47) derived from (run seed, row key). A zero rate becomes one
under every factor, so every field differs. The rows are processed in a
seeded-shuffled order (the run seed is `int(now.timestamp())`, the same
seed the factors derive from), and appended entries take their `from`
stamps from a global counter one second apart, incremented per appended
entry, so per-row stamps stay strictly increasing while cross-row
interleaving is shuffled. The three version constants in
backend/constants.py move up one. A test that pins repository-managed
data then fails as a test failure on this tree, never as a broken
refresh. The CI leg calls this script before pytest; the guard test
tests/test_no_pinned_version_literals.py is the other half.

The pricing document is rewritten in the exact layout
json.dumps(doc, indent=2, sort_keys=True) writes (SV-RATE-DATA's
canonical layout), and the perturbed document is validated through the
same loader the backend uses — pricing.load_tables refuses a broken
file before anything is written. Schedules, the openrouter section and
provider_rates_fetched are untouched; a second run appends a further
three entries per row and moves the constants again.

    python3 scripts/ci/perturb_test_data.py [--pricing PATH] [--constants PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
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
NOTE_PREFIX = "sv-test-data perturbation: rates ×"
NOTE_SUFFIX = " (a zero rate becomes one)"
FIXED_FACTORS = ("2.0", "0.37")


def perturb_pricing(path: Path, now: datetime | None = None) -> int:
    """Append three scaled entries to every rate row; return the row count.

    Each of the three scales the row's previous newest entry: by ×2.0,
    by ×0.37, and by a per-row irregular factor in [0.61, 1.47) derived
    from (run seed, row key). A zero rate becomes one under every
    factor, so every field differs whatever the row prices. The run
    seed is `int(now.timestamp())`: it drives both the seeded-shuffled
    row order and the irregular factors, so the same `now` reproduces
    the run byte for byte. Appended entries take their `from` from a
    global counter one second apart — never at or before the row's
    previous stamp, and one second past a newest stamp that is somehow
    already in the future — so per-row stamps stay strictly increasing
    while cross-row interleaving is shuffled. The document is validated
    through the backend's own loader before it is written.
    """
    doc = json.loads(path.read_text(encoding="utf-8"))
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    seed = int(stamp.timestamp())
    all_rows = _rows(doc)
    if not all_rows:
        # A zero-row perturbation would let the leg pass constants-only,
        # pricing nothing — a partial-vacuous pass that proves nothing
        # about the rate half.
        raise ValueError(f"{path}: no rate rows under models/providers; "
                         "perturbing nothing would price nothing")
    counter = stamp
    for row_key, entries in _shuffled(all_rows, seed):
        base = entries[-1]
        previous_from = base.get("from")
        for factor_text in (*FIXED_FACTORS, _irregular_factor(seed, row_key)):
            entry_from = _stamp_after(previous_from, counter)
            entries.append({
                "from": entry_from,
                **{field: _scaled(base[field], float(factor_text))
                   for field in pricing.RATE_FIELDS},
                "note": f"{NOTE_PREFIX}{factor_text}{NOTE_SUFFIX}",
            })
            previous_from = entry_from
            counter += timedelta(seconds=1)
    pricing.load_tables(doc)
    path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return len(all_rows)


def _rows(doc: dict) -> list[tuple[str, list]]:
    """Every rate row as (row key, entry list): models first, then hosts."""
    rows = list(doc["models"].items())
    for model, hosts in doc["providers"].items():
        rows.extend((f"{model} via {host}", entries)
                    for host, entries in hosts.items())
    return rows


def _shuffled(rows: list[tuple[str, list]], seed: int) -> list[tuple[str, list]]:
    """The rows in a seeded-shuffled order (the factors' own seed)."""
    order = list(rows)
    random.Random(seed).shuffle(order)
    return order


def _irregular_factor(seed: int, row_key: str) -> str:
    """A per-row factor in [0.61, 1.47), six decimals, never exactly 1.0.

    Deterministic in (seed, row key), so different runs of the same
    `now` — and the same row across runs — reproduce it exactly. A
    factor of exactly 1.0 would change nothing, so it is excluded.
    """
    digest = hashlib.blake2b(f"{seed}:{row_key}".encode("utf-8"),
                             digest_size=8).digest()
    micros = 610_000 + int.from_bytes(digest, "big") % 860_000
    if micros == 1_000_000:
        micros += 1
    return f"{micros / 1_000_000:.6f}"


def _scaled(rate, factor: float):
    """The rate scaled by the factor; zero becomes one, so it differs."""
    return rate * factor if rate else 1.0


def _stamp_after(previous: str | None, candidate: datetime) -> str:
    """An appended entry's `from`: the counter value, never at or before
    the row's predecessor."""
    if previous is not None:
        earlier = datetime.fromisoformat(previous.replace("Z", "+00:00"))
        if earlier >= candidate:
            return (earlier + timedelta(seconds=1)).strftime(STAMP_FORMAT)
    return candidate.strftime(STAMP_FORMAT)


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
        description="Perturb the tree's test data: append three entries to "
                    "every rate row in src/pricing.json — ×2.0, ×0.37, and "
                    "a per-row irregular factor in [0.61, 1.47) (a zero rate "
                    "becomes one under every factor) — and bump the three "
                    "version constants, so the suite runs against data the "
                    "repository changed by design.")
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
