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
LOADER_JS = ROOT / "src" / "pricing-loader.js"
PARSER_JS = ROOT / "src" / "parser.js"
CTX_INPUT_JS = ROOT / "src" / "ctx-input.js"
RATES_JS = ROOT / "src" / "rates.js"
CONTEXT_GROWTH_JSX = ROOT / "src" / "context-growth-view.jsx"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ctx_reply_before_prompt.jsonl", [(1, 100, 100), (3, 200, 100)]),
        ("ctx_out_of_order_ts.jsonl", [(2, 100, 100), (4, 200, 100)]),
        (
            "ctx_prompt_inside_merged_request.jsonl",
            [(2, 200, 200), (5, 300, 100)],
        ),
    ],
)
def test_backend_context_turns_use_file_order(name, expected):
    """Leading, out-of-order, and merged requests retain line-order turns."""
    result = parse.parse_file(name, (FIXTURE_DIR / name).read_bytes())
    assert [
        (turn["line"], turn["input"], turn["delta"])
        for turn in result["ctx_turns"]
    ] == expected
    assert result["turn_count"] == len(expected)


def _browser_context_stats(fixtures: list[Path]) -> dict[str, Any]:
    script = r"""
      const fs = require('fs');
      const path = require('path');
      global.window = {};
      const [lanesPath, loaderPath, ratesPath, parserPath, ctxInputPath, viewPath] = __MODULE_PATHS__;
      require(lanesPath);
      require(loaderPath);
      require(ratesPath);
      require(parserPath);
      require(ctxInputPath);
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
            delta: turn.delta,
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
        json.dumps([str(PARSER_LANES_JS), str(LOADER_JS), str(RATES_JS),
                    str(PARSER_JS), str(CTX_INPUT_JS),
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
        "ctx_prompt_inside_merged_request.jsonl",
    } <= names

    result = _browser_context_stats([path for path, _ in fixtures])
    browser_by_name = {
        fixture["name"]: fixture["turns"]
        for fixture in result["fixtures"]
    }
    disagreements = []
    for path, backend_turns in fixtures:
        expected = [(turn["line"], turn["input"]) for turn in backend_turns]
        actual = browser_by_name.get(path.name)
        if actual is None:
            disagreements.append(f"{path.name}: browser fixture result missing")
            continue
        actual_contexts = [(turn["line"], turn["ctx"]) for turn in actual]
        if actual_contexts != expected:
            disagreements.append(
                f"{path.name}: browser {actual_contexts!r}, backend {expected!r}"
            )
            continue
        backend_deltas = [turn["delta"] for turn in backend_turns]
        browser_deltas = [turn["delta"] for turn in actual]
        if browser_deltas[0] is not None:
            disagreements.append(
                f"{path.name}: browser first-row delta {browser_deltas[0]!r}, "
                "expected null"
            )
        if browser_deltas[1:] != backend_deltas[1:]:
            disagreements.append(
                f"{path.name}: browser deltas {browser_deltas[1:]!r}, "
                f"backend deltas {backend_deltas[1:]!r} after first row"
            )
        context_deltas = [
            actual[index]["ctx"] - actual[index - 1]["ctx"]
            for index in range(1, len(actual))
        ]
        if context_deltas != backend_deltas[1:]:
            disagreements.append(
                f"{path.name}: browser ctx differences {context_deltas!r}, "
                f"backend deltas {backend_deltas[1:]!r} after first row"
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
