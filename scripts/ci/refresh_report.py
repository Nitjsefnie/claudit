#!/usr/bin/env python3
"""Format hourly OpenRouter provider-rate refresh reports and commit messages."""
from __future__ import annotations

import argparse
from decimal import Decimal
from typing import TYPE_CHECKING
from pathlib import Path

if TYPE_CHECKING:
    from refresh_provider_rates import Move, Result

_RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")


def _move_text(move: Move) -> str:
    new = move.new
    off = f" ({_discount_note(new.discount)})" if new.discount else ""
    windows = f", schedule of {len(new.schedule)} windows" if new.schedule else ""
    if move.source == "log" and move.entries_appended > 1:
        if move.old is None:
            return (f"  new       {move.host}: {move.entries_appended} log entries, "
                    f"newest fresh {new.rates['fresh']!r}{off}")
        return (f"  changed   {move.host}: {move.entries_appended} log entries, "
                f"newest fresh {move.old['fresh']!r} → {new.rates['fresh']!r}{off}")
    if move.old is None:
        rates = ", ".join(f"{field} {new.rates[field]!r}" for field in _RATE_FIELDS)
        return f"  new       {move.host}: {rates}{windows}{off}"
    moved = [f"{field} {move.old[field]!r} → {new.rates[field]!r}"
             for field in _RATE_FIELDS if move.old[field] != new.rates[field]]
    if move.old.get("schedule") != new.schedule:
        moved.append(f"schedule of {len(move.old.get('schedule') or [])} → "
                     f"{len(new.schedule or [])} windows")
    return f"  changed   {move.host}: {', '.join(moved)}{windows}{off}"


def _discount_note(discount: Decimal) -> str:
    return f"{format((discount * 100).normalize(), 'f')}% off"


def report(stamp: str, result: Result, tracked: dict) -> str:
    """Render moves, sampled coverage, vanished rows, and notices."""
    lines = [f"OpenRouter provider rates, detected {stamp}"]
    if not result.moves:
        lines.append("no rate moved")
    for model, source in tracked.items():
        section = [_move_text(move) for move in result.moves if move.model == model]
        section += [f"  vanished  {host} (row kept)"
                    for name, host in result.vanished if name == model]
        sampled = sorted(result.sampled.get(model, {}))
        if sampled:
            section.append(f"  sampled   {', '.join(sampled)}")
        if section:
            lines += ["", f"{model} ({source['id']})", *section]
    if result.refusals:
        lines += ["", "refused, rows left untouched:",
                  *(f"  {reason}" for reason in result.refusals)]
    if result.notices:
        lines += ["", "notices:", *(f"  {notice}" for notice in result.notices)]
    return "\n".join(lines)


def commit_message(result: Result, body: str) -> str:
    """Build the hourly pricing commit message around the report."""
    changed = sum(1 for move in result.moves if move.old is not None)
    added = len(result.moves) - changed
    counts = [f"{changed} changed" if changed else "",
              f"{added} new" if added else "",
              f"{len(result.vanished)} vanished" if result.vanished else "",
              f"{len(result.refusals)} refused" if result.refusals else ""]
    subject = "Refresh OpenRouter provider rates: " + ", ".join(count for count in counts if count)
    return f"{subject}\n\n{body}\n\nCaptured by .github/workflows/refresh-pricing.yml.\n"


def arguments(argv: list[str] | None) -> argparse.Namespace:
    """Parse the hourly refresh's command-line options."""
    parser = argparse.ArgumentParser(
        description="Append moved OpenRouter provider rates to src/pricing.json.")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be appended; write nothing")
    parser.add_argument("--commit-msg", type=Path,
                        help="write the commit message here when anything is appended")
    return parser.parse_args(argv)
