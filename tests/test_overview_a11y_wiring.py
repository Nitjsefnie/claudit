"""Source-level guards for the Overview accessibility findings (issue #395).

Same boundary as test_a11y_wiring.py and test_panel_wiring.py: node cannot
parse JSX and nothing here renders React, so a legend can dim its own text
below AA (finding 4), a checkbox can stay a 13x13 target (finding 5), a nav
button can carry its current state only as a CSS class (finding 6) and a
panel toolbar can refuse to wrap at 320 px (finding 7) while the whole
suite stays green. These guards read the sources directly and pin:

4. the Overview legend checkbox rows never dim their TEXT (the counts and
   names) through an opacity binding on the row's <label> — muted computes
   4.84:1 over --bg-card at full opacity, 2.51:1 at the old 0.6 (axe
   color-contrast);
5. every legend checkbox input carries an explicit >= 24x24 target (axe
   target-size; the default control renders 13x13);
6. each TopBar nav button carries `aria-current` bound to its own route
   equality, so the active page is exposed beyond the .on class (axe
   aria-current-valid... its absence left state class-only);
7. the CSS mechanisms that keep the Overview inside 320 px: wrapping panel
   toolbars, a wrapping sub-panel row, a cell floor that fits the 320 px
   card, and a topbar that stacks instead of overflowing its 3-column
   grid.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTRA = ROOT / "src" / "dashboard-charts-extra.jsx"
APP = ROOT / "src" / "app.jsx"
CSS = ROOT / "public" / "app.css"


def _strip_line_comments(src: str) -> str:
    """Drop `//` line comments so prose ABOUT the wiring is not read as
    the wiring. The lookbehind spares `https://` (a colon precedes those
    slashes), the only // that shows up mid-expression here."""
    return re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)


def _jsx_opening_tag(src: str, pos: int) -> str:
    """The opening tag starting at src[pos]: it ends at the first `>`
    outside {...} bindings and string literals, so `=>` inside a prop
    binding cannot end the tag early."""
    i = pos
    depth = 0
    quote = None
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "{":
            depth += 1
        else:
            if ch == ">" and depth == 0:
                break
            if ch == "}":
                depth -= 1
        i += 1
    return src[pos:i + 1]


def _panel_src(name: str, src: str) -> str:
    """One component's body: from its `function <name>(` to the next
    top-level `function`/`window.` — the test_panel_wiring approach, so
    a sibling occurrence in another panel cannot satisfy a pin."""
    start = src.index(f"function {name}(")
    nxt = re.search(r"^(?:function |window\.)", src[start + 1:], re.M)
    end = start + 1 + nxt.start() if nxt else len(src)
    return src[start:end]


def _checkbox_rows() -> list[tuple[str, str]]:
    """The legend checkbox rows as (label tag, input tag) pairs.

    A row's input is the first child of its <label>, so the row is
    located from the input: the label opening tag that immediately
    precedes it, with no markup between the label tag's end and the
    input tag's start."""
    src = _strip_line_comments(EXTRA.read_text(encoding="utf-8"))
    rows = []
    for m in re.finditer(r'<input type="checkbox"', src):
        open_lt = src.rindex("<label", 0, m.start())
        label_tag = _jsx_opening_tag(src, open_lt)
        between = src[open_lt + len(label_tag):m.start()]
        if not between.strip():
            rows.append((label_tag, _jsx_opening_tag(src, m.start())))
        else:
            # The checkbox is not its label's first child; it is not one
            # of the legend rows this guard pins.
            continue
    return rows


# -- Finding 4: legend text is never dimmed below AA -------------------

def test_legend_rows_never_dim_their_text():
    """The checkbox rows' counts ('(70)') computed 2.51:1 over --bg-card:
    the label carried `opacity: checked ? 1 : 0.6`, compositing --muted
    (4.84:1 at full strength) below the AA floor whenever a series was
    unchecked. The dimming must not touch the row's text: any `opacity`
    binding on the row <label> is banned outright, because no opacity
    over --bg-card keeps --muted at 4.5:1 (full opacity computes 4.84:1;
    even 0.95 drops to 4.51:1 and 0.9 to 3.9:1).
    """
    rows = _checkbox_rows()
    assert len(rows) == 6, (
        f"expected the 6 legend checkbox rows in "
        f"dashboard-charts-extra.jsx, found {len(rows)} -- the guard "
        f"would pass vacuously")
    for label_tag, _ in rows:
        assert "opacity" not in label_tag, (
            f"a legend row label dims its text: {label_tag.strip()!r} -- "
            f"no opacity over --bg-card keeps --muted at 4.5:1; dim the "
            f"swatch (non-text) or nothing at all")


def test_legend_rows_keep_a_nontext_state_affordance():
    """The affordance the label opacity provided (which series are
    toggled on) survives on the swatch -- a non-text element the AA
    contrast floor does not reach -- rather than on the text."""
    src = _strip_line_comments(EXTRA.read_text(encoding="utf-8"))
    rows = _checkbox_rows()
    assert rows, "no legend checkbox rows -- the guard would pass vacuously"
    dimmed = re.findall(
        r"background: [\w_]+, display: 'inline-block', borderRadius: 2, "
        r"opacity: checked \? 1 : 0\.45", src)
    assert len(dimmed) == len(rows), (
        f"{len(dimmed)} of {len(rows)} legend swatches carry the "
        f"non-text dimming -- move the label's old opacity there")


# -- Finding 5: checkbox targets are at least 24x24 --------------------

def test_legend_checkboxes_meet_the_24px_target_floor():
    """The legend checkboxes rendered at the browser default 13x13
    (axe target-size); the input's own box is the hit target, so each
    carries an explicit 24x24."""
    rows = _checkbox_rows()
    assert len(rows) == 6, (
        f"expected the 6 legend checkbox rows, found {len(rows)} -- the "
        f"guard would pass vacuously")
    for _, input_tag in rows:
        w = re.search(r"width: (\d+)", input_tag)
        h = re.search(r"height: (\d+)", input_tag)
        assert w and h, (
            f"the legend checkbox {input_tag.strip()!r} sets no explicit "
            f"size -- it renders at the 13x13 browser default, below the "
            f"24x24 target floor")
        assert int(w.group(1)) >= 24 and int(h.group(1)) >= 24, (
            f"the legend checkbox renders {w.group(1)}x{h.group(1)} -- "
            f"below the 24x24 target floor")


# -- Finding 6: the nav exposes its current page -----------------------

def test_topbar_nav_buttons_carry_aria_current():
    """Each TopBar nav button binds aria-current to its OWN route
    equality, so exactly the active page exposes current (the .on class
    alone is invisible to assistive technology)."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    start = src.index("function TopBar(")
    nxt = re.search(r"^function ", src[start + 1:], re.M)
    end = start + 1 + nxt.start() if nxt else len(src)
    topbar = src[start:end]

    buttons = re.findall(r"<button\b", topbar)
    assert len(buttons) == 4, (
        f"TopBar renders {len(buttons)} nav buttons, expected 4 -- the "
        f"guard would pass vacuously")
    for route in ("dashboard", "sessions", "cache", "session"):
        m = re.search(
            r"aria-current=\{route === '" + route + r"' \? 'page' : undefined\}",
            topbar)
        assert m, (
            f"the {route!r} nav button binds no "
            f"aria-current={{route === '{route}' ? 'page' : undefined}} -- "
            f"its active state lives only in the .on class")
    assert topbar.count("aria-current=") == 4, (
        "aria-current leaked onto a non-nav element of TopBar")


# -- Finding 7: the Overview fits 320 px -------------------------------


def _has_flexwrap(body: str) -> bool:
    return "flexWrap: 'wrap'" in body


def test_panel_toolbars_wrap_below_320():
    """Four panel toolbars refused to wrap, so at 320 px their intrinsic
    width (title + buttons + model select) pushed the document to
    scrollWidth 539 (measured, signed-in, headless Chromium): the page
    scrolled horizontally. Each toolbar and its header row must wrap."""
    src = _strip_line_comments(EXTRA.read_text(encoding="utf-8"))
    for panel, toolbar_anchor in (
        ("ToolUsagePanel", "display: 'inline-flex', alignItems: 'center', gap: 6"),
        ("ActivityHeatmapPanel", "display: 'inline-flex', alignItems: 'center', gap: 6"),
        ("ToolErrorRatePanel", "display: 'inline-flex', alignItems: 'center', gap: 6"),
    ):
        body = _panel_src(panel, src)
        assert _has_flexwrap(body), (
            f"{panel} carries no flexWrap: 'wrap' anywhere -- one of its "
            f"rows refuses to wrap and overflows 320 px")
        # Both halves of the header: the flex row holding title+toolbar,
        # and the toolbar itself.
        header_m = re.search(
            r"borderBottom: `1px solid \$\{TH_X\.border\}`, display: 'flex'"
            r", alignItems: 'center', gap: 16", body)
        assert header_m, (
            f"{panel}'s header row moved -- relocate this guard with it")
        assert _has_flexwrap(header_m.group(0)), (
            f"{panel}'s header row does not wrap -- the toolbar cannot "
            f"drop below the title and overflows 320 px")
        tool_m = re.search(re.escape(toolbar_anchor) + r"[^}]*", body)
        assert tool_m, (
            f"{panel}'s toolbar moved -- relocate this guard with it")
        assert "flexWrap: 'wrap'" in tool_m.group(0), (
            f"{panel}'s toolbar does not wrap -- its buttons plus model "
            f"select overflow 320 px")
    # Token Breakdown's model filter sits in a justify-end flex row in
    # app.jsx with the same defect.
    app = _strip_line_comments(APP.read_text(encoding="utf-8"))
    body = _panel_src("TokenBreakdownPanel", app)
    m = re.search(r"justifyContent: 'flex-end',\s*\n?\s*gap: 8", body)
    assert m, "TokenBreakdownPanel's filter row moved; relocate this guard"
    wrap_m = re.search(
        r"display: 'flex', alignItems: 'center', justifyContent: "
        r"'flex-end',\s*\n?\s*gap: 8, padding: '8px 14px 0',\s*\n?"
        r"\s*flexWrap: 'wrap'", body)
    assert wrap_m, (
        "TokenBreakdownPanel's model filter row does not wrap -- it "
        "overflows 320 px on a phone-width window")


def test_context_growth_cells_fit_a_320_card():
    """ContextGrowthPanel lays its per-model sub-panels out in a flex row
    of fixed-width cells. The 280px floor exceeded the 250px a 320 px
    card can hold (320 - 44 page padding - 2 border - 24 row padding)
    and the row refused to wrap, so the svg boxes scrolled the page.
    The floor must fit the card and the row must wrap."""
    src = _strip_line_comments(EXTRA.read_text(encoding="utf-8"))
    body = _panel_src("ContextGrowthPanel", src)
    m = re.search(r"const cellW = Math\.max\((\d+),", body)
    assert m, "ContextGrowthPanel's cellW floor moved; relocate this guard"
    floor = int(m.group(1))
    # 320 viewport - 44px .dashboard side padding - 2px card border
    # - 24px row padding = 250px of card the cell must fit into.
    assert floor <= 320 - 44 - 2 - 24, (
        f"ContextGrowthPanel's cell floor is {floor}px -- wider than the "
        f"250px a 320 px card holds, so the sub-panel svgs overflow the "
        f"viewport")
    row_m = re.search(
        r"display: 'flex', gap: 12,\s*\n\s*padding:", body)
    assert row_m, "the sub-panel row moved; relocate this guard"
    row_src = body[row_m.start():row_m.start() + 200]
    assert "flexWrap: 'wrap'" in row_src, (
        "the sub-panel row does not wrap -- two 250px cells cannot share "
        "a 250px row, so they overflow the viewport")


def test_topbar_stacks_instead_of_overflowing():
    """The topbar's 3-column grid (1fr auto 1fr) has a ~760px min-content
    (logo + 4 nav buttons + Export PNG/Logout); below that width the grid
    overflows and was the widest 320 px offender (right=539). Below 800px
    it must stack: one column, every row wrapping."""
    css = CSS.read_text(encoding="utf-8")
    m = re.search(r"@media \(max-width: 800px\)\s*\{(.*?)\n\}", css, re.S)
    assert m, (
        "no @media (max-width: 800px) block in app.css -- the topbar's "
        "3-column grid overflows below ~760px with nothing to catch it")
    block = m.group(1)
    assert re.search(r"\.topbar\s*\{[^}]*grid-template-columns: 1fr", block), (
        "the narrow topbar keeps its 3-column grid -- it overflows the "
        "viewport below ~760px")
    assert "height: auto" in block, (
        "the stacked topbar keeps height: 52px -- the stacked rows clip")
    for sel in (".topnav", ".topbar-left", ".topbar-right"):
        assert re.search(
            re.escape(sel) + r"[^{}]*\{[^}]*flex-wrap: wrap", block), (
            f"{sel} does not wrap inside the stacked topbar -- its "
            f"content overflows 320 px")
