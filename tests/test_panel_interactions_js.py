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
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "ci" / "panel_interactions.mjs"
LAYOUT = ROOT / "scripts" / "ci" / "panel_layout.mjs"
SERVER = ROOT / "scripts" / "ci" / "panel_server.mjs"
WORKFLOW = ROOT / ".github" / "workflows" / "panel-layout.yml"


def _node(body: str):
    """Import the guard in node and evaluate `body` against its exports.

    Over STDIN with --input-type=module: the payload embeds paths, and
    Windows refuses a CreateProcess command line over 32k. This import
    is itself an assertion — the guard's main() is gated on direct
    execution, so an import that launched Chromium would never return.
    """
    # A file:// URI, not a bare path: on Windows a bare path imports as
    # the `d:` scheme and the ESM loader refuses it
    # (ERR_UNSUPPORTED_ESM_URL_SCHEME); as_uri() spells the URI right on
    # every platform.
    url = MODULE.as_uri()
    script = (
        f"const mod = await import({json.dumps(url)});\n"
        f"{body}\n"
    )
    proc = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True,
        text=True, timeout=60, check=False, encoding="utf-8", errors="strict",
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


# --- the KNOWN ledger -------------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_every_ledger_entry_fires_on_the_defect_it_names():
    """Each entry must match a synthetic finding of its kind — an entry
    that matches nothing is dead weight the stale-entry check would
    fail the next run on."""
    out = _node("""
      // A panel name each entry's matcher genuinely admits; null when a
      // regex matches neither name this test knows, which is itself a
      // ledger defect the assertion below names.
      const known = ['Cost by Context Size', 'Input Tokens',
        'Prompt-Cache TTL Split', 'Cost by Model'];
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_liveness_is_keyed_per_entry_not_per_issue_kind_pair():
    """Two entries may share an issue and a kind -- #651's twins were
    the motivating case -- and the matcher must hand back the ENTRY, so
    the sweep's liveness set can prove each twin fired on its own; a
    matcher that collapses to the issue/kind pair lets one twin's
    firing stand as the other's. The live ledger is empty now that #651
    is fixed, so the pin drives synthetic twin entries and puts the
    ledger back the way it found it."""
    out = _node("""
      const a = { issue: 4242, panel: 'Cost by Agent Type',
        kind: 'height-growth' };
      const b = { issue: 4242, panel: 'Tokens by Agent Type',
        kind: 'height-growth' };
      const c = { issue: 4243, panel: null, kind: 'hover-style' };
      mod.FILED.push(a, b, c);
      const fa = mod.filedEntry('Cost by Agent Type', 'height-growth');
      const fb = mod.filedEntry('Tokens by Agent Type', 'height-growth');
      const fc = mod.filedEntry('Anything At All', 'hover-style');
      const emptyAgain = (mod.FILED.pop() === c && mod.FILED.pop() === b
        && mod.FILED.pop() === a && mod.FILED.length === 0);
      console.log(JSON.stringify({
        distinctScopes: fa !== fb && fa !== null && fb !== null,
        sameIssuePair: fa.issue === 4242 && fb.issue === 4242,
        nullPanelMatches: fc === c,
        emptyAgain,
      }));
    """)
    assert out == {"distinctScopes": True, "sameIssuePair": True,
                   "nullPanelMatches": True, "emptyAgain": True}, out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
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


# --- the height comparison (#694) -------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_one_pixel_wobble_passes_the_height_check():
    """The two height renders are separate headless contexts: Chromium's
    sub-pixel rounding is not reproducible between them, and the exact
    comparison reds a one-pixel wobble (#694 hit master as 339px ->
    338px at 1024px — a shrink failing a growth check). ±1px agrees."""
    out = _node("""
      console.log(JSON.stringify({
        equal: mod.heightsAgree(339, 339),
        up1: mod.heightsAgree(339, 340),
        down1: mod.heightsAgree(339, 338),
      }));
    """)
    assert out == {"equal": True, "up1": True, "down1": True}, out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_one_row_growth_or_shrink_still_fails():
    """A real per-entry row growth is many pixels — one bar row is tens
    of pixels — and the noise band must never stretch that far. A gross
    disagreement fails in BOTH directions: the panel is not rendering
    the same page in the two variants at all. The ±2px probes hold the
    band's refusal side at its nearest boundary: a silently doubled
    HEIGHT_NOISE_PX turns these True and reds the test."""
    out = _node("""
      console.log(JSON.stringify({
        grow: mod.heightsAgree(339, 364),
        shrink: mod.heightsAgree(339, 314),
        nearGrow: mod.heightsAgree(339, 341),
        nearShrink: mod.heightsAgree(339, 337),
      }));
    """)
    assert out == {"grow": False, "shrink": False,
                   "nearGrow": False, "nearShrink": False}, out


def test_the_height_loop_compares_through_the_noise_band():
    """Source pin in the tooltip-oracle's style: the height loop must
    compare through heightsAgree, so the exact-equality regression that
    reds on a 1px wobble (#694) cannot return while this file passes."""
    src = MODULE.read_text(encoding="utf-8")
    assert "if (heightsAgree(two.get(name), hMany)) continue;" in src, (
        "the height loop must compare through heightsAgree (#694's noise "
        "band), not raw equality")


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
        "dashboard-charts-extra must carry no listPanel prop: the height "
        "category's subject, the agent-type bar panels, is exempted by "
        "one (#651) -- in this module or in "
        "src/cost-by-agent-panel.jsx, where the panels now live")


def test_the_agent_panel_declares_a_bound_the_guard_reads():
    """#651's fixed bound, pinned on both sides of the shared literal:
    the panel card declares data-max-h and never a listPanel prop (that
    would exempt the one panel the height category exists to fail), and
    the guard reads the bound off the nearest [data-max-h] ancestor.
    Drop either half and a rename on the other silently disarms the
    check; the rendered leg proves the pair live on every CI run."""
    panel = ROOT / "src" / "cost-by-agent-panel.jsx"
    assert panel.exists(), "the #651 panel module is missing"
    src = panel.read_text(encoding="utf-8")
    assert "data-max-h={" in src, (
        "the agent panel declares no data-max-h bound; the guard has "
        "nothing to enforce")
    assert "listPanel" not in src, (
        "the agent panel carries a listPanel prop -- that would exempt "
        "the one panel the height category exists to fail")
    guard = MODULE.read_text(encoding="utf-8")
    assert "closest('[data-max-h]')" in guard, (
        "the guard no longer reads a panel's data-max-h bound")
    assert "&& breachesBound(h, bound)" in guard, (
        "the guard no longer feeds declared bounds through the "
        "comparison the node tests drive -- the && prefix names the "
        "CALL SITE, not the definition, which would satisfy its own "
        "pin")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_bound_comparison_fires_by_near_miss():
    """The bounded branch's pinnable half: the comparison a declared
    bound feeds must fire above the ceiling and hold at and under it,
    with the attribute's string shape and the measured integer. Nothing
    else proves the branch can fire -- the healthy page never breaches
    (338px under its 364px bound), so a dead or flipped comparison
    would green every run while enforcing nothing."""
    out = _node("""
      console.log(JSON.stringify({
        above: mod.breachesBound(365, '364'),
        atBound: mod.breachesBound(364, '364'),
        under: mod.breachesBound(122, '364'),
      }));
    """)
    assert out == {"above": True, "atBound": False, "under": False}, out


def test_the_expand_toggle_gates_on_the_collapsed_fold():
    """The toggle must stay mounted while expanded, or nothing can
    collapse the list again: an expanded cap hides nothing, so a gate
    on the CURRENT fold's hidden count unmounts the button the moment
    it is used. The gate reads the fold at the cap; the label switch
    reads expanded. Pinned at source -- node cannot render JSX and the
    rendered leg never clicks."""
    src = (ROOT / "src" / "cost-by-agent-panel.jsx").read_text(
        encoding="utf-8")
    assert "collapsedHidden > 0" in src, (
        "the toggle's gate no longer reads the fold at the cap; a gate "
        "on the current fold unmounts the expanded toggle")
    assert "hiddenCount > 0" not in src, (
        "the gate reads the current fold's hidden count -- the exact "
        "spelling that unmounts the expanded toggle")


def test_cost_by_agent_is_hidden_when_the_range_is_free():
    """Moved from test_panel_wiring.py with its subject: the agent panel
    now lives in src/cost-by-agent-panel.jsx (#651). Same rule as Cost
    by Model, in the panel that owns its own fetch: with every bar at
    $0 the cost list is a list of zeros, so it is dropped, while
    Tokens by Agent Type, which measures tokens, still renders."""
    src = (ROOT / "src" / "cost-by-agent-panel.jsx").read_text(
        encoding="utf-8")
    idx = src.index('title="Cost by Agent Type"')
    assert "total > 0 && (" in src[max(0, idx - 200):idx]
    tokens = src.index('title="Tokens by Agent Type"')
    assert "rows={tokenCap.rows}" in src[tokens:tokens + 200]
    assert "total > 0 && (" not in src[max(0, tokens - 120):tokens]


def test_the_agent_panel_modules_are_loaded_by_the_page():
    """A script tag someone drops fails no layout check -- the guard
    counts the panels that render, never the ones that should -- so the
    two #651 modules are load-pinned like every other panel module."""
    index = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    assert '/src/agent-list-caps.js' in index, (
        "agent-list-caps.js is not loaded by the page, so "
        "window.agentListCaps is undefined at render and the panel "
        "throws")
    assert '/src/cost-by-agent-panel.jsx' in index, (
        "cost-by-agent-panel.jsx is not loaded by the page, so the "
        "Cost by Agent Type panel silently disappears from the Overview")


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
    """The overflow check has two halves and two liveness controls: CI's
    font stack fires the own-box scroll half against the probe identity,
    and the rendered sweep asserts it on every by-Model hover — delete
    the check and a real overflow fails the run. The source pin
    here holds the second half (the viewport comparison, which CI's
    fixtures do not reproduce) so neither half can be dropped without
    this file naming it."""
    src = MODULE.read_text(encoding="utf-8")
    assert "tip.scrollWidth > tip.clientWidth + 1" in src
    assert "r.right > vw + 0.5" in src


# --- the #701 long-key row probe --------------------------------------

def test_both_primitives_render_identical_row_spans():
    """#701's shape: the two tooltip primitives lay their rows out with
    the SHARED .chart-tooltip-key / .chart-tooltip-val rules in
    public/app.css, and carry no inline sizing styles — that per-
    primitive divergence was the defect. The spans must stay
    byte-identical across the two modules. The rendered long-key probe
    pins the BEHAVIOR the contract names — a key longer than the box
    wraps inside it because the row may shrink — and any shrink-lock,
    inline or CSS-side, turns it red. It cannot pin WHICH rule supplies
    the wrap: the box's own overflow-wrap inherits into the row, so a
    delete of the key rule's copy alone renders green by equivalence."""
    tags = {}
    for fname in ("dashboard-charts.jsx", "dashboard-charts-extra.jsx"):
        src = (ROOT / "src" / fname).read_text(encoding="utf-8")
        for cls in ("chart-tooltip-key", "chart-tooltip-val"):
            found = re.findall(rf'<span className="{cls}"[^>]*>', src)
            assert len(found) == 1, (
                f"{fname}: expected exactly one {cls} span, found "
                f"{len(found)}")
            tags[(fname, cls)] = found[0]
    for cls in ("chart-tooltip-key", "chart-tooltip-val"):
        assert tags[("dashboard-charts.jsx", cls)] == tags[
            ("dashboard-charts-extra.jsx", cls)], (
            f"the two tooltip primitives' {cls} spans diverge — the row "
            "layout lives in the shared .chart-tooltip-* CSS rules, "
            "never inline on one primitive (#701)")


def test_the_guard_probes_a_rendered_long_key_row():
    """The rendered half: the sweep's real tooltips draw only short
    fixed keys, so the probe — the real primitive mounted with a key
    longer than the box — is the ONE fixture case that renders a long
    key at every run. Drop it and #701's row contract loses its
    rendered witness; the byte-identical pin above still holds the
    source half. The oracle strings are asserted inside the LONGKEY
    body's own slice: the sweep's READ carries byte-identical copies,
    and a whole-source assert would be satisfied by that sibling while
    the probe's own copy drifted."""
    src = MODULE.read_text(encoding="utf-8")
    probe = src[src.index("const LONGKEY"):src.index("async function main")]
    assert "window.DashTooltip" in probe, (
        "the guard no longer mounts the real DashTooltip primitive for "
        "the long-key probe — #701's rendered row witness is gone")
    assert "tip.scrollWidth > tip.clientWidth + 1" in probe
    assert "r.right > vw + 0.5" in probe
    assert src.count("(long-key probe)") == 1, (
        "the long-key probe finding lost its ledger name, or a second "
        "site now answers for it")


# --- #690: coverage is never opt-in -----------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_no_target_kind_is_in_the_closed_kind_table():
    """A panel rendered with data but no [data-hover-target] is the
    sweep's own finding kind — printed NO TARGETS is the #690 blindness
    and may not come back."""
    out = _node("""
      console.log(JSON.stringify({
        kinds: mod.KINDS,
        seeds: mod.SEEDS,
      }));
    """)
    assert "no-targets" in out["kinds"], out
    # The sweep fails an unmarked non-static panel and refuses a static
    # declaration that covers marked targets; both sites stay in source.
    src = MODULE.read_text(encoding="utf-8")
    assert "renders with data but carries no [data-hover-target]" in src
    assert "data-static-panel but renders marked targets" in src


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_ledger_never_carries_an_other_region_catch_all():
    """#690's second bullet: every layout shift, cold-load included,
    resolves to a named region or panel. The old escape — a catch-all
    `{ panel: null, kind: 'other-region' }` entry that matched every
    unnamed attribution and could never fail — is banned outright: a
    future `other` shift must fail the run, not match a ledger line."""
    out = _node("""
      console.log(JSON.stringify(
        mod.FILED.filter(f => f.kind === 'other-region'
          && (f.panel === null || f.panel === 'other' || f.panel === '(sweep)'
            || f.panel === '(cold load)'))));
    """)
    assert out == [], out


def test_the_static_panels_declare_their_exemption():
    """The two panels with no interactive surface by design — the
    heatmap's gradient legend and the Page performance stat panel —
    declare `data-static-panel`, the only path the sweep's no-targets
    check exempts. A third declaration belongs only on a genuinely
    static panel; the sweep fails a static panel that renders marks."""
    for fname in ("activity-heatmap-panel.jsx", "perf-panel.jsx"):
        src = (ROOT / "src" / fname).read_text(encoding="utf-8")
        assert 'data-static-panel=""' in src, (
            f"{fname} no longer declares data-static-panel — the sweep "
            "would fail it as a data panel with no hover target")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_seeded_violations_are_wired_end_to_end():
    """Each seed the guard exports is driven once by the seeds runner,
    which asserts exit 1 per seed and greps the finding kind the seed
    exists to prove; the workflow runs it beside the sweep. A seed
    without the runner leg is a self-check nobody runs."""
    seeds = _node("console.log(JSON.stringify(mod.SEEDS));")
    assert seeds == ["no-targets", "other-region", "cold-region"], seeds
    runner = (ROOT / "scripts" / "ci" / "panel_interactions_seeds.mjs") \
        .read_text(encoding="utf-8")
    for seed in seeds:
        assert f"'{seed}'" in runner, (
            f"the seeds runner never drives {seed}")
    # The runner greps the finding kind each seed proves, so a seed
    # failing for the wrong reason is not a proof.
    for kind in ("no-targets", "other-region"):
        assert f"'{kind}'" in runner, (
            f"the seeds runner does not verify the {kind} kind by name")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "node scripts/ci/panel_interactions_seeds.mjs" in workflow, (
        "panel-layout.yml no longer runs the seeded-violation proof")


def test_cold_load_shifts_are_attributed_in_the_guard_source():
    """#690's cold-load half at source level, beside the rendered proof:
    SHIFTS attributes the cold half like the sweep half, and the sweep
    records an other-region finding when a cold shift cannot name its
    region — the old 'counted for the log, not asserted' posture is the
    blindness the issue closes."""
    src = MODULE.read_text(encoding="utf-8")
    shifts = src[src.index("const SHIFTS"):src.index("const LONGKEY")]
    assert "cold.push(entry)" in shifts, (
        "SHIFTS no longer returns the cold half's attributed entries")
    assert "cold-load shift(s) resolve to" in src, (
        "the sweep no longer asserts the cold load's shift attribution")
