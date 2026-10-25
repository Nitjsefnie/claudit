from __future__ import annotations

from collections.abc import Callable
import difflib
import fnmatch
import os
import re
from pathlib import Path

VERSION = re.compile(rb'''PRICING_VERSION\s*=\s*(['"])([0-9]+)\1\s*''')

# Files a ci-gate leg reads: .py (lint) and .pyw (CodeQL python); the
# javascript-typescript setupFilters (HTML/JS/TS enums; YAML minus the
# JS pass's -**/*.lock — the actions pass re-includes actions.lock;
# JSON names, index-filter patterns); zizmor (.y(a)ml names, covered by
# the YAML members); the tests leg's *requirements* sweep.
LEG_READ_SUFFIXES = frozenset({'.py', '.pyw', '.htm', '.html', '.xhtm',
                               '.xhtml', '.vue', '.hbs', '.ejs', '.njk',
                               '.erb', '.jsp', '.dot', '.js', '.jsx', '.mjs',
                               '.cjs', '.es6', '.es', '.xsjs', '.xsjslib',
                               '.raml', '.yaml', '.yml', '.ts', '.tsx',
                               '.mts', '.cts'})
LEG_READ_BASENAMES = frozenset({'action.yml', 'action.yaml', 'actions.lock',
                                'package.json', 'manifest.json',
                                'xs-app.json', '.xsaccess'})
LEG_READ_PATTERNS = ('*.view.json', '*tsconfig*.json', '.eslintrc*',
                     'codeql-javascript-*.json', '*requirements*.txt',
                     '*requirements*.in')
LEG_SHEBANG_JS = re.compile(rb'\b(node(js)?|bun|tsx)\b')
LEG_SHEBANG_PY = re.compile(rb'#! *(/usr|/bin|/local)*/?(env)? *python')


def _leg_reads_shebang(path: str) -> bool:
    """Read the first bytes of the extensionless `path` from the checkout cwd."""
    try:
        with open(path, 'rb') as leg_file:
            prefix = leg_file.read(100)
    except OSError:
        return True
    if not prefix.startswith(b'#!'):
        return False
    line = re.split(rb'[\r\n]', prefix, maxsplit=1)[0]
    if len(line) == len(prefix):
        return True
    return bool(LEG_SHEBANG_JS.search(line) or LEG_SHEBANG_PY.match(line))


def leg_reads(path: str) -> bool:
    """Whether a ci-gate leg reads `path`, so the change runs full gates."""
    name = Path(path).name
    if (Path(path).suffix.lower() in LEG_READ_SUFFIXES
            or name in LEG_READ_BASENAMES
            or any(fnmatch.fnmatchcase(name, pattern)
                   for pattern in LEG_READ_PATTERNS)
            or path == 'examples/claudit.service'):
        return True
    if os.path.splitext(path)[1] != '':
        return False
    return _leg_reads_shebang(path)


def pricing_change(event: dict, run: Callable, refs_reader: Callable, blob_reader: Callable) -> bool:
    repository = event.get('repository')
    refs = refs_reader(event, repository, run) if repository else None
    if refs is None:
        return False
    before, after = (blob_reader(repository, 'backend/constants.py', ref, run) for ref in refs)
    if before is None or after is None:
        return False
    return numeric_assignment_only(before, after)


def numeric_assignment_only(before: bytes, after: bytes) -> bool:
    """Only same-width digits may change; the measured byte pair stays exact."""
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    edits = [change for change in difflib.SequenceMatcher(None, old, new).get_opcodes()
             if change[0] != 'equal']
    if len(edits) != 1:
        return False
    kind, first, last, start, stop = edits[0]
    if kind != 'replace' or last - first != 1 or stop - start != 1:
        return False
    removed, added = VERSION.fullmatch(old[first]), VERSION.fullmatch(new[start])
    if removed is None or added is None or len(removed[2]) != len(added[2]):
        return False
    return (old[first][:removed.start(2)] + old[first][removed.end(2):]
            == new[start][:added.start(2)] + new[start][added.end(2):])


def espree_dir() -> str:
    return os.environ.get('CLAUDIT_ESPREE_DIR') or str(Path(__file__).resolve().parents[2] / 'node_modules')


def decode_output(command: list[str], output: bytes) -> bytes | str:
    return output if 'Accept: application/vnd.github.raw' in command else output.decode('utf-8')


def source_tokens(text: bytes, path: str, run: Callable, python: Callable, javascript: Callable):
    try:
        decoded = text.decode('utf-8')
    except UnicodeError:
        return None
    if path.endswith('.py'):
        return python(decoded)
    if path.endswith(('.js', '.jsx')):
        return javascript(decoded, path, run)
    return None
