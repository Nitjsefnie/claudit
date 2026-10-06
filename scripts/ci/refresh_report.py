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
_SAMPLE_REASONS = {
    "cheapest resolution has no stable endpoint identity": "cheapest resolution",
    "host endpoints use more than one tag prefix": "multiple provider tags",
    "tag prefix is shared by another host": "provider tag shared",
    "endpoint has a pricing schedule": "endpoint schedule",
    "series has a schedule": "log series schedule",
    "series count does not match endpoint count": "log series count mismatch",
    "series current rates do not identify exactly one endpoint":
        "multiple endpoints at one current price",
    "more than one series matches the same endpoint":
        "multiple log series match one endpoint",
    "one or more endpoints have no matching series": "log is missing endpoint history",
    "latest log state disagrees with the listed price": "log disagrees with listing",
    "the listing lags the price log": "listing lags log",
    "no in-force log state matches the listing": "no matching log state",
    "no series joined to the listed host": "no matching log series",
    "series has null input or output, or no complete state": "log has incomplete rates",
    "selected endpoint has no usable history": "no endpoint history",
    "stored schedule changed outside the price log": "stored schedule differs from log",
}


def _sampled_host(host: str, reason: str) -> str:
    if reason.startswith("listed-pricing log unavailable for "):
        reason = "log unavailable"
    elif reason.startswith("endpoint selection found "):
        count = reason.removeprefix("endpoint selection found ").split(" ", 1)[0]
        reason = "no endpoint selected" if count == "0" else f"{count} endpoints selected"
    else:
        reason = _SAMPLE_REASONS.get(reason, reason)
    return f"{host} ({reason})"


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
    return f"  changed   {move.host}: {', '.join(moved)}{off}"


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
        sampled = sorted(result.sampled.get(model, {}).items())
        if sampled:
            descriptions = [_sampled_host(host, reason) for host, reason in sampled]
            section.append(f"  sampled   {', '.join(descriptions)}")
        if section:
            lines += ["", f"{model} ({source['id']})", *section]
    if result.refusals:
        lines += ["", "refused, rows left untouched:",
                  *(f"  {reason}" for reason in result.refusals)]
    if result.notices:
        lines += ["", "notices:", *(f"  {notice}" for notice in result.notices)]
    return "\n".join(lines)


def commit_message(result: Result, body: str, vendor=None) -> str:
    """Build the hourly pricing commit message around the report."""
    changed = sum(1 for move in result.moves if move.old is not None)
    added = len(result.moves) - changed
    counts = [f"{changed} changed" if changed else "",
              f"{added} new" if added else "",
              f"{len(result.vanished)} vanished" if result.vanished else "",
              f"{len(result.refusals)} refused" if result.refusals else ""]
    subject = "Refresh OpenRouter provider rates: " + ", ".join(c for c in counts if c)
    if vendor is not None and vendor.moves:
        v_changed = sum(1 for m in vendor.moves if m.entries and m.old is not None)
        v_new = sum(1 for m in vendor.moves if m.entries and m.old is None)
        v_metered = len(vendor.moves) - v_changed - v_new
        segments = [f"{v_new} new" if v_new else "",
                    f"{v_changed} changed" if v_changed else "",
                    f"{v_metered} metered" if v_metered else ""]
        subject += "; vendor list rates: " + ", ".join(s for s in segments if s)
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


def vendor_report(stamp: str, vendor) -> str:
    """Render the vendor pass's section of the hourly report."""
    lines = [f"Vendor list rates, detected {stamp}"]
    if not (vendor.moves or vendor.refusals or vendor.notices):
        lines.append("no first-party rate moved")
    for move in vendor.moves:
        tail = (" [metered]" if move.membership == "+"
                else " [unmetered]" if move.membership == "-" else "")
        if move.entries and move.old is None:
            rates = ", ".join(f"{f} {move.new[f]!r}" for f in _RATE_FIELDS)
            lines.append(f"  new       {move.key} ({move.id}): {rates}{tail}")
        elif move.entries:
            moved = ", ".join(f"{f} {move.old[f]!r} -> {move.new[f]!r}"
                              for f in _RATE_FIELDS if move.old[f] != move.new[f])
            lines.append(f"  changed   {move.key} ({move.id}): {moved}{tail}")
        else:
            on = move.membership == "+"
            lines.append(f"  {'metered' if on else 'unmetered'}  {move.key} "
                         f"({move.id}): long-context meter "
                         f"{'on' if on else 'off'}")
        if move.entries and move.new.get("note"):
            lines.append(f"            note: {move.new['note']}")
    if vendor.refusals:
        lines += ["refused, rows left untouched:",
                  *(f"  {reason}" for reason in vendor.refusals)]
    if vendor.notices:
        lines += ["notices:", *(f"  {notice}" for notice in vendor.notices)]
    return "\n".join(lines)
