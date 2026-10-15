"""Provider-row storage and grouped instants; model histories stay untouched."""
from __future__ import annotations

import copy
import math
import re
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal

FORMAT = "pricing-rows/3"
_FIELDS = ("fresh", "output", "read")
_RESERVED = {"from", "note", *_FIELDS}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_SECOND = 253402300799  # 9999-12-31T23:59:59Z, shared with the browser.
_INSTANT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                      r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])")


def _histories(doc: dict) -> Iterator[tuple[dict, str, list, str]]:
    providers = doc.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("pricing.json: providers is not an object")
    for model, hosts in providers.items():
        if not isinstance(hosts, dict):
            raise ValueError(f"pricing.json: {model}: providers are not an object")
        for host, entries in hosts.items():
            where = f"{model} via {host}"
            if not isinstance(entries, list) or not entries:
                raise ValueError(f"{where}: expected a non-empty provider series")
            yield hosts, host, entries, where


def _second(stamp: object, at: str) -> int:
    try:
        if not isinstance(stamp, str) or not _INSTANT.fullmatch(stamp):
            raise ValueError
        instant = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")
        return _delta(int((instant - _EPOCH).total_seconds()), at)
    except ValueError as exc:
        raise ValueError(f"{at}: timestamp must be whole-second ISO with Z or ±HH:MM") from exc


def _delta(value: object, at: str) -> int:
    if (not isinstance(value, int) or isinstance(value, bool)
            or not 0 <= value <= _MAX_SECOND):
        raise ValueError(f"{at}: timestamp delta is not a non-negative integer in range")
    return value


def _stamp(second: int, at: str) -> str:
    _delta(second, at)
    return (_EPOCH + timedelta(seconds=second)).isoformat().replace("+00:00", "Z")


def _discount_text(value: int | float) -> str:
    """Render a JSON discount number with JavaScript's decimal/exponent bounds."""
    if value == 0:
        return "0% off"
    text = str(float(value))
    coefficient, exponent_mark, exponent = text.partition("e")
    if exponent_mark:
        power = int(exponent)
        if -6 <= power < 21:
            text = format(Decimal(text), "f")
        else:
            text = f"{coefficient.removesuffix('.0')}e{'+' if power >= 0 else '-'}{abs(power)}"
    else:
        text = text.removesuffix(".0")
    return f"{text}% off"


def _note_number(note: str) -> str | int | float:
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?% off", note):
        number = float(note[:-5])
        if math.isfinite(number) and number <= 2**53 - 1:
            number = int(number) if number.is_integer() else number
            if _discount_text(number) == note:
                return number
    return note


def _encode_times(stamp: object, previous: int | None, seen: set[int],
                  at: str, leading: bool) -> tuple[object, int | None]:
    if stamp is None:
        if not leading:
            raise ValueError(f"{at}: only the first entry has no 'from'")
        return None, previous
    stamps = stamp if isinstance(stamp, list) else [stamp]
    if not stamps:
        raise ValueError(f"{at}: timestamp list is empty")
    seconds = [_second(s, at) for s in stamps]
    if seen.intersection(seconds):
        raise ValueError(f"{at}: timestamp instants are not distinct")
    seen.update(seconds)
    first = seconds[0]
    deltas = [first if previous is None else first - previous]
    deltas += [b - a for a, b in zip(seconds, seconds[1:])]
    if (previous is not None and deltas[0] <= 0
            or any(d <= 0 for d in deltas[1:])):
        raise ValueError(f"{at}: timestamp instants are not strictly increasing")
    return (deltas if isinstance(stamp, list) else deltas[0]), first


def _encode_values(entry: dict, at: str) -> list:
    if not isinstance(entry, dict) or not all(f in entry for f in _FIELDS):
        raise ValueError(f"{at}: expected an entry with fresh, output, read")
    row = [entry[f] for f in _FIELDS]
    if "note" in entry:
        if not isinstance(entry["note"], str):
            raise ValueError(f"{at}: 'note' is not a string")
        row.append(_note_number(entry["note"]))
    rest = {k: v for k, v in entry.items() if k not in _RESERVED}
    if rest:
        row.append(rest)
    return row


def encode_pricing_doc(doc: dict) -> dict:
    """Copy entries into rows; legacy offset spellings normalize to UTC instants."""
    if "_format" in doc:
        raise ValueError("pricing.json: encode expects a decoded document")
    out = copy.deepcopy(doc)
    for hosts, host, entries, where in _histories(out):
        previous, seen, rows = None, set(), []
        for i, entry in enumerate(entries):
            at = f"{where}[{i}]"
            values = _encode_values(entry, at)
            slot, previous = _encode_times(entry.get("from"), previous, seen, at, i == 0)
            rows.append([slot, *values])
        hosts[host] = rows
    out["_format"] = FORMAT
    return out


def _decode_values(row: object, at: str) -> dict:
    if not isinstance(row, list) or not 4 <= len(row) <= 6:
        raise ValueError(f"{at}: expected a positional provider row of 4 to 6 slots")
    entry: dict = dict(zip(_FIELDS, row[1:4]))
    tail = row[4:]
    if tail and not isinstance(tail[0], dict):
        note = tail.pop(0)
        if isinstance(note, str):
            entry["note"] = note
        elif (isinstance(note, (int, float)) and not isinstance(note, bool)
              and math.isfinite(note)):
            entry["note"] = _discount_text(note)
        else:
            raise ValueError(f"{at}: note is not a string or finite number")
    if tail:
        if (len(tail) != 1 or not isinstance(tail[0], dict)
                or set(tail[0]) & _RESERVED):
            raise ValueError(f"{at}: invalid provider row rest object")
        entry.update(tail[0])
    return entry


def _decode_times(slot: object, previous: int | None, seen: set[int],
                  at: str, leading: bool) -> tuple[object, int | None]:
    if slot is None:
        if not leading:
            raise ValueError(f"{at}: only the first entry has no 'from'")
        return None, previous
    deltas = slot if isinstance(slot, list) else [slot]
    if not deltas:
        raise ValueError(f"{at}: timestamp list is empty")
    seconds = []
    for j, delta in enumerate(deltas):
        value = _delta(delta, at)
        if (j or previous is not None) and value == 0:
            raise ValueError(f"{at}: timestamp instants are not distinct")
        second = value + (seconds[-1] if j else previous or 0)
        if second in seen:
            raise ValueError(f"{at}: timestamp instants are not distinct")
        seen.add(second)
        seconds.append(second)
    stamps = [_stamp(s, at) for s in seconds]
    return (stamps if isinstance(slot, list) else stamps[0]), seconds[0]


def decode_pricing_doc(doc: dict) -> dict:
    """Copy stored rows to canonical entries, retaining grouped ``from`` lists."""
    if doc.get("_format") != FORMAT:
        raise ValueError(f"pricing.json: _format must be {FORMAT!r}")
    out = copy.deepcopy(doc)
    out.pop("_format")
    for hosts, host, rows, where in _histories(out):
        previous, seen, entries = None, set(), []
        for i, row in enumerate(rows):
            at = f"{where}[{i}]"
            entry = _decode_values(row, at)
            entry["from"], previous = _decode_times(row[0], previous, seen, at, i == 0)
            entries.append(entry)
        hosts[host] = entries
    return out


def migrate_pricing_doc(doc: dict) -> dict:
    """Copy legacy entries to rows; existing storage is validated and re-encoded.

    Absent/null leading ``from`` become T=null, decoded as explicit null.
    Legacy offset spellings normalize to UTC, as in decoded row documents.
    """
    return encode_pricing_doc(decode_pricing_doc(doc) if "_format" in doc else doc)


def flatten_provider_history(
        entries: list[dict], where: str, instant_for: Callable[[object, str], datetime]
        ) -> tuple[list[dict], list[int]]:
    """Order grouped instants, keeping owning rows for predecessor no-op checks."""
    if not any(isinstance(e.get("from"), list) for e in entries):
        return entries, list(range(len(entries)))
    points, seen = [], set()
    previous_first = None
    for i, entry in enumerate(entries):
        at = f"{where}[{i}]"
        stamps = entry.get("from")
        stamps = stamps if isinstance(stamps, list) else [stamps]
        if not stamps:
            raise ValueError(f"{at}: timestamp list is empty")
        previous = None
        for j, stamp in enumerate(stamps):
            if stamp is None:
                if i or j or len(stamps) > 1:
                    raise ValueError(f"{at}: only the first entry has no 'from'")
                instant = datetime.min.replace(tzinfo=timezone.utc)
            else:
                instant = instant_for(stamp, at)
            unordered = (previous is not None and instant <= previous
                         or j == 0 and previous_first is not None and instant <= previous_first)
            if instant in seen or unordered:
                raise ValueError(f"{at}: timestamp instants are not distinct and increasing")
            if j == 0:
                previous_first = instant
            seen.add(instant)
            points.append((instant, i, {**entry, "from": stamp}))
            previous = instant
    points.sort(key=lambda point: point[0])
    return [entry for _, _, entry in points], [i for _, i, _ in points]
