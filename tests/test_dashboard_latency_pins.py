"""Pins for two dashboard-latency mechanisms that could be reverted with
no test failing.

1. The gzip middleware's `compresslevel=6` and `minimum_size=1024`
   (backend/app.py). Removing the level silently ships a different
   compression trade-off.
2. The shared in-flight fetch in `fetchCostByContext`, moved out of
   src/dashboard-charts-extra.jsx into the plain-JS src/cost-by-context.js
   so node can drive it: app.jsx renders the tokens and the cost variants
   of Cost by Context side by side and both must join ONE request per URL,
   re-requesting only after the previous one settles.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from backend.app import _SelectiveGZip, app
from tests.node_run import run_node

ROOT = Path(__file__).resolve().parents[1]
CBC_JS = ROOT / "src" / "cost-by-context.js"


def test_gzip_middleware_uses_level_six():
    """The dashboard's gzip middleware must keep its pinned compression
    level and minimum size. The assertion fails if `compresslevel=6` is
    removed (the kwarg disappears) or changed."""
    entries = [m for m in app.user_middleware if m.cls is _SelectiveGZip]
    assert entries, "_SelectiveGZip is not installed on the app"
    kwargs = entries[0].kwargs
    assert kwargs.get("compresslevel") == 6, (
        f"gzip compresslevel is {kwargs.get('compresslevel')!r}, not 6")
    assert kwargs.get("minimum_size") == 1024, (
        f"gzip minimum_size is {kwargs.get('minimum_size')!r}, not 1024")


def _node(body: str):
    script = f"""
      global.window = {{}};
      require({str(CBC_JS)!r});
      {body}
    """
    proc = run_node(
        ["node"], input=script, capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# A fetch fake that records every call and resolves to {url}. node ships a
# real global fetch, so plain assignment does not take -- defineProperty.
_FETCH_FAKE = """
  const calls = [];
  Object.defineProperty(globalThis, 'fetch', {
    value: (url, init) => {
      calls.push({ url, creds: init && init.credentials });
      return Promise.resolve({
        ok: true, json: () => Promise.resolve({ url }),
      });
    },
    writable: true, configurable: true, enumerable: true,
  });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_cost_by_context_shares_one_in_flight_request():
    """Two concurrent calls for one URL issue ONE fetch and resolve to the
    same promise; a different URL issues its own."""
    out = _node(_FETCH_FAKE + """
      (async () => {
        const p1 = window.costByContext.fetch('/a');
        const p2 = window.costByContext.fetch('/a');
        const p3 = window.costByContext.fetch('/b');
        const [r1, r2, r3] = await Promise.all([p1, p2, p3]);
        console.log(JSON.stringify({
          calls: calls.map(c => c.url),
          creds: calls.map(c => c.creds),
          same: p1 === p2,
          different: p1 === p3,
          r1, r2, r3,
        }));
      })();
    """)
    assert out["calls"] == ["/a", "/b"], out
    assert out["same"] is True, "concurrent calls did not share the promise"
    assert out["different"] is False
    assert out["creds"] == ["same-origin", "same-origin"]
    assert out["r1"] == out["r2"] == {"url": "/a"}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_cost_by_context_refetches_after_settle():
    """The map entry is dropped once the promise settles, so a later call
    re-requests rather than reading the stale body."""
    out = _node(_FETCH_FAKE + """
      (async () => {
        const p1 = window.costByContext.fetch('/a');
        await p1;
        // let the settle-time map delete run
        await Promise.resolve();
        const p2 = window.costByContext.fetch('/a');
        await p2;
        console.log(JSON.stringify({
          calls: calls.length, reused: p1 === p2,
        }));
      })();
    """)
    assert out["calls"] == 2, "a settled URL was not re-requested"
    assert out["reused"] is False
