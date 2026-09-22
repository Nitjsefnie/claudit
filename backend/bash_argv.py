"""Line churn for a command given as an ARGV ARRAY.

Ported from codexmeter's bash_churn for Codex's `monitor` calls, whose
command is `command: ["bash", "-lc", "..."]` rather than a `cmd` string,
so the shell payload has to be found positionally. The churn itself is
read by backend.bash_churn's own scanners, so the estimates stay
claudit's (SV-BASH-CHURN).
"""
from __future__ import annotations

import re

from backend.bash_churn import _python_churn, bash_churn

# Programs whose inline-payload flag (-c / -lc) carries a whole script as
# the NEXT argv token.
_SHELL_PROGS = ("sh", "bash", "zsh", "dash", "ksh", "ash")
_PY_PROG = re.compile(r"^python(?:3(?:\.\d+)?)?$")
_INLINE_FLAGS = ("-c", "-lc", "-ic", "-lic")


def argv_churn(argv: object) -> tuple[int, int]:
    """(lines_added, lines_deleted) for a command given as an ARGV ARRAY.

    Codex's `monitor` takes `command: ["bash", "-lc", "..."]` rather
    than a `cmd` string, so the payload has to be found positionally.
    Only an inline script counts: `bash -lc '<script>'` is a whole shell
    command and `python3 -c '<program>'` a whole program, both fully
    present in the call. A plain invocation — `python3 tests/run.py` —
    carries no payload at all; whatever it writes is described in a
    FILE this call does not contain, so it counts 0.
    """
    if not isinstance(argv, list) or not argv:
        return 0, 0
    if not all(isinstance(token, str) for token in argv):
        return 0, 0
    prog = argv[0].rsplit("/", 1)[-1]
    for i, token in enumerate(argv[1:], start=1):
        if token not in _INLINE_FLAGS or i + 1 >= len(argv):
            continue
        payload = argv[i + 1]
        if _PY_PROG.match(prog):
            return _python_churn(payload)
        if prog in _SHELL_PROGS:
            return bash_churn(payload)
        return 0, 0
    return 0, 0
