"""Shared checks for workflow references and local apt sanitization."""
from __future__ import annotations

import re
from pathlib import Path

import yaml


_PROPERTY = r"[A-Za-z_][A-Za-z0-9_-]*"
_TOKEN = re.compile(r"(?P<string>'(?:[^']|'')*')|(?P<name>" + _PROPERTY
                    + r")|(?P<operator>[^\s])")


def references(value: str, context: str) -> set[tuple[str, ...]]:
    """Static dot/index references inside expressions, excluding string literals."""
    found = set()
    for expression in re.findall(r"\$\{\{(.*?)\}\}", value, re.S):
        tokens = [(match.lastgroup, match[0][1:-1].replace("''", "'"))
                  if match.lastgroup == "string" else (match.lastgroup, match[0])
                  for match in _TOKEN.finditer(expression)]
        for index, token in enumerate(tokens):
            if token != ("name", context) or (index and tokens[index - 1][1] == "."):
                continue
            parts = []
            index += 1
            while index < len(tokens):
                if (tokens[index] == ("operator", ".") and index + 1 < len(tokens)
                        and tokens[index + 1][0] == "name"):
                    parts.append(tokens[index + 1][1])
                    index += 2
                elif (tokens[index] == ("operator", "[") and index + 2 < len(tokens)
                      and tokens[index + 1][0] == "string"
                      and tokens[index + 2] == ("operator", "]")):
                    parts.append(tokens[index + 1][1])
                    index += 3
                else:
                    break
            if parts:
                found.add(tuple(parts))
    return found


def written_outputs(run: str) -> set[str]:
    """Output names in echo assignments redirected singly or as a shell group."""
    target = r'>> *(?:"\$GITHUB_OUTPUT"|\$GITHUB_OUTPUT)(?=\s|$)'
    blocks = re.findall(r'(?ms)^ *\{\n(.*?)^ *\} *' + target, run)
    blocks += [line for line in run.splitlines() if re.search(target, line)]
    return set(re.findall(r'(?m)^ *echo ["\']?([A-Za-z_][A-Za-z0-9_-]*)=',
                          "\n".join(blocks)))


def assert_apt_sanitizer(root: Path, step: dict) -> None:
    """Resolve the called local composite and check its Microsoft source removal."""
    ref = step.get("uses", "")
    assert ref.startswith("$/"), ref
    action = yaml.load((root / ref[2:] / "action.yml").read_text(encoding="utf-8"),
                       Loader=yaml.BaseLoader)
    assert action["runs"]["using"] == "composite"
    assert any(re.search(
        r"\brm\s+-f\s+/etc/apt/sources.list.d/microsoft-prod.list"
        r"\s+/etc/apt/sources.list.d/microsoft-prod.sources",
        item.get("run", "")) for item in action["runs"]["steps"])
