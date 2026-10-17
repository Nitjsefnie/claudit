"""Compare emitted executable lines with Espree over the measured JS surface."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[1]
NODE_MODULES = REPO / "node_modules"


@pytest.mark.skipif(not NODE_MODULES.exists(), reason="JS toolchain not installed")
def test_js_coverage_scanner_matches_espree() -> None:
    """A scanner misparse must neither hide code nor add comment-only lines."""
    files = subprocess.run(
        ["git", "ls-files", "-z", "src/*.js"], cwd=REPO,
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.rstrip("\0").split("\0")
    assert files and all(files)
    script = r"""
      const fs = require('node:fs');
      const path = require('node:path');
      const { createRequire } = require('node:module');
      const [repo, modules] = process.argv.slice(1);
      const load = createRequire(path.join(modules, 'package.json'));
      const espree = load(path.join(modules, 'espree'));
      const convert = load(path.join(modules, 'v8-to-istanbul'));
      const files = JSON.parse(fs.readFileSync(0, 'utf8'));
      (async () => {
        const results = [];
        for (const file of files) {
          const absolute = path.join(repo, file);
          const source = fs.readFileSync(absolute, 'utf8').trimEnd();
          const parsed = espree.parse(source, {
            ecmaVersion: 'latest', sourceType: 'module',
            comment: true, range: true,
          });
          const code = source.split('');
          for (const { range: [start, end] } of parsed.comments) {
            for (let i = start; i < end; i++) {
              if (code[i] !== '\n' && code[i] !== '\r') code[i] = ' ';
            }
          }
          const expected = code.join('').split(/\r?\n/)
            .flatMap((line, index) => line.trim() ? [index + 1] : []);
          const converter = convert(absolute);
          await converter.load();
          const emitted = Object.values(converter.toIstanbul()[absolute].statementMap)
            .map(statement => statement.start.line);
          results.push({ file, expected, emitted });
        }
        console.log(JSON.stringify(results));
      })().catch(error => { console.error(error); process.exitCode = 1; });
    """
    result = subprocess.run(
        ["node", "-e", script, str(REPO), str(NODE_MODULES)],
        cwd=REPO, input=json.dumps(files), check=True,
        capture_output=True, text=True, timeout=60,
    )
    for comparison in json.loads(result.stdout):
        assert comparison["emitted"] == comparison["expected"], comparison["file"]
