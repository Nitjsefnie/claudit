"""The real AST coverage gate must not measure comments or blank lines."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from tests.node_run import run_node


REPO = Path(__file__).resolve().parents[1]
NODE_MODULES = REPO / "node_modules"
CONVERTER = REPO / "scripts" / "ci" / "js_coverage.mjs"
SOURCE = """function run(flag) {
  if (flag) {
    return 1;
  } else {
    return 2;
  }
}
run(true);
"""
FUNCTION_EXPRESSION_SOURCE = SOURCE.replace(
    "  if (flag)", "  const quotient = function () {} / 2;\n  if (flag)")


def _line_percentage(directory: Path, source: str) -> float:
    """Execute and report through the same converter as the JS gate."""
    directory.mkdir()
    script = directory / "variant.js"
    script.write_text(source, encoding="utf-8")
    cov_dir = directory / "v8-coverage"
    env = {**os.environ, "NODE_V8_COVERAGE": str(cov_dir)}
    run_node(["node", str(script)], cwd=directory, env=env,
             check=True, capture_output=True, text=True, timeout=60)
    command = ["node", str(CONVERTER), str(cov_dir),
               "--include", "*.js"]
    run_node(command, cwd=directory, check=True, capture_output=True,
             text=True, timeout=60)
    summary = json.loads((directory / "coverage" / "coverage-summary.json")
                         .read_text(encoding="utf-8"))
    return summary["total"]["lines"]["pct"]


@pytest.mark.skipif(not NODE_MODULES.exists(), reason="JS toolchain not installed")
@pytest.mark.parametrize("source,needle,insertion,expected", [
    (SOURCE, "  if (flag) {\n", "  // first\n  // second\n  // third\n", 75.0),
    (SOURCE, "  if (flag) {\n", "\n\n\n", 75.0),
    (SOURCE, "  if (flag) {\n", "  /* first\n     second\n     third */\n", 75.0),
    (FUNCTION_EXPRESSION_SOURCE, " / 2;", " /* start\n comment only\n*/", 80.0),
], ids=["line-comments", "blank-lines", "block-comment", "function-expression"])
def test_js_coverage_ignores_non_code_lines(
    tmp_path: Path, source: str, needle: str, insertion: str, expected: float,
) -> None:
    """Non-code additions preserve the baseline's executable-line coverage."""
    with_lines = source.replace(needle, needle + insertion)
    baseline = _line_percentage(tmp_path / "baseline", source)
    variant = _line_percentage(tmp_path / "variant", with_lines)
    assert baseline == variant == expected
