"""Declared threshold removals from one bounded judged-range history read."""
from __future__ import annotations

import importlib
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

BASELINE_FAMILIES = frozenset({'pylint_suppression_baseline'})


class Declarations(frozenset[str]):
    """Exact entry tokens or bare baseline-family declarations."""

    def paths(self, family: str, removed_entries: Iterable[str]) -> set[str]:
        """Select declared paths from a supplied set of removed entries."""
        if family not in BASELINE_FAMILIES:
            return set()
        return {path for path in removed_entries
                if family in self or f'{family}.{path}' in self}


def declared(base_rev: str | None, head_rev: str, root: Path) -> Declarations:
    """One bounded history read; a malformed trailer line admits nothing."""
    if not base_rev or not base_rev.strip():
        return Declarations()
    try:
        proc = subprocess.run(
            ['git', '-C', str(root), 'log', '--format=%B', f'{base_rev}..{head_rev}'],
            capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return Declarations()
    if proc.returncode != 0:
        return Declarations()
    declarations = set()
    for line in proc.stdout.decode('utf-8', 'replace').split('\n'):
        match = re.fullmatch(r'Thresholds-Removed:[ \t]*(.*)', line)
        if not match:
            continue
        entries = set()
        for token in match[1].split(','):
            entry, separator, kind = (part.strip() for part in token.partition('='))
            if not separator or not entry or kind not in ('bug', 'definition'):
                break
            entries.add(entry)
        else:
            declarations.update(entries)
    return Declarations(declarations)


def exempt_paths(base_rev: str | None, head_rev: str, root: Path,
                 family: str, head_entries: Iterable[str],
                 candidates: Iterable[str]) -> set[str]:
    """Exempt only measured, declared entries actually removed since base."""
    declarations = declared(base_rev, head_rev, root)
    if not declarations or family not in BASELINE_FAMILIES:
        return set()
    try:
        proc = subprocess.run(
            ['git', '-C', str(root), 'show', f'{base_rev}:.github/ci-thresholds.json'],
            capture_output=True, check=False, timeout=10)
        if proc.returncode != 0:
            return set()
        thresholds = importlib.import_module(
            'scripts.ci.thresholds' if __package__ else 'thresholds')
        base = thresholds.normalise(thresholds.validate.decode(proc.stdout))
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()
    removed_entries = set(base.get(family, {})) - set(head_entries)
    return declarations.paths(family, removed_entries) & set(candidates)
