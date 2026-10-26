"""Shared checks for workflow references and local apt sanitization."""
from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

TIGHTEN_CMD = (
    'python3 "$RUNNER_TEMP/ratchet-repo/scripts/ci/suite_ratchet.py" '
    '--tighten "$RUNNER_TEMP/suite-data/suite-measurement.json" '
    '--thresholds '
    '"$RUNNER_TEMP/ratchet-repo/.github/ci-thresholds.json"')

_PROPERTY = r"[A-Za-z_][A-Za-z0-9_-]*"
_TOKEN = re.compile(r"(?P<string>'(?:[^']|'')*')|(?P<name>" + _PROPERTY
                    + r")|(?P<operator>[^\s])")


def load_workflow(path: Path) -> dict:
    """Read workflow/action YAML with string keys, including on."""
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def workflow_by_name(name: str) -> dict:
    """A workflow file from the workflows directory, empty when blank."""
    return load_workflow(WORKFLOWS / name) or {}


def job_of(doc: dict, job_id: str) -> dict:
    """The named job of a workflow."""
    return doc["jobs"][job_id]


def steps_of(job: dict) -> list:
    """The job's step list."""
    return job["steps"]


def run_lines(step: dict) -> list[str]:
    """The step's run block as stripped non-empty lines."""
    return [line.strip() for line in (step.get("run") or "").splitlines()
            if line.strip()]


def find_step(job: dict, needle: str) -> dict:
    """The one step whose run names needle; fails otherwise."""
    matches = [step for step in steps_of(job)
               if needle in (step.get("run") or "")]
    assert len(matches) == 1, (needle, [s.get("name") for s in matches])
    return matches[0]


def is_checkout(step: dict) -> bool:
    """Shared checkout identification for workflow controls."""
    return step.get("uses", "").partition("@")[0] == "actions/checkout"


def _expression_bodies(value: str) -> Iterator[tuple[int, str]]:
    """Yield body offsets and text, respecting quoted expression delimiters."""
    offset = 0
    while offset < len(value):
        opening = value.find("${{", offset)
        if opening < 0:
            return
        start = opening + 3
        cursor = start
        quoted = False
        while cursor < len(value):
            if value[cursor] == "'":
                if quoted and value.startswith("''", cursor):
                    cursor += 2
                    continue
                quoted = not quoted
            elif not quoted and value.startswith("}}", cursor):
                break
            cursor += 1
        else:
            return
        yield start, value[start:cursor]
        offset = cursor + 2


def references(value: str, context: str) -> set[tuple[str, ...]]:
    """Static dot/index references inside expressions, excluding string literals."""
    found = set()
    for _, expression in _expression_bodies(value):
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


def rename_references(value: str, context: str, names: dict[str, str]) -> str:
    """Rename first lookup keys inside expressions, retaining all other text."""
    changes = []
    for offset, expression in _expression_bodies(value):
        tokens = list(_TOKEN.finditer(expression))
        for index, token in enumerate(tokens):
            if (token.lastgroup != "name" or token[0] != context
                    or (index and tokens[index - 1][0] == ".")
                    or index + 2 >= len(tokens)):
                continue
            start, key = tokens[index + 1:index + 3]
            if start[0] == "." and key.lastgroup == "name":
                name, end = key[0], key.end()
            elif (start[0] == "[" and key.lastgroup == "string"
                  and index + 3 < len(tokens) and tokens[index + 3][0] == "]"):
                name, end = key[0][1:-1].replace("''", "'"), tokens[index + 3].end()
            else:
                continue
            if name in names:
                replacement = "['" + names[name].replace("'", "''") + "']"
                changes.append((offset + start.start(), offset + end, replacement))
    for start, end, replacement in reversed(changes):
        value = value[:start] + replacement + value[end:]
    return value


def _output_redirect(line: str) -> bool | None:
    lexer = shlex.shlex(line, posix=False, punctuation_chars=">")
    lexer.whitespace_split = True
    words = list(lexer)
    targets = [words[index + 1] for index, word in enumerate(words[:-1])
               if word in (">", ">>") and (index == 0 or not words[index - 1].isdigit() or words[index - 1] == "1")]
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


def dockerfile_from_reference(path: Path) -> str:
    """The one FROM reference of a Dockerfile.

    Raises on zero or several FROM lines or a missing token.
    """
    lines = [line for line in path.read_text(encoding="utf-8").splitlines()
             if line.startswith("FROM")]
    assert len(lines) == 1, (path, lines)
    match = re.match(r"FROM\s+([^\s#]+)", lines[0])
    assert match, lines[0]
    return match[1]


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
