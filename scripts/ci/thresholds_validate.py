#!/usr/bin/env python3
"""The thresholds document's decoding and scalar validation.

Split out of ``thresholds.py`` for the reason every split in this tree
is made: the loader sat exactly on its 500-line production ceiling, and
a recorded ceiling is never raised by hand (SV-CI-RATCHETS) — code
moves, entries never rise. Everything here answers one question about
one value — is this a number this family accepts, is this a path a
baseline entry may name — and knows nothing about the families
themselves, which the loader names and checks.

The module-path rules are the security-shaped part: a baseline entry
travels into git plumbing and subprocess calls, so a path carrying a
device name, a traversal component or a shell metacharacter is refused
on lexical grounds alone, with no filesystem lookup and no way to name
something outside the tree.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

_INVALID_PATH_CHARS = set('<>:"|?*')
_DEVICE_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{number}' for number in range(1, 10)),
    *(f'LPT{number}' for number in range(1, 10)),
}


def reject_constant(value):
    raise ValueError(f'non-finite JSON number: {value}')


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def decode(raw):
    """Decode thresholds bytes, refusing what JSON would let through.

    Numbers arrive as Decimal so a recorded value keeps the exact
    spelling the file carries — a float would round-trip 92.0 to
    whatever the nearest double spells. A duplicate key, a non-finite
    number and malformed UTF-8 are all refused here rather than being
    silently resolved by the json module's own defaults.
    """
    try:
        text = raw.decode('utf-8') if isinstance(raw, bytes) else raw
        return json.loads(
            text, parse_float=Decimal, parse_int=Decimal,
            parse_constant=reject_constant, object_pairs_hook=_object_pairs)
    except UnicodeDecodeError as error:
        raise ValueError(f'invalid thresholds JSON: {error}') from None
    except json.JSONDecodeError as error:
        raise ValueError(f'invalid thresholds JSON: {error}') from None


def number(value, name):
    """One JSON number as an exact Decimal, or a refusal naming it."""
    if isinstance(value, bool) or not isinstance(
            value, (int, float, Decimal)):
        raise ValueError(f'{name} must be a JSON number')
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f'{name} must be a JSON number') from None
    if not result.is_finite():
        raise ValueError(f'{name} must be finite')
    return result


def required_fields(value, expected, name, labels, optional=()):
    """Every expected field present and nothing beside them.

    ``labels`` names the family in the message, so a refusal inside the
    suite-cost family reads "suite cost run: missing measured" rather
    than a bare field path. ``optional`` names fields that MAY be
    absent and MAY be present: the re-seed tolerance lets the
    suite-cost family go missing, and when it is present it is validated
    like any other family — an empty object is a malformed family, not
    an absent one, and is refused either way.
    """
    if not isinstance(value, dict):
        raise ValueError(f'{name} must be an object')
    label = labels.get(name, f'field: {name}.{{field}}')
    for field in expected:
        if field not in value:
            raise ValueError(f'missing {label.format(field=field)}')
    expected_set = set(expected) | set(optional)
    for field in value:
        if field not in expected_set:
            raise ValueError(f'unknown {label.format(field=field)}')


def coverage_value(value, name):
    result = number(value, name)
    if result < 0 or result > 100:
        raise ValueError(f'{name} must be between 0.0 and 100.0')
    exponent = result.as_tuple().exponent
    # The canonical spelling of a coverage number carries exactly one
    # decimal place (92.0, never 92 or 92.00): it is what the ratchet
    # writes and what coverage --precision=1 measures.
    if not isinstance(exponent, int) or exponent != -1:
        raise ValueError(f'{name} must have exactly one decimal place')
    return result


def instruction_value(value, name):
    """A suite-cost number: one-decimal, finite, never negative.

    Millions of instructions have no natural upper bound, unlike
    coverage's 0..100: the bound here is the one the semantics impose
    (a count cannot be negative), and the one-decimal spelling is what
    the bench writes and the ratchet records.
    """
    result = number(value, name)
    if result < 0:
        raise ValueError(f'{name} must be non-negative')
    exponent = result.as_tuple().exponent
    if not isinstance(exponent, int) or exponent != -1:
        raise ValueError(f'{name} must have exactly one decimal place')
    return result


def share_value(value, name):
    """A reparse phase's share: a percent of the pass's own CPU.

    The same shape and bounds as a coverage value — a bounded percentage
    carrying exactly one decimal place — read as a share of the run
    rather than of a corpus. Zero is a real reading (a phase this corpus
    never reaches, like the sidecar step), not an absence.
    """
    return coverage_value(value, name)


def count_value(value, name):
    """A reparse phase's cost: hundreds of bytecodes per file.

    Bounded below only: the number is a count, so it has no natural
    ceiling, and it carries the same one-decimal spelling as every other
    recorded number. Zero is a real reading, not an absence.
    """
    result = number(value, name)
    if result < 0:
        raise ValueError(f'{name} must not be negative')
    exponent = result.as_tuple().exponent
    if not isinstance(exponent, int) or exponent != -1:
        raise ValueError(f'{name} must have exactly one decimal place')
    return result


def _path_component_safe(component):
    safe = bool(component) and component not in ('.', '..')
    safe = safe and component.rstrip(' .') == component
    safe = safe and component.upper().split('.', 1)[0] not in _DEVICE_NAMES
    safe = safe and not any(char in _INVALID_PATH_CHARS
                            for char in component)
    safe = safe and not any(
        ord(char) < 32 or 127 <= ord(char) <= 159
        or 0xD800 <= ord(char) <= 0xDFFF for char in component)
    if not safe:
        return False
    try:
        return len(component.encode('utf-8')) <= 240
    except UnicodeEncodeError:
        return False


def module_path(value):
    """A baseline entry's path, accepted on lexical grounds alone.

    No filesystem lookup and no reachability claim: the entry is a
    git path, and what CI needs of it is that it names one file in this
    tree and survives being handed to git and to a subprocess as an
    argument.
    """
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError(f'unsafe module path: {value!r}')
    if value.startswith('/') or value.startswith('//'):
        raise ValueError(f'unsafe module path: {value!r}')
    try:
        encoded_length = len(value.encode('utf-8'))
    except UnicodeEncodeError:
        raise ValueError(f'unsafe module path: {value!r}') from None
    if encoded_length > 240:
        raise ValueError(f'unsafe module path: {value!r}')
    components = value.split('/')
    if not all(_path_component_safe(c) for c in components):
        raise ValueError(f'unsafe module path: {value!r}')
    return value


def suite_identity(value, name):
    """The suite-cost seed's workload identity (issue #524): the
    scanned tree's line count. A positive whole number — a Decimal
    that is integral passes, because decode parses every JSON number
    as Decimal."""
    result = number(value, name)
    if result <= 0 or result != result.to_integral_value():
        raise ValueError(f'{name} must be a positive integer')
    return int(result)
