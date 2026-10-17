"""shortModelName keeps vendor prefixes while folding snapshot and variant suffixes.
Snapshots and bracketed variants must share a colour and series with the
base id. Execute the extracted JSX helper through node; rendering is separate."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHARTS_EXTRA_JSX = ROOT / "src" / "dashboard-charts-extra.jsx"


def _source() -> str:
    src = CHARTS_EXTRA_JSX.read_text(encoding="utf-8")
    start = src.index("function shortModelName")
    end = src.index("\nfunction ContextGrowthPanel", start)
    return src[start:end]


def _node(body: str):
    script = f"""
      global.window = {{}};
      eval({json.dumps(_source())});
      {body}
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


CASES = [
    ("claude-opus-5-5",                 "claude-opus-5-5"),
    ("claude-fable-5[1m]",              "claude-fable-5"),
    ("claude-opus-4-7-20251101",        "claude-opus-4-7"),
    ("Claude-Opus-4-7",                 "claude-opus-4-7"),
    ("claude-fable-5-1[1m]",            "claude-fable-5-1"),
    # other vendors pass through the fold untouched
    ("gpt-6.1-sol",                     "gpt-6.1-sol"),
    ("glm-5.3-flash",                   "glm-5.3-flash"),
    ("deepseek/deepseek-v4-pro",        "deepseek/deepseek-v4-pro"),
    ("<synthetic>",                     "<synthetic>"),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("raw,key", CASES)
def test_short_model_name_keeps_the_vendor_prefix(raw, key):
    got = _node(
        f"console.log(JSON.stringify(shortModelName({json.dumps(raw)})));"
    )
    assert got == key


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_short_model_name_falls_back_to_unknown():
    got = _node(
        "console.log(JSON.stringify([shortModelName(''), "
        "shortModelName(undefined), shortModelName(null)]));"
    )
    assert got == ["unknown", "unknown", "unknown"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_no_claude_key_loses_its_prefix():
    """The regression itself, stated over every Claude id in the corpus's
    shape: a dated snapshot, a bracketed variant, a plain id. Each keeps
    `claude-`; only the suffix after it is folded."""
    got = _node("""
      const raw = ['claude-opus-5-5', 'claude-sonnet-5', 'claude-haiku-4-5',
                   'claude-opus-4-8-20260101', 'claude-fable-5[1m]'];
      console.log(JSON.stringify(
        raw.map(m => [m, shortModelName(m)])));
    """)
    assert all(before.startswith("claude-")
               for before, _ in got), got
    assert [after for _, after in got] == [
        "claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5",
        "claude-opus-4-8", "claude-fable-5",
    ], got
