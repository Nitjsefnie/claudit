"""Backend and Inspector parity for Claude context-turn traces."""
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from backend import constants, parse
from backend.parse_lanes import sniff_format

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "fixtures" / "parser"
PARSER_LANES_JS = ROOT / "src" / "parser-lanes.js"
PARSER_JS = ROOT / "src" / "parser.js"
CONTEXT_GROWTH_JSX = ROOT / "src" / "context-growth-view.jsx"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ctx_reply_before_prompt.jsonl", [(1, 100), (3, 200)]),
        ("ctx_out_of_order_ts.jsonl", [(2, 100), (4, 200)]),
    ],
)
def test_backend_context_turns_use_file_order(name, expected):
    """Resumed and out-of-order files retain each line-ordered turn."""
    result = parse.parse_file(name, (FIXTURE_DIR / name).read_bytes())
    assert [(turn["line"], turn["input"]) for turn in result["ctx_turns"]] \
        == expected


def _browser_context_stats(fixtures: list[Path]) -> dict[str, Any]:
    script = r"""
      const fs = require('fs');
      const path = require('path');
      global.window = {};
      const [lanesPath, parserPath, viewPath] = __MODULE_PATHS__;
      require(lanesPath);
      require(parserPath);
      const source = fs.readFileSync(viewPath, 'utf8');
      const boundary = source.indexOf('function ContextGrowthView');
      if (boundary < 0) throw new Error('ContextGrowthView boundary not found');
      const computeTurnStats = new Function(
        'window', source.slice(0, boundary) + '\nreturn computeTurnStats;'
      )(window);
      const rows = __FIXTURE_PATHS__.map((filename) => {
        const text = fs.readFileSync(filename, 'utf8');
        const tx = window.parseTranscript(text);
        return {
          name: path.basename(filename),
          turns: computeTurnStats(tx).map((turn) => ({
            line: turn.line,
            ctx: turn.ctx,
          })),
        };
      });
      console.log(JSON.stringify({
        maxPlausibleCtx: window.MAX_PLAUSIBLE_CTX,
        fixtures: rows,
      }));
    """
    script = script.replace(
        "__MODULE_PATHS__",
        json.dumps([str(PARSER_LANES_JS), str(PARSER_JS),
                    str(CONTEXT_GROWTH_JSX)]),
    ).replace(
        "__FIXTURE_PATHS__",
        json.dumps([str(path) for path in fixtures]),
    )
    proc = subprocess.run(
        ["node", "-"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _claude_fixtures_with_context_turns() -> list[tuple[Path, list[dict]]]:
    fixtures = []
    for path in sorted(FIXTURE_DIR.glob("*.jsonl")):
        blob = path.read_bytes()
        if sniff_format(blob) != "claude":
            continue
        turns = parse.parse_file(path.name, blob)["ctx_turns"]
        if turns:
            fixtures.append((path, turns))
    return fixtures


def test_browser_context_turns_match_backend_for_claude_fixtures():
    """Inspector rows match every non-empty Claude fixture trace."""
    if shutil.which("node") is None:
        pytest.skip("node not available")

    fixtures = _claude_fixtures_with_context_turns()
    names = {path.name for path, _ in fixtures}
    assert len(fixtures) >= 5
    assert {
        "ctx_reply_before_prompt.jsonl",
        "ctx_out_of_order_ts.jsonl",
    } <= names

    result = _browser_context_stats([path for path, _ in fixtures])
    browser_by_name = {
        fixture["name"]: [
            (turn["line"], turn["ctx"]) for turn in fixture["turns"]
        ]
        for fixture in result["fixtures"]
    }
    disagreements = []
    for path, backend_turns in fixtures:
        expected = [(turn["line"], turn["input"]) for turn in backend_turns]
        actual = browser_by_name.get(path.name)
        if actual != expected:
            disagreements.append(
                f"{path.name}: browser {actual!r}, backend {expected!r}"
            )
    assert not disagreements, "\n".join(disagreements)


def test_browser_context_plausibility_bound_matches_backend():
    if shutil.which("node") is None:
        pytest.skip("node not available")
    result = _browser_context_stats([])
    assert result.get("maxPlausibleCtx") == constants.MAX_PLAUSIBLE_CTX


def test_browser_drops_context_above_backend_plausibility_bound():
    if shutil.which("node") is None:
        pytest.skip("node not available")
    path = FIXTURE_DIR / "ctx_implausible_context.jsonl"
    backend_result = parse.parse_file(path.name, path.read_bytes())
    assert len(backend_result["records"]) == 1
    assert backend_result["ctx_turns"] == []

    browser_result = _browser_context_stats([path])
    assert browser_result["fixtures"][0]["turns"] == []
