"""The real c8 line gate must not measure comments or blank lines."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[1]
C8 = REPO / "node_modules" / ".bin" / "c8"
SOURCE = """function run(flag) {
  if (flag) {
    return 1;
  } else {
    return 2;
  }
}
run(true);
"""


def _line_percentage(directory: Path, source: str) -> float:
    """Execute and report through the same converter as the JS gate."""
    directory.mkdir()
    script = directory / "variant.js"
    script.write_text(source, encoding="utf-8")
    command = [str(C8), "--reporter=json-summary", "--include", "*.js"]
    command += ["node", str(script)]
    subprocess.run(command, cwd=directory, check=True, capture_output=True,
                   text=True, timeout=60)
    summary = json.loads((directory / "coverage" / "coverage-summary.json")
                         .read_text(encoding="utf-8"))
    return summary["total"]["lines"]["pct"]


@pytest.mark.skipif(not C8.exists(), reason="c8 is installed in the JS gate only")
@pytest.mark.parametrize("insertion", [
    "  // first\n  // second\n  // third\n",
    "\n\n\n",
    "  /* first\n     second\n     third */\n",
], ids=["line-comments", "blank-lines", "block-comment"])
def test_js_coverage_ignores_non_code_lines(tmp_path: Path, insertion: str) -> None:
    """Adding non-code lines to the executed arm must leave 6/8 covered."""
    with_lines = SOURCE.replace("    return 1;", insertion + "    return 1;")
    baseline = _line_percentage(tmp_path / "baseline", SOURCE)
    variant = _line_percentage(tmp_path / "variant", with_lines)
    assert baseline == variant == 75.0
