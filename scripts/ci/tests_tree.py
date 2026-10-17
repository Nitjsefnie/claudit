"""Dependency-free tests-tree workload identity shared by the bench and writer."""
from __future__ import annotations

from pathlib import Path


def _tests_tree_lines(repo_root: Path) -> int:
    """The scanned tree's size: the workload identity the seed binds to.

    The fixture's tree-scanning tests (the db-marker derivation, the
    version-literal guard, the scratch-DB scan) walk ``tests/``
    recursively, and their cost is per-line over that set: a change to
    the tree's content moves the counts with no fixture, interpreter or
    machine change (issue #524's series). A tree with no ``tests/`` has
    no scanned workload and records an honest 0.
    """
    root = repo_root / 'tests'
    if not root.is_dir():
        return 0
    return sum(len(path.read_bytes().splitlines())
               for path in sorted(root.rglob('*.py')))
