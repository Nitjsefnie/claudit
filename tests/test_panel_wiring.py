"""Source-level guards for two JSX wiring mistakes that render silently.

Neither is catchable by the rest of the suite: node cannot parse JSX and
nothing here renders React, so a panel can pass every test while drawing
nothing (see test_vbar_label_geometry.py's note on that boundary). Both
of these shipped in the Cost by Context panel and were found by a human
looking at the screen, which is the failure mode these assertions close.

1. `COL` is keyed by SERIES NAME. Indexing it numerically yields
   undefined, and an SVG <rect fill={undefined}> renders BLACK while a
   <path stroke={undefined}> renders nothing -- no error, no warning.
2. `DashTooltip` takes ONE `tip` prop. Spreading the tip object instead
   leaves `tip` undefined, so its `if (!tip) return null` fires on every
   hover and the panel is silently unhoverable.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHARTS = ROOT / "src" / "dashboard-charts.jsx"
EXTRA = ROOT / "src" / "dashboard-charts-extra.jsx"


def _strip_line_comments(src: str) -> str:
    """Drop `//` line comments so prose ABOUT a mistake is not read as the
    mistake. The lookbehind spares `https://` (a colon precedes those
    slashes), which is the only // that shows up mid-expression here.
    """
    return re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)


def _col_keys() -> set[str]:
    """The keys actually defined on `const COL = {...}`."""
    src = CHARTS.read_text(encoding="utf-8")
    m = re.search(r"^const COL = \{(.*?)^\};", src, re.S | re.M)
    assert m, "could not locate `const COL = {...}` in dashboard-charts.jsx"
    return set(re.findall(r"^\s*(\w+):", m.group(1), re.M))


def test_col_palette_is_never_indexed_numerically():
    """COL_X[0] is undefined -> a black bar, not an error."""
    for path in (CHARTS, EXTRA):
        hits = re.findall(
            r"\bCOL(?:_X)?\[\s*\d+\s*\]",
            _strip_line_comments(path.read_text(encoding="utf-8")))
        assert not hits, f"{path.name} indexes the COL palette numerically: {hits}"


def test_every_referenced_palette_key_exists():
    """A typo'd key fails exactly like a numeric index: undefined fill."""
    known = _col_keys()
    assert known, "COL parsed as empty - the guard would pass vacuously"
    used = set(re.findall(
        r"\bCOL(?:_X)?\.(\w+)",
        _strip_line_comments(EXTRA.read_text(encoding="utf-8"))))
    unknown = used - known
    assert not unknown, f"panels reference undefined COL keys: {sorted(unknown)}"


def test_dash_tooltip_is_passed_the_tip_prop_not_a_spread():
    """`<DashTooltip {...tip} />` leaves the `tip` prop undefined, so the
    component returns null and the panel never shows a tooltip."""
    src = EXTRA.read_text(encoding="utf-8")
    uses = re.findall(r"<window\.DashTooltip\s+([^/>]*)/>", src)
    assert uses, "no DashTooltip usages found - the guard would pass vacuously"
    bad = [u.strip() for u in uses if "tip={tip}" not in u]
    assert not bad, f"DashTooltip called without tip={{tip}}: {bad}"


def test_cumulative_line_is_anchored_to_bucket_edges():
    """A cumulative curve over bins reaches its value at the bin's UPPER
    bound, so plotting it at the bin centre states it half a bucket early
    — and leaves the line floating inside the bars instead of spanning
    them. Pin the edge anchoring: start at the plot's left edge with a
    zero, then step to each bucket's right edge.
    """
    src = _strip_line_comments(EXTRA.read_text(encoding="utf-8"))
    m = re.search(r"const cumPath = React\.useMemo\(\(\) => \{(.*?)\}, \[",
                  src, re.S)
    assert m, "could not locate the cumPath builder"
    body = m.group(1)
    assert "bw / 2" not in body, (
        "cumPath centre-anchors its points; a cumulative value belongs on "
        "the bucket's right edge")
    assert "(i + 1) * bw" in body, "cumPath must step to each bucket's right edge"
    assert re.search(r"\$\{padL\},\$\{yShare\(0\)\}", body), (
        "cumPath must start at the left edge of the first bar at zero")


def _panel_src(name: str) -> str:
    """Just ONE component's body.

    Searching the whole file is how a guard silently checks the wrong
    panel: several of them define an `onMove`, and a bare regex takes
    whichever appears first. Slice from the component to the next
    top-level `function`/`window.` and search inside that.
    """
    src = _strip_line_comments(EXTRA.read_text(encoding="utf-8"))
    start = src.index(f"function {name}(")
    nxt = re.search(r"^(?:function |window\.)", src[start + 1:], re.M)
    end = start + 1 + nxt.start() if nxt else len(src)
    return src[start:end]


def test_cost_by_context_replicates_the_reference_mark_treatment():
    """Cost by Context is the same shape as TimeSeriesPanel's Cost (USD)
    -- cost bars with a cumulative line over them -- so it uses that
    panel's treatment rather than a hand-rolled one.

    The mechanism matters, not just the look. A cumulative line in a
    CONTRASTING hue has to out-lighten the bars and stay visible on the
    dark surface at the same time, and nothing does both: the first
    attempt scored 1.07:1 against the bars and was invisible. The
    reference solves it without a second colour -- a dim field (bars at
    0.3, lifted to 0.85 on hover) under a same-hue line ringed in white
    at 0.15 opacity. Pin all three; dropping the halo or brightening the
    bars quietly reintroduces the original bug.
    """
    ref = _strip_line_comments(CHARTS.read_text(encoding="utf-8"))
    src = _panel_src("CostByContextPanel")

    # The reference's own values, so this tracks it instead of freezing a
    # copy of numbers that may move.
    assert 'stroke="#fff" strokeOpacity="0.15" strokeWidth="4"' in ref, (
        "TimeSeriesPanel no longer haloes its cumulative line -- reread it "
        "before changing the panel that copies it")
    assert "fillOpacity={isHover ? 0.85 : 0.3}" in ref

    # The opacity constants sit at module scope, above the component --
    # uniquely named, so the whole file is the right place to read them.
    whole = _strip_line_comments(EXTRA.read_text(encoding="utf-8"))
    rest = re.search(r"const BAR_OPACITY = ([\d.]+)", whole)
    hover = re.search(r"const BAR_OPACITY_HOVER = ([\d.]+)", whole)
    assert rest and hover, "could not read the panel's bar opacities"
    assert float(rest.group(1)) <= 0.35, (
        f"bars at {rest.group(1)} are too bright a field for a same-hue "
        f"line to read over")
    assert float(hover.group(1)) > float(rest.group(1)), (
        "hover must brighten the bar, as the reference does")

    assert 'stroke="#fff" strokeOpacity="0.15" strokeWidth="4"' in src, (
        "the cumulative line lost its white halo -- it is what makes a "
        "same-hue line legible over the bars")
    assert 'data-cumulative-line=""' in src


def test_cost_by_context_hover_matches_the_reference():
    """Hover is on the CONTAINER (the tooltip's offsetParent) and guarded
    to the plot area, so the tip does not appear over the header or the
    x-axis gutter -- both straight from TimeSeriesPanel."""
    src = _panel_src("CostByContextPanel")
    m = re.search(r"function onMove\(e\) \{(.*?)\n  \}", src, re.S)
    assert m, "could not locate the panel's hover handler"
    body = m.group(1)
    assert "ref.current.getBoundingClientRect()" in body, (
        "hover must measure against the container, not the <svg>")
    assert "my < padT || my > padT + plotH" in body, (
        "hover must be guarded to the plot area")
    assert "onMouseMove={onMove}" in src and "onMouseLeave" in src


APP = ROOT / "src" / "app.jsx"

# Every token/churn panel in the main dash-grid, and the guard that must
# gate it. An UNGATED panel draws a flat empty plot for any dataset whose
# series is zero throughout -- which is not hypothetical: this codebase is
# also deployed as glmmeter over the `zai` bucket, where cache_creation,
# eph5 and eph1h are 0 across every canonical record.
_GATED_PANELS = {
    "Input Tokens": "panels.input",
    "Output Tokens": "panels.output",
    "Cache Create": "panels.cacheCreate",
    "Cache Read": "panels.cacheRead",
    "Total Tokens": "panels.any",
    "Cost (USD)": "hasSeries(events, 'cost_usd')",
    "Lines Added": "hasSeries(events, 'lines_added')",
    "Lines Deleted": "hasSeries(events, 'lines_deleted')",
}

# Panels outside the dash-grid that plot the SAME cache-create tokens, so
# they go dark for exactly the datasets the grid's Cache Create panel does.
# CacheTTLPanel splits cache_create into its 5m/1h tiers: with no cache
# creation at all it draws two empty series and a 5m-share strip over
# nothing, which is what glmmeter renders today.
_GATED_ELEMENTS = {
    "window.CacheTTLPanel": "panels.cacheCreate",
}


def test_every_dash_grid_panel_is_gated_on_having_data():
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    for title, guard in _GATED_PANELS.items():
        idx = src.index(f'title="{title}"')
        # The guard sits immediately before the element that carries the
        # title, so look back a short window rather than the whole file.
        window = src[max(0, idx - 220):idx]
        assert guard in window, (
            f"{title!r} panel is not gated on {guard!r} -- it will render "
            f"an empty plot when its series is zero throughout"
        )


def test_backend_token_key_sums_are_absent_safe():
    """cache_create is derived by ADDING two optional wire fields. Once
    the backend suppresses them, `undefined + undefined` is NaN, which
    poisons the panel rather than zeroing it."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    assert "(h.cache_5m_tokens || 0) + (h.cache_1h_tokens || 0)" in src
    assert "cache_create: h.cache_5m_tokens + h.cache_1h_tokens" not in src


def test_cache_ttl_panel_is_gated_on_cache_create_data():
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    for element, guard in _GATED_ELEMENTS.items():
        idx = src.index(element)
        window = src[max(0, idx - 220):idx]
        assert guard in window, (
            f"{element} is not gated on {guard!r} -- it plots cache-create "
            f"tiers and renders empty when there is no cache creation"
        )


def test_tokens_by_model_mirrors_cost_by_model():
    """Tokens by Model is Cost by Model with a different measure: the same
    HBar, the same per-model identity colours, rows sorted desc with
    zero rows dropped, and a share label that adds to 100% over the
    charted rows. Pinned at the source level (nothing here renders
    React): a copy that dropped fixedColors would lose the model colour
    the burn-rate dots and Cost by Model already carry."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    cost = src.index('title="Cost by Model"')
    tokens = src.index('title="Tokens by Model"')
    for idx in (cost, tokens):
        window = src[idx:idx + 400]
        assert "<window.HBar" in src[idx - 40:idx]
        assert "fixedColors={window.modelColors}" in window
    tokens_window = src[tokens:tokens + 400]
    assert "rows={tokensByModel}" in tokens_window
    assert "window.humanFmt(r.value" in tokens_window
    assert "tokensByModelTotal" in tokens_window
    # Folded from the same hourly events as the cost fallback, over every
    # token the model processed, not a subset of the types.
    assert ("tokensByModel[e.model] = (tokensByModel[e.model] || 0) + "
            "e.input_tokens + e.output_tokens + e.cache_create + e.cache_read") in src


def test_cost_surfaces_are_hidden_when_the_whole_range_is_free():
    """A free lane (llamameter: bonsai-2-27b at $0) charts nothing but
    zeros on every cost surface. Cost by Model draws an empty bar list,
    the cost half of Token Breakdown draws bars whose share is a 0/0
    NaN%, and the total-cost card reads $0 next to the real token
    counts. Each is gated on there being cost in view, the way the
    Cost (USD) time series already is."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    idx = src.index('title="Cost by Model"')
    assert "costByModelTotal > 0 && (" in src[max(0, idx - 260):idx]
    # The card sits in the summary row, so the guard is inline on it.
    assert "{totals.cost > 0 && <Stat label=\"total cost\"" in src
    # Tokens by Model, which measures tokens, must NOT be gated on cost:
    # a free lane charts real bars there. The window back to its own
    # element start must carry no guard (the cost panel's fmt mentions
    # costByModelTotal, which is why this looks at the guard, not the
    # identifier).
    tokens = src.index('title="Tokens by Model"')
    assert "costByModelTotal > 0 && (" not in src[max(0, tokens - 120):tokens]


def test_token_breakdown_drops_its_cost_bar_when_the_range_is_free():
    """The cost bar is dropped, not blanked: with costTotal 0 every row
    is 0 and `r.value / costTotal` is NaN, which renders "NaN%" on each
    label. The token bar stays — tokens are non-zero either way."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    idx = src.index('title="Token Breakdown — by cost"')
    assert "hasCost && (" in src[max(0, idx - 200):idx]
    assert "const hasCost = rows.some(r => r.cost > 0);" in src
    tokens_idx = src.index('title="Token Breakdown — by tokens"')
    assert "hasCost" not in src[max(0, tokens_idx - 200):tokens_idx]


def test_cost_by_agent_is_hidden_when_the_range_is_free():
    """Same rule as Cost by Model, in the panel that owns its own fetch:
    with every bar at $0 the card is a list of zeros, so it is dropped —
    while Tokens by Agent Type, which measures tokens, still renders."""
    src = _strip_line_comments(EXTRA.read_text(encoding="utf-8"))
    idx = src.index('title="Cost by Agent Type"')
    assert "total > 0 && (" in src[max(0, idx - 200):idx]
    tokens = src.index('title="Tokens by Agent Type"')
    assert "rows={tokenBars}" in src[tokens:tokens + 200]
    assert "total > 0 && (" not in src[max(0, tokens - 120):tokens]


def test_activity_heatmap_drops_its_cost_metric_when_the_range_is_free():
    """The heatmap's metric toggle offers cost; on a free lane every cell
    is $0, so the button is filtered out and the default metric falls
    back to one that has data rather than painting an empty grid."""
    src = _strip_line_comments(EXTRA.read_text(encoding="utf-8"))
    assert "const hasCost = cells.some(c => (c.cost_usd || 0) > 0);" in src
    assert "_HEAT_METRICS.filter(m => m.key !== 'cost_usd' || hasCost)" in src
    # And the selected metric cannot stay on a filtered-out button.
    assert "metric === 'cost_usd' && !hasCost" in src


def test_context_panel_renders_both_measures_from_one_component():
    """Tokens by Context Size is the SAME component as Cost by Context
    Size with measure="tokens" — not a copy. The reference treatment
    (dim bars + same-hue cumulative line under a white halo, container
    hover, rotated axis captions) is pinned by the tests above against
    one implementation, and a second copy would drift out from under
    them."""
    extra = _strip_line_comments(EXTRA.read_text(encoding="utf-8"))
    app = _strip_line_comments(APP.read_text(encoding="utf-8"))
    assert extra.count("function CostByContextPanel(") == 1
    assert "measure === 'tokens'" in extra
    assert 'window.CostByContextPanel' in extra
    # Both mounts exist, and only the cost one is gated on there being cost.
    assert app.count("<window.CostByContextPanel") == 2
    assert 'measure="tokens"' in app
    cost_mount = app.index("<window.CostByContextPanel")
    assert "hasCost && (" in app[max(0, cost_mount - 200):cost_mount]


def test_a_lone_survivor_in_a_two_column_grid_spans_the_row():
    """Hiding a cost panel leaves its partner alone in `.dash-grid-2`,
    which still reserves two columns — so Tokens by Model rendered at
    half width with an empty half beside it (measured on the live
    llamameter deploy: 826px inside a 1664px grid). A lone child spans
    the row instead."""
    css = (ROOT / "public" / "app.css").read_text(encoding="utf-8")
    assert ".dash-grid-2 > div:only-child { grid-column: 1 / -1; }" in css


def test_thinking_panel_is_gated_and_never_enters_a_total():
    """Thinking Output plots a SUBSET of Output Tokens. It gets the same
    zero-suppression gate as the other token panels, and it must not be
    added to `t.total` or to the Token Breakdown rows — both of those
    partition the billed tokens, and a subset counted there is a double
    count that inflates every derived figure."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    idx = src.index('title="Thinking Output"')
    assert "panels.thinking && (" in src[max(0, idx - 220):idx]
    assert "t.total = t.input + t.output + t.cc + t.cr;" in src
    assert "t.thinking" not in src.split("t.total =")[1][:200]
    breakdown = src[src.index("function computeTokenBreakdown"):]
    breakdown = breakdown[:breakdown.index("\n}")]
    assert "thinking" not in breakdown


def test_browser_prices_an_undeclared_ttl_at_the_1h_rate():
    """The browser re-derives cost in two places (the synthetic preview's
    per-event cost and Token Breakdown's per-type split). Both must price
    a cache write with no declared TTL the way backend/pricing does — at
    the 1h rate — or the breakdown disagrees with the stored total it is
    meant to decompose (SV-COST-SPLIT)."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    assert "(eph1h + unsplit) * r.c1h" in src
    assert "c.ccUnsplit += unsplit                  * r.c1h * lcIn;" in src
    assert "(eph5 + unsplit) * r.c5" not in src
    assert "unsplit                  * r.c5" not in src


def test_tokens_by_project_mirrors_the_cost_panel_treatment():
    """Tokens by Project sits beside Cost by Project the way Tokens by
    Model sits beside Cost by Model: the same VBar, the same project
    filter gate, humanFmt + share-of-charted labels — but gated on
    having tokens, never on cost, so a free lane (llamameter, priced $0)
    still sees its project split."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    # Wired from the backend key, absent-safe like cost_by_project.
    assert "tokensByProject: b.tokens_by_project || []" in src
    # The whole panel is behind the project filter and a has-tokens guard.
    guard = "activeProject === '' && tokensByProject.length > 0 && ("
    assert guard in src
    idx = src.index('title="Tokens by Project"')
    assert "<window.VBar" in src[max(0, idx - 40):idx]
    assert guard in src[max(0, idx - 260):idx]
    # NOT gated on cost — the one deliberate difference from Cost by
    # Project (whose length guard doubles as its has-data gate; the cost
    # visibility rule lives on the list being non-empty).
    assert "hasCost" not in src[max(0, idx - 120):idx]
    tokens_window = src[idx:idx + 320]
    assert "rows={tokensByProject}" in tokens_window
    assert "window.humanFmt(r.value" in tokens_window
    assert "tokensByProjectTotal" in tokens_window
