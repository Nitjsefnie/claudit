from __future__ import annotations

from collections.abc import Callable
import difflib
import os
import re
import shlex
from pathlib import Path

VERSION = re.compile(rb'''PRICING_VERSION\s*=\s*(['"])([0-9]+)\1\s*''')

SUFFIXES = {'.py': 'python', '.js': 'javascript', '.jsx': 'javascript',
            '.mjs': 'javascript', '.cjs': 'javascript', '.yaml': 'yaml',
            '.yml': 'yaml', '.sh': 'shell'}
SHELLS = {'sh', 'bash', 'dash', 'ash', 'ksh', 'mksh', 'zsh'}


def language_for(path: Path) -> str | None:
    language = SUFFIXES.get(path.suffix)
    if language or path.suffix:
        return language
    with path.open('rb') as handle:
        first = handle.readline()
    if not first.startswith(b'#!'):
        return None
    words = shlex.split(first[2:].decode('utf-8'))
    if not words:
        return None
    executable = Path(words[0]).name
    if executable == 'env':
        words = [word for word in words[1:]
                 if not word.startswith('-') and '=' not in word]
        executable = Path(words[0]).name if words else ''
    return 'shell' if executable in SHELLS else None


def measured(paths: list[str]) -> bool:
    try:
        return any(language_for(Path(path)) for path in paths)
    except (OSError, ValueError):
        return True


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
