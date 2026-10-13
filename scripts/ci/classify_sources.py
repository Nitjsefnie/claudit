from __future__ import annotations

from collections.abc import Callable
import difflib
import importlib
import os
import re
from pathlib import Path

measurement = importlib.import_module('scripts.ci.comment_share' if __package__ else 'comment_share')
VERSION = re.compile(r'''PRICING_VERSION\s*=\s*(['"])([0-9]+)\1\s*''')


def measured(paths: list[str]) -> bool:
    try:
        return any(measurement.language_for(Path(path)) for path in paths)
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


def numeric_assignment_only(before: str, after: str) -> bool:
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    edits = [change for change in difflib.SequenceMatcher(None, old, new).get_opcodes()
             if change[0] != 'equal']
    if len(edits) != 1:
        return False
    kind, first, last, start, stop = edits[0]
    if kind != 'replace' or last - first != 1 or stop - start != 1:
        return False
    removed, added = VERSION.fullmatch(old[first]), VERSION.fullmatch(new[start])
    return bool(removed and added)


def espree_dir() -> str:
    return os.environ.get('CLAUDIT_ESPREE_DIR') or str(Path(__file__).resolve().parents[2] / 'node_modules')
