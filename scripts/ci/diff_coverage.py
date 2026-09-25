#!/usr/bin/env python3
"""How much of what a pull request ADDED is covered, and what it missed.

The coverage ratchet next door reports one number for the whole tree, and
that number barely moves: a pull request can add fifty uncovered lines to a
well-covered codebase and the tree figure falls by a fraction of a point,
which is inside the buffer the floor allows. The tree number answers whether
the repository as a whole still is covered; only a patch figure answers
whether the code in front of a reviewer was tested. This reports it: of the
lines a change added that coverage considers executable, how many did the
suites reach.

It is deliberately NOT a gate. A refactor that moves code between files, a
change that only deletes, and a fix whose test lives behind an optional
dependency all produce a low patch figure for reasons a reviewer should
weigh rather than a threshold should block on. The module exits 0 for any
result it could compute, even 0%, and nonzero only for a hard setup error
(a missing file, unparseable input).

Only `backend/` is measured: the suite runs with `--cov=backend`, so tests/
and scripts/ changes have no patch figure at all - absence from the report
is scope, not a miss. Within a measured file, lines coverage does not
consider executable (blank lines, comments, `else:`) are excluded, keeping
the percentage independent of formatting. A changed backend file the report
does not name is listed separately: either a module no test imports, or a
path-spelling mismatch - the one thing absence can mean inside the measured
tree.
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple

# `+++ b/path`, with git's optional quoting and the /dev/null of a deletion.
_TARGET = re.compile(r'^\+\+\+ (.*)$')
# `@@ -old,count +new,count @@`; the counts are optional and mean 1.
_HUNK = re.compile(r'^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
# `condition-coverage="NN% (a/b)"`: real `coverage xml` spells a partially
# executed branch line as a sub-100% condition percentage.
_CONDITION = re.compile(r'^(\d+)%')

# The tree the coverage run measures (--cov=backend). Changed Python files
# outside it are scope, not misses, and a file inside it that the report
# does not name is listed separately in the readout.
_MEASURED_ROOT = 'backend/'


class InputError(ValueError):
    """A coverage report or a diff that cannot be measured at all."""


class Line(NamedTuple):
    """One measured statement: times executed, and whether partially."""
    hits: int
    partial: bool


class FileRow(NamedTuple):
    """One changed file's added-line buckets in the readout table."""
    path: str
    covered: int
    partial: int
    missed: int
    missed_lines: list[int]


def _decode_git_path(value: str) -> str:
    """Decode a quoted Git path and remove its diff-side prefix."""
    if not value.startswith('"'):
        value = value.split('\t', 1)[0]
        return value[2:] if value.startswith('b/') else value
    escaped = value[1:-1] if value.endswith('"') else value[1:]
    escapes = {
        '\\': b'\\', '"': b'"', 'a': b'\a', 'b': b'\b',
        'f': b'\f', 'n': b'\n', 'r': b'\r', 't': b'\t',
        'v': b'\v',
    }
    decoded = bytearray()
    index = 0
    while index < len(escaped):
        char = escaped[index]
        if char != '\\':
            decoded.extend(char.encode('utf-8'))
            index += 1
            continue
        octal = escaped[index + 1:index + 4]
        if len(octal) == 3 and all(item in '01234567' for item in octal):
            decoded.append(int(octal, 8))
            index += 4
            continue
        if index + 1 == len(escaped):
            decoded.extend(b'\\')
            index += 1
            continue
        escaped_char = escaped[index + 1]
        decoded.extend(escapes.get(escaped_char,
                                   escaped_char.encode('utf-8')))
        index += 2
    value = decoded.decode('utf-8')
    return value[2:] if value.startswith('b/') else value


def executable_lines(coverage_xml: Path) -> dict[str, dict[int, Line]]:
    """Read {path: {line number: record}} from a Cobertura report.

    A line is partially executed when the report says so — either the
    `partial` attribute or a sub-100% `condition-coverage`, the spelling
    real `coverage xml` uses for a branch line not every branch of which
    ran. A file may appear as more than one <class>; a line reached by any
    of them keeps its best hit count.
    """
    try:
        root = ET.parse(coverage_xml).getroot()
    except ET.ParseError as error:
        raise InputError(f'{coverage_xml}: unparseable XML: {error}') from error
    if root.tag != 'coverage':
        raise InputError(f'{coverage_xml}: root element is not <coverage>')
    measured: dict[str, dict[int, Line]] = {}
    usable = False
    for class_node in root.iter('class'):
        filename = class_node.get('filename')
        if not filename:
            raise InputError('a <class> without a filename attribute')
        lines = measured.setdefault(filename, {})
        for line_node in class_node.iter('line'):
            number = _int_field(line_node, 'number', filename,
                                required=True)
            incoming = _line_record(line_node, filename)
            existing = lines.get(number)
            if existing is None or incoming.hits > existing.hits:
                lines[number] = incoming
            elif incoming.hits == existing.hits:
                lines[number] = Line(incoming.hits,
                                     incoming.partial or existing.partial)
            usable = True
    if not usable:
        raise InputError('no <line> records — the run measured nothing')
    return measured


def _line_record(line_node: ET.Element, filename: str) -> Line:
    """One <line> element: hits, and partial in either XML spelling."""
    hits = _int_field(line_node, 'hits', filename, required=False)
    partial = line_node.get('partial') == 'true'
    condition = line_node.get('condition-coverage') or ''
    found = _CONDITION.match(condition)
    if found and int(found.group(1)) < 100:
        partial = True
    return Line(hits, partial)


def _int_field(node: ET.Element, field: str, filename: str,
               required: bool) -> int:
    """Read an integer attribute; a line number must be positive."""
    value = node.get(field)
    if value is None:
        if required:
            raise InputError(f'missing {field} for {filename}')
        return 0
    try:
        number = int(value)
    except ValueError as error:
        raise InputError(
            f'invalid {field} for {filename}: {value!r}') from error
    if number < (1 if required else 0):
        raise InputError(f'invalid {field} for {filename}: {value!r}')
    return number


def added_lines(diff_text: str) -> dict[str, set[int]]:
    """Return {path: {line numbers this diff adds}} from a unified diff."""
    added: dict[str, set[int]] = {}
    path: str | None = None
    line_number = 0
    in_hunk = False
    old_remaining = new_remaining = 0
    for line in diff_text.split('\n'):
        header = line.removesuffix('\r')
        if line.startswith('Binary files '):
            raise InputError(
                f'binary diff record is not measurable: {line}')
        # `--- ` counts as a file header only outside a hunk. Git renders a
        # REMOVED line whose content begins `-- ` as `--- ...`, and taking
        # that for a header clears the path and silently drops every later
        # hunk of the file. The `+++` match below is guarded the same way.
        if header.startswith('diff --git ') or (
                not in_hunk and header.startswith('--- ')):
            path = None
            in_hunk = False
            continue
        target = _TARGET.match(header) if not in_hunk else None
        if target is not None:
            name = _decode_git_path(target.group(1))
            path = None if name == '/dev/null' else name
            continue
        hunk = _HUNK.match(header)
        if hunk is not None:
            old_remaining = int(hunk.group(1) or 1)
            line_number = int(hunk.group(2))
            new_remaining = int(hunk.group(3) or 1)
            in_hunk = bool(old_remaining or new_remaining)
            continue
        if path is None or not in_hunk:
            continue
        if line.startswith('+'):
            added.setdefault(path, set()).add(line_number)
            line_number += 1
            new_remaining -= 1
        elif line.startswith('-'):
            old_remaining -= 1
        elif line.startswith(' ') or line == '':
            line_number += 1
            old_remaining -= 1
            new_remaining -= 1
        # A `-` line exists only in the old file and moves nothing.
        if old_remaining == 0 and new_remaining == 0:
            in_hunk = False
    return added


def measure(
    measured: dict[str, dict[int, Line]],
    added: dict[str, set[int]],
) -> tuple[list[FileRow], int, int, int]:
    """Return (per-file rows, covered, partial, total) over added lines.

    A line the XML does not list in a measured file is not an executable
    statement (blank, comment, `else:`): excluding it keeps the percentage
    independent of formatting. A file the XML does not name is out of the
    denominator entirely — absence is not a miss — and is named by
    unmeasured_scope.
    """
    rows: list[FileRow] = []
    covered = partial = total = 0
    for path in sorted(added):
        lines = measured.get(path)
        if not lines:
            continue
        file_covered = file_partial = file_missed = 0
        missed_lines: list[int] = []
        for number in sorted(added[path]):
            line = lines.get(number)
            if line is None:
                continue
            if line.hits == 0:
                file_missed += 1
                missed_lines.append(number)
            elif line.partial:
                file_partial += 1
            else:
                file_covered += 1
        counted = file_covered + file_partial + file_missed
        if not counted:
            continue
        rows.append(FileRow(path, file_covered, file_partial, file_missed,
                            missed_lines))
        covered += file_covered
        partial += file_partial
        total += counted
    return rows, covered, partial, total


def unmeasured_scope(
    measured: dict[str, dict[int, Line]],
    added: dict[str, set[int]],
) -> list[str]:
    """Changed backend/ Python files the coverage report never named.

    Inside the measured tree, absence can mean only a module no test
    imports or a path-spelling mismatch — both worth a reviewer's eye.
    """
    return sorted(
        path for path in added
        if path.lower().endswith('.py')
        and path.startswith(_MEASURED_ROOT)
        and path not in measured)


def _ranges(numbers: list[int]) -> str:
    """Collapse sorted line numbers into `3`, `5-9` spans for readability."""
    spans: list[list[int]] = []
    for number in sorted(numbers):
        if spans and number == spans[-1][1] + 1:
            spans[-1][1] = number
        else:
            spans.append([number, number])
    return ', '.join(str(low) if low == high else f'{low}-{high}'
                     for low, high in spans)


def render(rows: list[FileRow], covered: int, partial: int, total: int,
           unmeasured: list[str] | tuple[str, ...] = ()) -> str:
    """Render the markdown readout body."""
    out = ['### Patch coverage', '']
    if total == 0:
        if unmeasured:
            out.append('No measured added lines. The coverage report names '
                       'none of these changed backend files, so nothing '
                       'here was measured - a module no test imports, or '
                       'a path-spelling mismatch:')
            out.append('')
            out.extend(f'- `{path}`' for path in unmeasured)
        else:
            out.append('No **measured** lines were added. Lines coverage '
                       'does not consider executable (blank lines, '
                       'comments, `else:`) are excluded, and changes '
                       'outside the measured tree have no patch figure.')
    else:
        executed = covered + partial
        percent = 100.0 * executed / total
        if executed < total:
            percent = min(percent, 99.9)
        out.append(f'**{percent:.1f}%** of {total} added lines were '
                   f'executed ({covered} covered, {partial} partial, '
                   f'{total - executed} missed).')
        out.append('')
        out.append('| File | Covered | Partial | Missed | Missed lines |')
        out.append('| --- | ---: | ---: | ---: | --- |')
        for path, file_covered, file_partial, file_missed, missed in rows:
            detail = _ranges(missed) if missed else '—'
            out.append(f'| `{path}` | {file_covered} | {file_partial} | '
                       f'{file_missed} | {detail} |')
        out.append(f'| Total | {covered} | {partial} | '
                   f'{total - executed} | — |')
        if unmeasured:
            out.append('')
            out.append('Changed backend files the coverage report does not '
                       'name (a module no test imports, or a spelling '
                       'mismatch):')
            out.extend(f'- `{path}`' for path in unmeasured)
        if executed == total:
            out.append('')
            out.append('Every added line was executed.')
    out.append('')
    out.append(f'Only `{_MEASURED_ROOT}` is measured by the coverage run; '
               'this is information beside the tree-level ratchet, never '
               'a gate.')
    return '\n'.join(out) + '\n'


def main(argv: list[str] | None = None) -> int:
    """Read the coverage report and a diff; print the markdown body."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--coverage', required=True,
        help='Cobertura XML written by `coverage xml`')
    parser.add_argument(
        '--diff', default='-',
        help='unified diff to read, or - for stdin')
    parser.add_argument(
        '--summary-file',
        help='append the report to this file (e.g. $GITHUB_STEP_SUMMARY)')
    args = parser.parse_args(argv)
    try:
        if args.diff == '-':
            diff_text = sys.stdin.buffer.read().decode('utf-8')
        else:
            diff_text = Path(args.diff).read_text(encoding='utf-8')
        measured = executable_lines(Path(args.coverage))
        added = added_lines(diff_text)
    except (OSError, ET.ParseError, UnicodeDecodeError, InputError) as error:
        print(f'patch coverage: cannot measure: {error}', file=sys.stderr)
        return 1
    rows, covered, partial, total = measure(measured, added)
    body = render(rows, covered, partial, total,
                  unmeasured_scope(measured, added))
    sys.stdout.write(body)
    if args.summary_file:
        with open(args.summary_file, 'a', encoding='utf-8') as handle:
            handle.write(body)
    return 0


if __name__ == '__main__':
    sys.exit(main())
