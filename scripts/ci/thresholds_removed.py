"""Declared threshold removals from one bounded judged-range history read."""
from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

BASELINE_FAMILIES = frozenset({
    'module_size_baseline', 'pylint_suppression_baseline', 'comment_share_baseline',
})


class Declarations(frozenset[str]):
    """Exact entry tokens, with bare baseline families expanded over candidates."""

    def paths(self, family: str, candidates: Iterable[str]) -> set[str]:
        """Return declared paths of a baseline family within the measured set."""
        if family not in BASELINE_FAMILIES:
            return set()
        return {path for path in candidates
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
