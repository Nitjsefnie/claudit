"""PARSER_VERSION's home stays outside the pricing bot's write surface."""
from __future__ import annotations

import ast
from pathlib import Path

import yaml

from backend import constants, parser_version
from tests.source_scan_cache import for_source, source

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "refresh-pricing.yml"
REFRESH_COMMON = ROOT / "scripts" / "ci" / "refresh_common.py"
HOME = "backend/parser_version.py"


def _run_blocks() -> list[str]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return [step.get("run") or ""
            for job in doc["jobs"].values()
            for step in (job.get("steps") or [])]


def _staged_paths() -> set[str]:
    """Paths the bot stages: cp sources, git-add args, diff paths."""
    paths: set[str] = set()
    for run in _run_blocks():
        for line in run.splitlines():
            stripped = line.strip()
            if stripped.startswith("cp "):
                token = stripped.split()[1]
                if not token.startswith(('"', "$")):
                    paths.add(token)
            elif "git diff --quiet -- " in stripped:
                paths.update(stripped.split("-- ", 1)[1].split())
            elif "push-repo\" add " in stripped:
                paths.update(stripped.split(" add ", 1)[1].split())
    return paths


def test_the_pricing_bots_write_surface_excludes_the_parser_home():
    staged = _staged_paths()
    # Non-vacuous: the bot still stages constants.py itself.
    assert "backend/constants.py" in staged
    home = Path(parser_version.__file__).resolve()
    assert home.relative_to(ROOT).as_posix() == HOME
    assert HOME not in staged, (
        "PARSER_VERSION's home is inside the pricing bot's write surface; "
        f"staged: {sorted(staged)}")


def test_the_bots_version_bump_never_names_the_parser_version():
    # Non-vacuous: the helper rewrites PRICING_VERSION lines only.
    text = REFRESH_COMMON.read_text(encoding="utf-8")
    assert "PRICING_VERSION" in text
    assert "PARSER_VERSION" not in text


def test_constants_holds_no_parser_version():
    # No re-export: the constant must not return to constants.py.
    assert not hasattr(constants, "PARSER_VERSION")


def test_every_backend_parser_version_read_goes_through_the_module():
    readers = set()
    offenders = []
    for path in sorted((ROOT / "backend").glob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(for_source(source(path)).tree()):
            if (isinstance(node, ast.Attribute)
                    and node.attr == "PARSER_VERSION"):
                if (isinstance(node.value, ast.Name)
                        and node.value.id == "parser_version"):
                    readers.add(rel)
                else:
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, offenders
    # Both stampers read the module: one bump reaches every stamp.
    assert {"backend/ingest.py", "backend/ingest_scope.py"} <= readers
