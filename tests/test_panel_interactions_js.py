"""The interaction guard's testable halves, driven through node.

Issue #647's guard proper is `scripts/ci/panel_interactions.mjs`, and it
runs only in the browser leg (panel-layout.yml): nothing in pytest can
hover a bar. What the suite CAN drive are the guard's pure halves — the
KNOWN ledger and the payload-variant rewriter the height check feeds —
plus the wiring that keeps the guard honest: the marks on the panels,
the workflow step, and the one shared server module both guards import.
Importing the module here is itself a test: the guard must launch its
sweep only when executed directly, never when imported.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "ci" / "panel_interactions.mjs"
LAYOUT = ROOT / "scripts" / "ci" / "panel_layout.mjs"
SERVER = ROOT / "scripts" / "ci" / "panel_server.mjs"
WORKFLOW = ROOT / ".github" / "workflows" / "panel-layout.yml"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _node(body: str):
    """Import the guard in node and evaluate `body` against its exports.

    Over STDIN with --input-type=module: the payload embeds paths, and
    Windows refuses a CreateProcess command line over 32k. This import
    is itself an assertion — the guard's main() is gated on direct
    execution, so an import that launched Chromium would never return.
    """
    script = (
        f"const mod = await import({json.dumps(str(MODULE))});\n"
        f"{body}\n"
    )
    proc = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True,
        text=True, timeout=60, check=False, encoding="utf-8", errors="strict",
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


# --- the KNOWN ledger -------------------------------------------------

def test_every_ledger_entry_fires_on_the_defect_it_names():
    """Each entry must match a synthetic finding of its kind — an entry
    that matches nothing is dead weight the stale-entry check would
    fail the next run on."""
    out = _node("""
      // A panel name each entry's matcher genuinely admits; null when a
      // regex matches neither name this test knows, which is itself a
      // ledger defect the assertion below names.
      const known = ['Cost by Context Size', 'Input Tokens',
        'Prompt-Cache TTL Split'];
      const pick = f => f.panel === null ? 'Anything At All'
        : f.panel instanceof RegExp
          ? known.find(n => f.panel.test(n)) || null
        : f.panel;
      const hits = mod.FILED.map(f => {
        const panel = pick(f);
        return [panel, f.kind, panel === null ? null : mod.filedFor(
          panel, f.kind)];
      });
      console.log(JSON.stringify({ hits, issues: mod.FILED.map(f => f.issue) }));
    """)
    assert len(out["hits"]) == len(out["issues"]), out
    for (panel, kind, matched), issue in zip(out["hits"], out["issues"]):
        assert panel is not None, (
            f"the entry for #{issue} ({kind}) matches no known panel name; "
            "its regex and the rendered panels have drifted apart")
        assert matched == issue, (
            f"{kind} on {panel!r} classified as #{matched}, not #{issue}")


def test_the_ledger_does_not_over_match():
    """A kind/panel pair with no entry must classify as a FAILURE — the
    ledger absorbs only the defect it names, never a new one."""
    out = _node("""
      console.log(JSON.stringify({
        ttlTooltip: mod.filedFor('Prompt-Cache TTL Split', 'hover-tooltip'),
        ttlOverflow: mod.filedFor('Prompt-Cache TTL Split', 'tooltip-overflow'),
        heatStyle: mod.filedFor('Activity Heatmap', 'hover-style'),
        unknownKind: mod.filedFor('Input Tokens', 'hover-blink'),
      }));
    """)
    assert out == {"ttlTooltip": None, "ttlOverflow": None,
                   "heatStyle": None, "unknownKind": None}, out


def test_liveness_is_keyed_per_entry_not_per_issue_kind_pair():
    """Two entries may share an issue and a kind — #645 scopes the same
    failure kind to two disjoint panel sets, #651 to two panels. The
    matcher must hand back the ENTRY, so the sweep's liveness set can
    prove each twin fired on its own; a matcher that collapses to the
    issue/kind pair lets one twin's firing stand as the other's."""
    out = _node("""
      const a = mod.filedEntry('Cost by Context Size', 'hover-tooltip');
      const b = mod.filedEntry('Input Tokens', 'hover-tooltip');
      const c = mod.filedEntry('Cost by Agent Type', 'height-growth');
      const d = mod.filedEntry('Tokens by Agent Type', 'height-growth');
      console.log(JSON.stringify({
        distinctScopes: a !== b,
        distinctPanels: c !== d,
        sameIssuePair: a !== null && b !== null && a.issue === b.issue,
        eachMatchesOwn: [a, b, c, d].map(e => e === null ? null : e.issue),
      }));
    """)
    assert out == {"distinctScopes": True, "distinctPanels": True,
                   "sameIssuePair": True,
                   "eachMatchesOwn": [645, 645, 651, 651]}, out


def test_every_ledger_kind_is_in_the_closed_set():
    out = _node("""
      console.log(JSON.stringify({
        covered: mod.FILED.every(f => mod.KINDS.includes(f.kind)),
        kinds: mod.KINDS,
      }));
    """)
    assert out["covered"], out


# --- payload variants: 2 vs 30+ models --------------------------------

BASE = {
    "me.json": {"user": 7},
    "dashboard.json": {
        "cost_by_model": [
            {"model": "alpha", "cost_usd": 3.0},
            {"model": "beta", "cost_usd": 2.0},
            {"model": "gamma", "cost_usd": 1.0},
        ],
        "ctx_traces": [{"model": "alpha", "turns": [1, 2]}],
        "hourly": [{"hour": "h1", "input_tokens": 5}],
    },
    "models.json": {"models": [
        {"model": "alpha", "n": 12}, {"model": "beta", "n": 8},
        {"model": "gamma", "n": 3}]},
    "cost_by_agent.json": {"agents": [
        {"agent_type": "general-purpose", "cost_usd": 9.0},
        {"agent_type": "Explore", "cost_usd": 4.0},
        {"agent_type": "implementer", "cost_usd": 1.0}]},
    "tool_usage.json": {"buckets": [{"tool": "Read", "n": 4}]},
}


def test_two_keeps_two_models_and_drops_the_rest():
    out = _node(f"""
      const v = mod.variantsFrom({json.dumps(BASE)});
      const models = a => [...new Set(a.map(r => r.model))];
      console.log(JSON.stringify({{
        cbm: models(v.two['dashboard.json'].cost_by_model),
        traces: models(v.two['dashboard.json'].ctx_traces),
        sel: models(v.two['models.json'].models),
        gammaGone: !v.two['dashboard.json'].cost_by_model.some(
          r => r.model === 'gamma'),
      }}));
    """)
    assert out["cbm"] == ["alpha", "beta"], out
    assert out["traces"] == ["alpha"], out
    assert out["sel"] == ["alpha", "beta"], out
    assert out["gammaGone"], out


def test_many_yields_at_least_thirty_distinct_models():
    out = _node(f"""
      const v = mod.variantsFrom({json.dumps(BASE)});
      const models = a => [...new Set(a.map(r => r.model))];
      console.log(JSON.stringify({{
        cbmN: models(v.many['dashboard.json'].cost_by_model).length,
        selN: models(v.many['models.json'].models).length,
        originals: models(v.many['dashboard.json'].cost_by_model)
          .filter(m => !m.includes('~')).length,
        firstIsAlpha: v.many['dashboard.json'].cost_by_model[0].model
          === 'alpha',
      }}));
    """)
    assert out["cbmN"] >= 30, out
    assert out["selN"] >= 30, out
    assert out["originals"] == 3, (
        "every original model name survives the expansion unchanged")
    assert out["firstIsAlpha"], out


def test_agent_keyed_lists_scale_like_model_lists():
    """#651's category: the agent-role identity drives the height check
    the same way the model identity does, so a panel cannot hide behind
    a differently-named identity field."""
    out = _node(f"""
      const v = mod.variantsFrom({json.dumps(BASE)});
      const ids = a => [...new Set(a.map(r => r.agent_type))];
      console.log(JSON.stringify({{
        twoN: ids(v.two['cost_by_agent.json'].agents).length,
        manyN: ids(v.many['cost_by_agent.json'].agents).length,
        longLast: v.many['cost_by_agent.json'].agents.some(
          r => r.agent_type.includes('tooltip-overflow')),
      }}));
    """)
    assert out["twoN"] == 2, out
    assert out["manyN"] >= 30, out
    assert out["longLast"], out


def test_the_last_expanded_copy_carries_a_long_label():
    """#642's overflow half never fires on the frozen fixtures' labels;
    the long identity the rewriter appends is the ONE fixture case that
    exercises the overflow assertion at every run."""
    out = _node(f"""
      const v = mod.variantsFrom({json.dumps(BASE)});
      const longs = [v.many['dashboard.json'].cost_by_model,
        v.many['cost_by_agent.json'].agents].map(doc =>
          doc.some(r => String(r.model ?? r.agent_type)
            .includes('a-very-long-identity-name')));
      console.log(JSON.stringify({{ longs }}));
    """)
    assert out["longs"] == [True, True], out


def test_lists_without_a_model_field_pass_through_untouched():
    out = _node(f"""
      const v = mod.variantsFrom({json.dumps(BASE)});
      console.log(JSON.stringify({{
        hourly: v.many['dashboard.json'].hourly,
        buckets: v.two['tool_usage.json'].buckets,
        me: v.many['me.json'],
      }}));
    """)
    assert out["hourly"] == BASE["dashboard.json"]["hourly"], out
    assert out["buckets"] == BASE["tool_usage.json"]["buckets"], out
    assert out["me"] == BASE["me.json"], out


# --- the wiring -------------------------------------------------------

def test_the_marks_are_pinned_at_source():
    """`data-hover-target` is how the sweep finds its targets; a mark
    deleted from a panel silently drops that panel from the sweep — the
    run prints NO TARGETS but stays green. The source pins hold the
    marks in place; the run's own NO TARGETS print is the operator's
    view of what they cover."""
    charts = (ROOT / "src" / "dashboard-charts.jsx").read_text(encoding="utf-8")
    ttl = (ROOT / "src" / "cache-ttl-panel.jsx").read_text(encoding="utf-8")
    extra = (ROOT / "src" / "dashboard-charts-extra.jsx").read_text(
        encoding="utf-8")
    assert charts.count("data-hover-target") >= 3, (
        "the time-series, hbar and vbar bars lost their hover marks")
    assert ttl.count("data-hover-target") >= 2, (
        "the TTL panel's 1h/5m bars lost their hover marks")
    assert extra.count("data-hover-target") >= 1, (
        "the context-size bars lost their hover marks")
    assert 'data-list-panel' in charts, (
        "the bar-list marker is gone; the height check would fail the "
        "list panels for growing with their own entries")
    assert 'listPanel' not in extra, (
        "dashboard-charts-extra carries the agent-type bar panels "
        "(#651's subject) — a listPanel prop there would exempt the "
        "one panel the height category exists to fail")


def test_the_workflow_runs_the_sweep():
    src = WORKFLOW.read_text(encoding="utf-8")
    assert "node scripts/ci/panel_interactions.mjs" in src, (
        "panel-layout.yml no longer runs the interaction sweep")


def test_both_guards_share_one_server_module():
    """Two copies of the /api fixture table is one drift away from a
    guard that answers an endpoint the other never heard of."""
    assert "from './panel_server.mjs'" in LAYOUT.read_text(encoding="utf-8")
    assert "from './panel_server.mjs'" in MODULE.read_text(encoding="utf-8")
    assert "const API = {" not in LAYOUT.read_text(encoding="utf-8"), (
        "panel_layout.mjs grew a private copy of the /api fixture table")


def test_the_tooltip_overflow_oracle_stays_pinned():
    """The sweep never reproduces #642's tooltip-overflow half (its flip
    logic keeps tooltips inside the viewport at these fixtures), so no
    ledger entry keeps this check alive — a deleted check would be
    invisible. The source pin is its liveness proof."""
    src = MODULE.read_text(encoding="utf-8")
    assert "tip.scrollWidth > tip.clientWidth + 1" in src
    assert "r.right > vw + 0.5" in src
