#!/usr/bin/env python3
from __future__ import annotations

import argparse
from decimal import Decimal
from typing import TYPE_CHECKING
from pathlib import Path

from backend.pricing_document import effective_rates

if TYPE_CHECKING:
    from refresh_provider_rates import Move, Result

_RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")
_SAMPLE_REASONS = {
    "cheapest resolution has no stable endpoint identity": "cheapest resolution",
    "host endpoints use more than one tag prefix": "multiple provider tags",
    "tag prefix is shared by another host": "provider tag shared",
    "endpoint has a pricing schedule": "endpoint schedule",
    "endpoint lists a long-context band": "long-context band",
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


def _sampled_reason(reason: str) -> str:
    if reason.startswith("listed-pricing log unavailable for "):
        reason = "log unavailable"
    elif reason.startswith("endpoint selection found "):
        count = reason.removeprefix("endpoint selection found ").split(" ", 1)[0]
        reason = "no endpoint selected" if count == "0" else f"{count} endpoints selected"
    else:
        reason = _SAMPLE_REASONS.get(reason, reason)
    return reason


def _sampled_host(host: str, reason: str) -> str:
    return f"{host} ({_sampled_reason(reason)})"


def _sampled_text(sampled: dict[str, str]) -> str:
    groups: dict[str, list[str]] = {}
    for host, reason in sorted(sampled.items()):
        groups.setdefault(_sampled_reason(reason), []).append(host)
    return "; ".join(_sampled_host(", ".join(hosts), reason)
                     for reason, hosts in groups.items())


_UNKNOWN_TAG_NOTICE = "names neither a known region nor a quantization"
_DELISTED_NOTICE = "delisted from OpenRouter's catalog; row kept, not fetched"


def _notice_parts(notice: str):
    subject, separator, message = notice.partition(": ")
    if not separator:
        return None
    if message.startswith(_DELISTED_NOTICE):
        return message, subject
    if (message.startswith("tag ") and
            message.endswith(_UNKNOWN_TAG_NOTICE)):
        return message, subject
    if message == "the vendor lists no first-party endpoint; skipped":
        return message, subject
    return None


def notice_lines(notices: list[str]) -> list[str]:
    """Group catalog notices by their shared message, retaining subjects.

    The rendered lines are the notice unit: the same text is what the run
    carries as its baseline and what the commit message shows, so a grouped
    line that gains or loses a subject is a different line."""
    lines: list[str | None] = []
    groups: dict[str, tuple[int, list[str]]] = {}
    for notice in notices:
        parts = _notice_parts(notice)
        if parts is None:
            if notice not in lines:
                lines.append(notice)
            continue
        message, subject = parts
        if message not in groups:
            groups[message] = len(lines), []
            lines.append(None)
        groups[message][1].append(subject)
    for message, (index, subjects) in groups.items():
        subjects_text = ", ".join(sorted(set(subjects)))
        lines[index] = f"{message}: {subjects_text}"
    return [line for line in lines if line is not None]


def vanished_state(vanished: list[tuple[str, str]]) -> dict[str, list[str]]:
    state: dict[str, list[str]] = {}
    for model, host in vanished:
        state.setdefault(model, []).append(host)
    return {model: sorted(set(hosts)) for model, hosts in sorted(state.items())}


def sampled_state(sampled: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Each sampled host's recorded classification: the short display
    reason the report's sampled line groups by, so a changed
    classification shows as a diff of the state file. A model with no
    sampled hosts leaves no empty entry behind."""
    return {model: {host: _sampled_reason(reason)
                    for host, reason in sorted(hosts.items())}
            for model, hosts in sorted(sampled.items()) if hosts}


def _visible(lines: list[str], previous: frozenset[str]) -> list[str]:
    """Drop a rendered line the baseline already carried.

    A line the previous run published — shown or suppressed — is not
    repeated; a line that is new, or that changed, is not in the baseline
    and still appears."""
    return [line for line in lines if line not in previous]


def _search_change(move: Move) -> str | None:
    """A separately sampled search-rate change on a token move."""
    old = move.old.get("web_search", 0.0) if move.old else 0.0
    new = move.new.rates.get("web_search", 0.0)
    if old == new:
        return None
    return f"web_search {old!r} → {new!r}, sampled at detection"


def _move_text(move: Move) -> str:
    text = _price_move_text(move)
    old = move.old.get("context_length") if move.old else None
    new = move.new.identity.get("context_length")
    if old != new:
        text += f"; context_length {old!r} → {new!r}, sampled at detection"
    return text


def _price_move_text(move: Move) -> str:
    new = move.new
    old_rates = effective_rates(move.old) if move.old is not None else None
    off = f" ({_discount_note(new.discount)})" if new.discount else ""
    windows = f", schedule of {len(new.schedule)} windows" if new.schedule else ""
    search = _search_change(move)
    if move.source == "search":
        return _search_move_text(move, off)
    if move.source == "context":
        return (f"  changed   {move.host}: token rates unchanged"
                f"{'; ' + search if search else ''}{off}")
    if move.source == "log" and (move.entries_appended > 1 or search):
        return _logged_move_text(move, new, old_rates, search, off)
    if move.old is None:
        return _new_move_text(move, new, windows, off)
    return _changed_move_text(move, new, old_rates, off)


def _search_move_text(move: Move, off: str) -> str:
    old_search = move.old.get("web_search", 0.0) if move.old else 0.0
    log_count = max(0, move.entries_appended - 1)
    history = (f"; retained {log_count} token-history entries"
               if log_count else "")
    verb = "changed" if move.old is not None else "new"
    return (f"  {verb:<9} {move.host}: web_search {old_search!r} → "
            f"{move.new.rates.get('web_search', 0.0)!r}, sampled at detection"
            f"{history}{off}")


def _logged_move_text(move: Move, new, old_rates: dict | None,
                      search: str | None, off: str) -> str:
    count = move.entries_appended
    unit = "entry" if count == 1 else "entries"
    if move.old is None:
        token_text = f"newest fresh {new.rates['fresh']!r}"
    elif search:
        assert old_rates is not None
        changed = [f"{field} {old_rates[field]!r} → {new.rates[field]!r}"
                   for field in _RATE_FIELDS
                   if old_rates[field] != new.rates[field]]
        token_text = ", ".join(changed) if changed else "token rates unchanged"
    else:
        assert old_rates is not None
        token_text = (f"newest fresh {old_rates['fresh']!r} → "
                      f"{new.rates['fresh']!r}")
    search_text = f"; {search}" if search else ""
    verb = "new" if move.old is None else "changed"
    return (f"  {verb:<9} {move.host}: {count} log {unit}, "
            f"{token_text}{search_text}{off}")


def _new_move_text(move: Move, new, windows: str, off: str) -> str:
    rates = ", ".join(f"{field} {new.rates[field]!r}" for field in _RATE_FIELDS)
    search = new.rates.get("web_search", 0.0)
    if search:
        rates += f", web_search {search!r}"
    return f"  new       {move.host}: {rates}{windows}{off}"


def _changed_move_text(move: Move, new, old_rates: dict | None,
                       off: str) -> str:
    assert move.old is not None
    assert old_rates is not None
    moved = [f"{field} {old_rates[field]!r} → {new.rates[field]!r}"
             for field in _RATE_FIELDS if old_rates[field] != new.rates[field]]
    if move.old.get("web_search", 0.0) != new.rates.get("web_search", 0.0):
        moved.append(f"web_search {move.old.get('web_search', 0.0)!r} → "
                     f"{new.rates.get('web_search', 0.0)!r}")
    if move.old.get("schedule") != new.schedule:
        moved.append(f"schedule of {len(move.old.get('schedule') or [])} → "
                     f"{len(new.schedule or [])} windows")
    return f"  changed   {move.host}: {', '.join(moved)}{off}"


def _discount_note(discount: Decimal) -> str:
    if discount < 0:
        return f"{format((-discount * 100).normalize(), 'f')}% over base"
    return f"{format((discount * 100).normalize(), 'f')}% off"


def report(stamp: str, result: Result, tracked: dict,
           previous: frozenset[str] = frozenset(),
           *, standing: bool = True) -> str:
    """Render moves, sampled coverage, vanished rows, and notices.

    A notice line the baseline already carried is omitted; an empty
    ``previous`` renders every notice. ``standing=False``
    drops the vanished and sampled blocks — the standing state the commit
    body leaves to the recorded JSON keys — together with any
    model section left empty by the drop; the run's own stdout report
    renders with the default, ``standing=True``."""
    lines = [f"OpenRouter provider rates, detected {stamp}"]
    if not result.moves:
        lines.append("no rate moved")
    for model, source in tracked.items():
        section = []
        for move in result.moves:
            if move.model == model:
                section.append(_move_text(move))
                if move.reason:
                    section.append(f"    {move.host}: {move.reason}")
        if standing:
            vanished = sorted({host for name, host in result.vanished
                               if name == model})
            if vanished:
                section.append(f"  vanished  {', '.join(vanished)} (rows kept)")
            sampled = sorted(result.sampled.get(model, {}).items())
            if sampled:
                section.append(f"  sampled   {_sampled_text(dict(sampled))}")
        if section:
            lines += ["", f"{model} ({source['id']})", *section]
    if result.refusals:
        lines += ["", "refused, rows left untouched:",
                  *(f"  {reason}" for reason in result.refusals)]
    notices = _visible(notice_lines(result.notices), previous)
    if notices:
        lines += ["", "notices:", *(f"  {notice}" for notice in notices)]
    return "\n".join(lines)


def commit_message(result: Result, body: str, vendor=None) -> str:
    changed = sum(1 for move in result.moves if move.old is not None)
    added = len(result.moves) - changed
    counts = [f"{changed} changed" if changed else "",
              f"{added} new" if added else "",
              f"{len(result.refusals)} refused" if result.refusals else ""]
    subject = "Refresh OpenRouter provider rates: " + ", ".join(c for c in counts if c)
    if vendor is not None and vendor.moves:
        # Disjoint counts: an added move that also joins the meter counts
        # under "added" alone, never twice.
        v_added = sum(1 for m in vendor.moves if m.added)
        v_metered = sum(1 for m in vendor.moves
                        if not m.added and m.membership == "+")
        v_unmetered = sum(1 for m in vendor.moves
                          if not m.added and m.membership == "-")
        v_updated = sum(1 for m in vendor.moves if not m.added and not m.membership)
        segments = [f"{v_added} added" if v_added else "",
                    f"{v_metered} metered" if v_metered else "",
                    f"{v_unmetered} unmetered" if v_unmetered else "",
                    f"{v_updated} updated" if v_updated else ""]
        subject += "; vendor table: " + ", ".join(s for s in segments if s)
    return f"{subject}\n\n{body}\n\nCaptured by .github/workflows/refresh-pricing.yml.\n"


def arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append moved OpenRouter provider rates to src/pricing.json.")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be appended; write nothing")
    parser.add_argument("--commit-msg", type=Path,
                        help="write the commit message here when anything is appended")
    return parser.parse_args(argv)


def vendor_report(stamp: str, vendor,
                  previous: frozenset[str] = frozenset()) -> str:
    """Render the vendor pass's section of the hourly report.

    ``previous`` is the same published-notice set ``report`` takes: a vendor
    notice line unchanged since the last commit is omitted."""
    lines = [f"Vendor tracked table, detected {stamp}"]
    if not (vendor.moves or vendor.refusals or vendor.notices):
        lines.append("no first-party vendor moved")
    for move in vendor.moves:
        meter_on = move.meter is not None or move.membership == "+"
        meter_off = move.membership == "-" and move.meter is None
        tail = (" [metered]" if meter_on
                else " [unmetered]" if meter_off else "")
        if move.context_length is not None:
            tail += f" [context_length {move.context_length}]"
        if move.meter is not None:
            meter = move.meter
            tail += (f" [threshold {meter['threshold']}, "
                     f"input x{meter['input_mult']:g}, "
                     f"output x{meter['output_mult']:g}]")
        if move.added:
            lines.append(f"  added     {move.key} ({move.id}): joins the "
                         f"tracked table{tail}")
        else:
            if meter_on:
                state, label = "on", "metered"
            elif meter_off:
                state, label = "off", "unmetered"
            else:
                state, label = "unchanged", "updated"
            lines.append(f"  {label:9} {move.key} ({move.id}): "
                         f"long-context meter {state}{tail}")
    if vendor.refusals:
        lines += ["refused, rows left untouched:",
                  *(f"  {reason}" for reason in vendor.refusals)]
    notices = _visible(notice_lines(vendor.notices), previous)
    if notices:
        lines += ["notices:", *(f"  {notice}" for notice in notices)]
    return "\n".join(lines)
