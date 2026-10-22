"""Retry one timeout."""
from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any


def run_node(
    node_argv: Sequence[str], *, input: str | bytes | None = None,  # pylint: disable=W0622
    timeout: float = 60.0, **run_kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    retried = False
    while True:
        try:
            return subprocess.run(node_argv, input=input, timeout=timeout, **run_kwargs)  # pylint: disable=W1510
        except subprocess.TimeoutExpired:
            if retried:
                raise
            retried = True
