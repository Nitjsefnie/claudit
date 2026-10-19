#!/usr/bin/env python3
"""Perturb the tree's test data (the second half of SV-TEST-DATA).

The perturbed-data CI leg runs the suite against a tree that simulates
what the automated refresh does: every rate row in src/pricing.json
gains FIVE appended entries per run — the predecessor's five rates
scaled whole by ×2.0, by ×0.37, and by a per-row irregular factor in
[0.61, 1.47) derived from (run seed, row key); then each field scaled
by its OWN seeded factor, derived from (run seed, row key, field), in
the same range; then a single-field move, exactly one seeded field
scaled by its own seeded factor and the other four copying the
per-field entry — so any within-row ratio the data once had (read ==
fresh * k, say) can flip. A zero rate becomes one on its first move.
Each append derives from its predecessor, with bounded seeded retries
and a minimal nonzero-field nudge if rounding would leave it unchanged.
Every PROVIDER row's appended entries also carry the optional
`web_search` rate (SV-RATE-REFRESH carries it forward). The first four
moves scale their predecessor's search rate by its own seeded factor;
the single-field move copies it,
so the leg exercises the shape a refresh appends; a
models-table row never carries it, the loader refusing the field
there. The rows are processed in a seeded-shuffled
order; the run seed is `int(now.timestamp())`, or the `--seed N`
value. The seed drives the factors and the shuffle ONLY — never a
`from` stamp: the appended entries take their stamps from
a global counter one second apart, based at the document's newest real
rate stamp, so every appended stamp is after every real one whatever
the seed — any seed is valid, which is what keeps the six closed-window
pins in tests/test_provider_pricing.py pricing their pristine windows
under every seed the leg can name. When NO entry in the document
carries a `from`, there is nothing older to protect and the run
instant is the base instead. The three version constants —
PARSER_VERSION in backend/parser_version.py, PRICING_VERSION and
MARKER_READER_VERSION in backend/constants.py — move up one. A test that pins repository-managed
data then fails as a test failure on this tree, never as a broken
refresh. The CI leg calls this script before pytest; the guard test
tests/test_no_pinned_version_literals.py is the other half.

The pricing document is rewritten through the shared compact serializer
(SV-RATE-DATA's canonical layout), and the serialized document is
validated through the same loader the backend uses — pricing.load_tables
refuses a broken file before anything is written. Schedules and the
openrouter section are untouched; provider_rates_fetched moves only as
far as the appended stamps. A second run appends a further
five entries per row and moves the constants again.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# pylint: disable=wrong-import-position
from backend import pricing  # noqa: E402
from backend.pricing_document import effective_rates, serialize_pricing_doc  # noqa: E402
from backend.pricing_rows import decode_pricing_doc, flatten_provider_history  # noqa: E402

PRICING_JSON = REPO_ROOT / "src" / "pricing.json"
CONSTANTS_PY = REPO_ROOT / "backend" / "constants.py"
PARSER_VERSION_PY = REPO_ROOT / "backend" / "parser_version.py"
VERSION_NAMES = ("PARSER_VERSION", "PRICING_VERSION", "MARKER_READER_VERSION")
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
NOTE_PREFIX = "sv-test-data perturbation: rates ×"
NOTE_SUFFIX = " (a zero rate becomes one)"
PER_FIELD_NOTE = "sv-test-data perturbation: independent per-field factors"
SINGLE_FIELD_NOTE_PREFIX = "sv-test-data perturbation: single-field move ("
SINGLE_FIELD_NOTE_SUFFIX = ")"
FIXED_FACTORS = ("2.0", "0.37")


def perturb_pricing(path: Path,
                    seed: int | None = None) -> tuple[int, int, str]:
    """Append five scaled entries to every rate row; report the run.

    Each append scales its predecessor. The first three scale whole: by
    ×2.0, by ×0.37, and by a per-row irregular factor in [0.61, 1.47)
    derived from (run seed, row key). The fourth scales each field by
    its OWN seeded factor, derived from (run seed, row key, field), in
    the same range. The fifth moves exactly one seeded field — scaled
    by its own seeded factor — and copies the fourth's other four, so
    any within-row ratio the data once had can flip. A zero rate
    becomes one on its first move; every append changes billing state.

    The seed drives the factors and the row order ONLY — never a `from`
    stamp. The appended stamps come from a global counter
    one second apart, based at the document's newest real rate stamp:
    it starts one second past that stamp and advances one second per
    appended entry (five per row now), so every appended stamp is after
    every real one whatever the seed — per-row stamps stay strictly
    increasing, and a closed window keeps answering at every instant it
    answered before, which is what the six closed-window pins in
    tests/test_provider_pricing.py price. When NO entry carries a
    `from`, there is nothing older to protect and the run instant is
    the base instead. `_stamp_after`'s clamp stays as a retained belt,
    its semantics unchanged: the document is read once here, so no
    in-process stamp can be newer than the counter and the clamp
    cannot fire on this path. The same (document, seed) reproduces the
    run byte for byte. The document is validated through the backend's
    own loader before it is written.

    Returns (row count, seed, stamp base) for the run report.
    """
    doc = json.loads(path.read_text(encoding="utf-8"))
    if "_format" in doc:
        doc = decode_pricing_doc(doc)
    if seed is None:
        seed = int(datetime.now(timezone.utc).timestamp())
    all_rows = _rows(doc)
    if not all_rows:
        # A zero-row perturbation would let the leg pass constants-only,
        # pricing nothing — a partial-vacuous pass that proves nothing
        # about the rate half.
        raise ValueError(f"{path}: no rate rows under models/providers; "
                         "perturbing nothing would price nothing")
    base = _append_rows(doc, all_rows, seed)
    serialized = serialize_pricing_doc(doc)
    pricing.load_tables(json.loads(serialized))
    path.write_text(serialized, encoding="utf-8", newline="\n")
    return len(all_rows), seed, base.astimezone(timezone.utc).strftime(
        STAMP_FORMAT)


def _append_rows(doc: dict, all_rows: list[tuple[str, list, bool]],
                 seed: int) -> datetime:
    """Append the five seeded entries to every row, then move the fetch
    stamp to cover them (no `from` postdates the fetch
    stamp). Returns the run's stamp base."""
    base = _newest_stamp(all_rows)
    if base is None:
        base = datetime.now(timezone.utc).replace(microsecond=0)
    counter = base + timedelta(seconds=1)
    last_appended = None
    for row_key, entries, may_search in _shuffled(all_rows, seed):
        history, _ = flatten_provider_history(
            entries, row_key, lambda stamp, _: _parse_stamp(cast(str, stamp)))
        newest = history[-1]
        previous_from = newest.get("from")
        for note, rates in _appended_specs(seed, row_key, newest, may_search):
            entry_from = _stamp_after(previous_from, candidate=counter)
            entries.append({
                "from": entry_from,
                **rates,
                "note": note,
            })
            previous_from = entry_from
            if last_appended is None or _parse_stamp(entry_from) > _parse_stamp(last_appended):
                last_appended = entry_from
            counter += timedelta(seconds=1)
    _bump_fetch_stamp(doc, last_appended)
    return base


def _bump_fetch_stamp(doc: dict, last_appended: str | None) -> None:
    """Move provider_rates_fetched to the last appended stamp when it
    postdates it — the way a committing run does."""
    stamped = doc.get("provider_rates_fetched")
    if last_appended is not None and (
            stamped is None
            or _parse_stamp(last_appended) > _parse_stamp(stamped)):
        doc["provider_rates_fetched"] = last_appended


def _newest_stamp(rows: list[tuple[str, list, bool]]) -> datetime | None:
    """The document's newest real `from`, parsed, over every entry of
    every row; None when no entry carries one."""
    stamps = [_parse_stamp(stamp)
              for _key, entries, _search in rows for entry in entries
              for stamp in (entry["from"] if isinstance(entry.get("from"), list)
                            else [entry.get("from")]) if stamp is not None]
    return max(stamps) if stamps else None


def _parse_stamp(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _rows(doc: dict) -> list[tuple[str, list, bool]]:
    """Every rate row as (row key, entry list, may_search): models
    first, then hosts.

    Only a PROVIDER row may carry the `web_search` rate — the loader
    refuses the field on a models-table row — so the flag decides which
    rows the perturbation gives it to.
    """
    rows = [(key, entries, False) for key, entries in doc["models"].items()]
    for model, hosts in doc["providers"].items():
        rows.extend((f"{model} via {host}", entries, True)
                    for host, entries in hosts.items())
    return rows


def _shuffled(rows: list[tuple[str, list, bool]], seed: int
              ) -> list[tuple[str, list, bool]]:
    order = list(rows)
    random.Random(seed).shuffle(order)
    return order


def _appended_specs(seed: int, row_key: str, newest: dict,
                    may_search: bool) -> list[tuple[str, dict]]:
    """Five genuine moves, each derived from its immediate predecessor."""
    specs = []
    previous = newest
    for index, factor in enumerate((*FIXED_FACTORS, _irregular_factor(seed, row_key),
                                    None, None)):
        if factor is not None:
            factors: dict[str, str] = dict.fromkeys(pricing.RATE_FIELDS, factor)
        elif index == 3:
            factors = {field: _field_factor(seed, row_key, field)
                       for field in pricing.RATE_FIELDS}
        else:
            moved = _moved_field(seed, row_key)
            factors = {moved: _single_field_factor(seed, row_key, moved)}
        if may_search and index < 4:
            factors["web_search"] = _search_factor(seed, row_key)
        rates, applied = _progressing_move(
            seed, f"{row_key}:{index}", previous, factors)
        token_factors = {field: value for field, value in applied.items()
                         if field in pricing.RATE_FIELDS}
        if len(token_factors) == 1:
            moved = next(iter(token_factors))
            note = f"{SINGLE_FIELD_NOTE_PREFIX}{moved}{SINGLE_FIELD_NOTE_SUFFIX}"
        elif index == 3:
            note = PER_FIELD_NOTE
        else:
            suffix = NOTE_SUFFIX if 0 in effective_rates(previous).values() else ""
            note = f"{NOTE_PREFIX}{token_factors['fresh']}{suffix}"
        specs.append((note, rates))
        previous = rates
    return specs


def _billing_state(entry: dict) -> tuple:
    return (effective_rates(entry), entry.get("web_search", 0.0),
            entry.get("schedule"))


def _progressing_move(seed: int, key: str, previous: dict,
                      factors: dict[str, str]) -> tuple[dict, dict[str, str]]:
    """Retry rounded no-ops eight times, then nudge one nonzero token rate."""
    source = effective_rates(previous)
    if "web_search" in previous or "web_search" in factors:
        source["web_search"] = previous.get("web_search", 0.0)
    independent = len({factor for field, factor in factors.items()
                       if field in pricing.RATE_FIELDS}) > 1
    for attempt in range(9):
        applied = (factors if attempt == 0 else {
            field: _bounded_factor(f"{seed}:{key}:retry:{attempt}"
                                   + (f":{field}" if independent else ""))
            for field in factors})
        rates = {**source, **{field: _scaled(source[field], float(factor))
                              for field, factor in applied.items()}}
        if _billing_state(rates) != _billing_state(previous):
            return rates, applied
    field = next(field for field in factors
                 if field in pricing.RATE_FIELDS and source[field])
    value = source[field]
    factor = math.nextafter(1.0, math.inf)
    if value * factor == value:
        factor = 1.0 + math.ulp(value) / (2 * value)
        if value * factor == value:
            factor = math.nextafter(factor, math.inf)
    return {**source, field: value * factor}, {field: repr(factor)}


def _bounded_factor(digest_input: str) -> str:
    """A factor in [0.61, 1.47), six decimals, never exactly 1.0, from
    a digest of the input. A factor of exactly 1.0 would change
    nothing, so it is excluded.
    """
    digest = hashlib.blake2b(digest_input.encode("utf-8"),
                             digest_size=8).digest()
    micros = 610_000 + int.from_bytes(digest, "big") % 860_000
    if micros == 1_000_000:
        micros += 1
    return f"{micros / 1_000_000:.6f}"


def _irregular_factor(seed: int, row_key: str) -> str:
    """A per-row factor in [0.61, 1.47), six decimals, never exactly 1.0.

    Deterministic in (seed, row key), so different runs of the same
    seed — and the same row across runs — reproduce it exactly.
    """
    return _bounded_factor(f"{seed}:{row_key}")


def _field_factor(seed: int, row_key: str, field: str) -> str:
    """A per-(row, field) factor in [0.61, 1.47), six decimals, never
    exactly 1.0: the per-field entry's own factor for one field.

    Deterministic in (seed, row key, field), like `_irregular_factor`
    is in its inputs.
    """
    return _bounded_factor(f"{seed}:{row_key}:{field}")


def _single_field_factor(seed: int, row_key: str, field: str) -> str:
    """A separately seeded factor applied to the per-field predecessor."""
    return _bounded_factor(f"{seed}:{row_key}:single:{field}")


def _search_factor(seed: int, row_key: str) -> str:
    """A per-row factor in [0.61, 1.47), six decimals, never exactly 1.0,
    for the appended `web_search` rate — keyed APART from every token
    field's factor, so the search rate moves off its own predecessor.
    """
    return _bounded_factor(f"{seed}:{row_key}:search")


def _moved_field(seed: int, row_key: str) -> str:
    """The one field a single-field entry moves, seeded in
    (seed, row key), so another seed can move another field.
    """
    digest = hashlib.blake2b(f"{seed}:{row_key}:single-field"
                             .encode("utf-8"), digest_size=8).digest()
    return pricing.RATE_FIELDS[int.from_bytes(digest, "big")
                               % len(pricing.RATE_FIELDS)]


def _scaled(rate, factor: float):
    """The rate scaled by the factor; zero becomes one, so it differs."""
    return rate * factor if rate else 1.0


def _stamp_after(previous: str | None, candidate: datetime) -> str:
    """An appended entry's `from`: the counter value, never at or before
    the row's predecessor — the belt for a real stamp newer than the
    counter. The candidate is normalised
    to UTC before formatting: strftime renders the
    datetime's own wall time, and a literal-Z format over an
    offset-carrying counter would write offset-local wall time as Z —
    hours early — so an offset-spelled predecessor could reject the
    document at the loader. The clamp's comparison and successor are
    normalised the same way, and the candidate is compared at second
    precision — the precision the stamp itself carries — so a
    microsecond-carrying counter never spells the predecessor's own
    second."""
    stamp = candidate.astimezone(timezone.utc).strftime(STAMP_FORMAT)
    if previous is not None:
        earlier = _parse_stamp(previous)
        truncated = _parse_stamp(stamp)
        if earlier >= truncated:
            return (earlier + timedelta(seconds=1)).astimezone(
                timezone.utc).strftime(STAMP_FORMAT)
    return stamp


def bump_constants(constants_path: Path, parser_version_path: Path) -> None:
    """Move each version constant up one, in place, same quoted style.

    PARSER_VERSION lives in its own module to avoid conflicts with the
    hourly pricing bot's PRICING_VERSION writes to backend/constants.py.
    The perturbation follows each constant to its home.
    """
    _bump_names(parser_version_path, ("PARSER_VERSION",))
    _bump_names(constants_path, ("PRICING_VERSION", "MARKER_READER_VERSION"))


def _bump_names(path: Path, names: tuple[str, ...]) -> None:
    text = path.read_text(encoding="utf-8")
    for name in names:
        pattern = re.compile(rf'(?m)^({name}) = "(\d+)"$')
        found = pattern.findall(text)
        if len(found) != 1:
            raise ValueError(f"{path}: expected exactly one {name} line, "
                             f"found {len(found)}")
        text = pattern.sub(rf'\1 = "{int(found[0][1]) + 1}"', text)
    path.write_text(text, encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="perturb_test_data",
        description="Perturb the tree's test data: append five entries to "
                    "every rate row in src/pricing.json — ×2.0, ×0.37, a "
                    "per-row irregular factor in [0.61, 1.47), five "
                    "independent per-field factors, and a single-field move "
                    "(a zero rate becomes one under every factor), plus the "
                    "optional web_search rate on every provider row — and "
                    "bump the three version constants, so the suite runs "
                    "against data the repository changed by design. The "
                    "seed drives the row order and the factors only; the "
                    "appended `from` stamps derive from the document's own "
                    "newest real stamp, so any seed is valid (issue #227).")
    parser.add_argument("--seed", type=int, default=None,
                        help="the run seed, an absolute epoch-seconds int: "
                             "drives the row shuffle, the per-row irregular "
                             "factor, the per-field factors and the "
                             "single-field choice, never a `from` stamp "
                             "(default: the wall clock)")
    parser.add_argument("--pricing", type=Path, default=PRICING_JSON,
                        help="path to the pricing document "
                             "(default: src/pricing.json)")
    parser.add_argument("--constants", type=Path, default=CONSTANTS_PY,
                        help="path to the constants module "
                             "(default: backend/constants.py)")
    parser.add_argument("--parser-version", type=Path,
                        default=PARSER_VERSION_PY,
                        help="path to the parser-version module "
                             "(default: backend/parser_version.py)")
    args = parser.parse_args(argv)
    rows, seed, base_text = perturb_pricing(args.pricing, seed=args.seed)
    bump_constants(args.constants, args.parser_version)
    print(f"perturbed {rows} rate rows (5 entries per row: ×2.0, ×0.37, "
          f"seeded per-row irregular, independent per-field factors, "
          f"single-field moves, web_search on provider rows), "
          f"seed={seed}, stamp base={base_text}, "
          f"and bumped {len(VERSION_NAMES)} version constants")
    return 0


if __name__ == "__main__":
    sys.exit(main())
