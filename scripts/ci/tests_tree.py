"""Sum tests/**/*.py lines; absent=0."""
from __future__ import annotations

from pathlib import Path


def _tests_tree_lines(repo_root: Path) -> int:
    root = repo_root / 'tests'
    if not root.is_dir():
        return 0
    return sum(len(path.read_bytes().splitlines())
               for path in sorted(root.rglob('*.py')))
