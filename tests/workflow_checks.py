"""Shared checks for workflow references and local apt sanitization."""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import yaml


_PROPERTY = r"[A-Za-z_][A-Za-z0-9_-]*"
_TOKEN = re.compile(r"(?P<string>'(?:[^']|'')*')|(?P<name>" + _PROPERTY
                    + r")|(?P<operator>[^\s])")


def references(value: str, context: str) -> set[tuple[str, ...]]:
    """Static dot/index references inside expressions, excluding string literals."""
    found = set()
    for expression in re.findall(r"\$\{\{((?:'(?:[^']|'')*'|[^'])*?)\}\}", value, re.S):
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


def _output_redirect(line: str) -> bool | None:
    lexer = shlex.shlex(line, posix=False, punctuation_chars=">")
    lexer.whitespace_split = True
    words = list(lexer)
    targets = [words[index + 1] for index, word in enumerate(words[:-1])
               if word in (">", ">>") and (index == 0 or not words[index - 1].isdigit()
                                          or words[index - 1] == "1")]
    if not targets:
        return None
    return targets[-1] in {"$GITHUB_OUTPUT", "${GITHUB_OUTPUT}",
                           '"$GITHUB_OUTPUT"', '"${GITHUB_OUTPUT}"'}


def written_outputs(run: str) -> set[str]:
    """Literal echo assignments, honoring command redirects over group redirects."""
    blocks = [(run, False)]
    blocks += [(body, _output_redirect(tail) is True)
               for body, tail in re.findall(r"(?ms)^ *\{\n(.*?)^ *\}([^\n]*)", run)]
    found = set()
    for block, inherited in blocks:
        for line in block.splitlines():
            match = re.match(r' *echo ["\']?([A-Za-z_][A-Za-z0-9_-]*)=', line)
            if match:
                target = _output_redirect(line)
                if target is True or (target is None and inherited):
                    found.add(match[1])
    return found


def output_references(value: str, steps: list[dict]) -> set[tuple[str, str]]:
    """Resolve each referenced producer and require its output write."""
    refs = references(value, "steps")
    assert refs, value
    found = set()
    for ref in refs:
        assert len(ref) == 3 and ref[1] == "outputs", ref
        producers = [step for step in steps if step.get("id") == ref[0]]
        assert len(producers) == 1, ref
        assert ref[2] in written_outputs(producers[0].get("run", "")), ref
        found.add((ref[0], ref[2]))
    return found


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
