"""No-op shell commands (#1366)."""
from __future__ import annotations

import re

_SEGMENTS = re.compile(r"\s*(?:;|&&|\|\||\||\n)\s*")
_LITERAL_OUT = re.compile(
    r"""^(?:echo|printf)(?:\s+(?:'[^']*'|"[^"$`\\]*"|[^\s$`'"<>|&;\\]+))*\s*$""")
SHELL_TOOLS = frozenset(("Bash", "PowerShell"))


def is_noop(command: str) -> bool:
    segments = [s for s in _SEGMENTS.split(command.strip()) if s]
    return bool(segments) and all(s in ("true", ":") or _LITERAL_OUT.match(s) is not None for s in segments)
