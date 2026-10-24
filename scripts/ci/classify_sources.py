from __future__ import annotations

from collections.abc import Callable
import difflib
import os
import re
from pathlib import Path

VERSION = re.compile(rb'''PRICING_VERSION\s*=\s*(['"])([0-9]+)\1\s*''')

# A ci-gate leg reads: tracked .py files (lint), CodeQL's Python and
# JS/TS extension set, action.yml/action.yaml anywhere (zizmor), and
# the shipped unit file. Only files no leg reads narrow as docs-only.
LEG_READ_SUFFIXES = frozenset({'.py', '.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx'})


def leg_reads(path: str) -> bool:
    """Whether a ci-gate leg reads `path`, so the change runs full gates."""
    return (Path(path).suffix in LEG_READ_SUFFIXES
            or Path(path).name in ('action.yml', 'action.yaml')
            or path == 'examples/claudit.service')


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
