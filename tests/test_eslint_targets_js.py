"""The lint gate's own answer to which files it lints, and under which rules."""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.node_run import run_node

ROOT = Path(__file__).resolve().parents[1]
ERROR = 2
# For each file ESLint lints under the given operands, the rules it resolves.
RESOLVED_RULES = """
import { ESLint } from 'eslint';
const eslint = new ESLint({ ruleFilter: () => false });
const rules = {};
for (const { filePath } of await eslint.lintFiles(process.argv.slice(1))) {
  rules[filePath] = (await eslint.calculateConfigForFile(filePath)).rules ?? {};
}
console.log(JSON.stringify(rules));
"""


def test_every_tracked_ci_module_is_linted_under_the_error_rules() -> None:
    if shutil.which("node") is None or not (ROOT / "node_modules" / "eslint").is_dir():
        pytest.skip("ESLint is installed only where `npm ci` has run")
    lint = shlex.split(json.loads(
        (ROOT / "package.json").read_text(encoding="utf-8"))["scripts"]["lint"])
    assert lint[0] == "eslint", lint
    resolved = run_node(
        ["node", "--input-type=module", "-e", RESOLVED_RULES, *lint[1:]],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=120)
    assert resolved.returncode == 0, resolved.stderr
    rules = json.loads(resolved.stdout)
    tracked = subprocess.run(
        ["git", "ls-files", "scripts/ci/*.mjs"], cwd=ROOT, capture_output=True,
        text=True, check=True).stdout.split()
    assert tracked, "git lists no scripts/ci/*.mjs, so nothing was checked"
    unlinted = [path for path in tracked if str(ROOT / path) not in rules]
    assert not unlinted, f"`npm run lint` does not lint {unlinted}"
    lenient = [(path, rule) for path in tracked for rule in ("no-undef", "no-unused-vars")
               if rules[str(ROOT / path)].get(rule, [0])[0] != ERROR]
    assert not lenient, f"linted, but not as an error: {lenient}"
