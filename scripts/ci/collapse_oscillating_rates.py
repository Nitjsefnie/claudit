#!/usr/bin/env python3
"""Collapse every oscillating provider-rate row into its band, once.

An OpenRouter host whose listed price moves inside a range and returns to
levels it has held before is dynamic, not repriced: the hourly refresh
appended every move, so master gained a commit and a PRICING_VERSION bump
nearly every hour. This is the one-time, human-reviewed whole-history pass
that gives each such row the band the hourly run then maintains (issues
#640, #663; SV-RATE-REFRESH, SV-RATE-DATA): the row keeps its own start,
its five rate fields become the time-weighted mean over the window that
classified it, and the band spans the window's levels — an old level far
from the oscillation neither widens the band nor pulls the mean. STEP and
STABLE rows are left exactly as they are. The hourly run forms bands
itself (issue #664), so no rerun is needed to stop the churn going
forward.

No row is rewritten here that the classifier does not call TOGGLE or BAND,
and the report names every host it collapsed, because a host whose prices
are now a mean must have priced no record: run the safety query over
exactly those names before merging

    SELECT DISTINCT provider FROM records WHERE provider IN (<those names>);

which must return no rows. As at backfill_provider_rates.py, the file is
validated through the loaders' own rules before either file is written,
and a row the collapse refuses (a schedule or changing web-search rate)
is reported and leaves the run red rather than silently rewritten.

    python3 scripts/ci/collapse_oscillating_rates.py [--days N] [--as-of T]
        [--dry-run]
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# pylint: disable=wrong-import-position
sys.path.insert(0, str(Path(__file__).resolve().parent))
import price_band  # noqa: E402
from refresh_common import bump_pricing_version  # noqa: E402
from backend import pricing  # noqa: E402

PRICING_JSON = REPO_ROOT / "src" / "pricing.json"
CONSTANTS_PY = REPO_ROOT / "backend" / "constants.py"
# The collapse's default window is the band machinery's own.
DEFAULT_DAYS = price_band.WINDOW_DAYS
_AS_OF = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def _as_of(value: str) -> str:
    if not _AS_OF.fullmatch(value):
        raise argparse.ArgumentTypeError("must be YYYY-MM-DDTHH:MM:SSZ")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a valid UTC instant") from exc
    return value


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collapse oscillating provider-rate rows into their band.")
    parser.add_argument("--days", type=float, default=DEFAULT_DAYS,
                        help="the classification window in days (default 7)")
    parser.add_argument("--as-of", type=_as_of, default=None,
                        help="the instant the rows are read at, and left as "
                             "provider_rates_fetched (default: the one the "
                             "file already carries)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the collapse without writing either file")
    return parser.parse_args(argv)


def _span(band: dict) -> str:
    """A band as one readable line: the ranges the host actually moved across."""
    return ", ".join(f"{field} {low:g}..{high:g}" for field, (low, high)
                     in sorted(band.items()))


def collapse(doc: dict, days: float, as_of: str) -> tuple[dict, list[str], set[str], bool]:
    """The file with every oscillating row collapsed, its report lines, the
    distinct host names collapsed, and whether anything moved.

    `as_of` is the instant the window is measured against and the one left
    in provider_rates_fetched; it is never wall clock.
    """
    result = copy.deepcopy(doc)
    at = pricing._instant(as_of, "--as-of")  # pylint: disable=protected-access
    reports: list[str] = []
    hosts: set[str] = set()
    for model, provider_hosts in doc["providers"].items():
        for host, history in provider_hosts.items():
            shape = price_band.classify(history, at, days)["shape"]
            if shape not in price_band.OSCILLATING:
                continue
            try:
                collapsed = price_band.collapse(history, at, days)
            except ValueError as exc:
                reports.append(f"  {model} via {host}: kept {len(history)} "
                               f"entries ({shape}); {exc}")
                continue
            result["providers"][model][host] = collapsed
            hosts.add(host)
            reports.append(f"  {model} via {host}: {len(history)} → 1 entries "
                           f"({shape}); band {_span(collapsed[0]['band'])}")
    if hosts:
        result["provider_rates_fetched"] = as_of
    # The loaders' own rules, run on what would be written.
    pricing.load_tables(result)
    return result, reports, hosts, bool(hosts)


def _render(as_of: str, days: float, reports: list[str], hosts: set[str]) -> str:
    lines = [f"OpenRouter provider rates collapsed to bands as of {as_of}",
             f"classification window {days:g} days"]
    if reports:
        lines += ["", *reports]
    if hosts:
        lines += ["", "collapsed hosts (the records-safety query takes exactly these):",
                  "  " + ", ".join(sorted(hosts))]
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, pricing_path: Path = PRICING_JSON,
         constants_path: Path = CONSTANTS_PY) -> int:
    args = _arguments(argv)
    try:
        doc = json.loads(pricing_path.read_text(encoding="utf-8"))
        as_of = args.as_of or doc["provider_rates_fetched"]
        result, reports, hosts, moved = collapse(doc, args.days, as_of)
        constants = constants_path.read_text(encoding="utf-8")
        if moved:
            constants = bump_pricing_version(constants)
    except (ValueError, OSError) as exc:
        print(f"collapse_oscillating_rates: {exc}", file=sys.stderr)
        return 1
    print(_render(as_of, args.days, reports, hosts))
    if moved and not args.dry_run:
        pricing_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                                encoding="utf-8")
        constants_path.write_text(constants, encoding="utf-8")
    # Every other row is written; the run is still red, so a human sees it.
    return 1 if any(" kept " in line for line in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
