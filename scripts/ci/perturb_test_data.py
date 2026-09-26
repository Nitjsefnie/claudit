#!/usr/bin/env python3
"""Perturb the tree's test data (the second half of SV-TEST-DATA).

The perturbed-data CI leg runs the suite against a tree that simulates
what the automated refresh does: every rate row in src/pricing.json
gains THREE appended entries per run — the newest entry's five rates
scaled by ×2.0, by ×0.37, and by a per-row irregular factor in
[0.61, 1.47) derived from (run seed, row key). A zero rate becomes one
under every factor, so every field differs. The rows are processed in a
seeded-shuffled order and the irregular factor derives from
(run seed, row key); the run seed is `int(now.timestamp())`, or the
`--seed N` value. The seed drives the factors and the shuffle ONLY —
never a `from` stamp (issue #227): the appended entries take their
stamps from a global counter one second apart, based at the document's
newest real rate stamp, so every appended stamp is after every real one
whatever the seed — any seed is valid, which is what keeps the six
closed-window pins in tests/test_provider_pricing.py pricing their
pristine windows under every seed the leg can name. When NO entry in
the document carries a `from`, there is nothing older to protect and
the run instant is the base instead. The three version constants in
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

    python3 scripts/ci/perturb_test_data.py [--seed N] [--pricing PATH]
        [--constants PATH]
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


def perturb_pricing(path: Path,
                    seed: int | None = None) -> tuple[int, int, str]:
    """Append three scaled entries to every rate row; report the run.

    Each of the three scales the row's previous newest entry: by ×2.0,
    by ×0.37, and by a per-row irregular factor in [0.61, 1.47) derived
    from (run seed, row key). A zero rate becomes one under every
    factor, so every field differs whatever the row prices.

    The seed drives the factors and the row order ONLY — never a `from`
    stamp (issue #227). The appended stamps come from a global counter
    one second apart, based at the document's newest real rate stamp:
    it starts one second past that stamp and advances one second per
    appended entry, so every appended stamp is after every real one
    whatever the seed — per-row stamps stay strictly increasing, and a
    closed window keeps answering at every instant it answered before,
    which is what the six closed-window pins in
    tests/test_provider_pricing.py price. When NO entry carries a
    `from`, there is nothing older to protect and the run instant is
    the base instead. `_stamp_after`'s clamp stays as a belt for a real
    stamp newer than the counter (a refresh landing mid-run); its
    semantics are unchanged. The same (document, seed) reproduces the
    run byte for byte. The document is validated through the backend's
    own loader before it is written.

    Returns (row count, seed, stamp base) for the run report.
    """
    doc = json.loads(path.read_text(encoding="utf-8"))
    if seed is None:
        seed = int(datetime.now(timezone.utc).timestamp())
    all_rows = _rows(doc)
    if not all_rows:
        # A zero-row perturbation would let the leg pass constants-only,
        # pricing nothing — a partial-vacuous pass that proves nothing
        # about the rate half.
        raise ValueError(f"{path}: no rate rows under models/providers; "
                         "perturbing nothing would price nothing")
    base = _newest_stamp(all_rows)
    if base is None:
        base = datetime.now(timezone.utc).replace(microsecond=0)
    counter = base + timedelta(seconds=1)
    for row_key, entries in _shuffled(all_rows, seed):
        newest = entries[-1]
        previous_from = newest.get("from")
        for factor_text in (*FIXED_FACTORS, _irregular_factor(seed, row_key)):
            entry_from = _stamp_after(previous_from, candidate=counter)
            entries.append({
                "from": entry_from,
                **{field: _scaled(newest[field], float(factor_text))
                   for field in pricing.RATE_FIELDS},
                "note": f"{NOTE_PREFIX}{factor_text}{NOTE_SUFFIX}",
            })
            previous_from = entry_from
            counter += timedelta(seconds=1)
    pricing.load_tables(doc)
    path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return len(all_rows), seed, base.strftime(STAMP_FORMAT)


def _newest_stamp(rows: list[tuple[str, list]]) -> datetime | None:
    """The document's newest real `from`, parsed, over every entry of
    every row; None when no entry carries one."""
    stamps = [_parse_stamp(entry["from"])
              for _key, entries in rows for entry in entries
              if entry.get("from") is not None]
    return max(stamps) if stamps else None


def _parse_stamp(text: str) -> datetime:
    """A `from` stamp as a UTC instant."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


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
    seed — and the same row across runs — reproduce it exactly. A
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
    the row's predecessor — the belt for a real stamp newer than the
    counter, unchanged by the decoupling. The candidate is compared at
    second precision — the precision the stamp itself carries — so a
    microsecond-carrying counter never spells the predecessor's own
    second."""
    stamp = candidate.strftime(STAMP_FORMAT)
    if previous is not None:
        earlier = _parse_stamp(previous)
        truncated = _parse_stamp(stamp)
        if earlier >= truncated:
            return (earlier + timedelta(seconds=1)).strftime(STAMP_FORMAT)
    return stamp


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
                    "repository changed by design. The seed drives the row "
                    "order and the irregular factors only; the appended "
                    "`from` stamps derive from the document's own newest "
                    "real stamp, so any seed is valid (issue #227).")
    parser.add_argument("--seed", type=int, default=None,
                        help="the run seed, an absolute epoch-seconds int: "
                             "drives the row shuffle and the per-row "
                             "irregular factor, never a `from` stamp "
                             "(default: the wall clock)")
    parser.add_argument("--pricing", type=Path, default=PRICING_JSON,
                        help="path to the pricing document "
                             "(default: src/pricing.json)")
    parser.add_argument("--constants", type=Path, default=CONSTANTS_PY,
                        help="path to the constants module "
                             "(default: backend/constants.py)")
    args = parser.parse_args(argv)
    rows, seed, base_text = perturb_pricing(args.pricing, seed=args.seed)
    bump_constants(args.constants)
    print(f"perturbed {rows} rate rows (3 entries per row: ×2.0, ×0.37, "
          f"seeded per-row irregular), seed={seed}, stamp base={base_text}, "
          f"and bumped {len(VERSION_NAMES)} version constants")
    return 0


if __name__ == "__main__":
    sys.exit(main())
