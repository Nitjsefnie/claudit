"""Tolerate one node timeout at the same bound; a second timeout still fails."""
from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any


def run_node(
    node_argv: Sequence[str], *, input: str | bytes | None = None,  # pylint: disable=redefined-builtin
    timeout: float = 60.0, **run_kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Retry exactly once on TimeoutExpired; pass other outcomes through."""
    retried = False
    while True:
        try:
            return subprocess.run(node_argv, input=input, timeout=timeout, **run_kwargs)  # pylint: disable=subprocess-run-check
        except subprocess.TimeoutExpired:
            if retried:
                raise
            retried = True
