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
5. the legend checkbox: its visible box stays in proportion with the 11px
   legend text and no larger than the 14px panel title (issue #474, which
   found the 24x24 target this finding installed rendering as the largest
   element in its row), and the 24x24 target SC 2.5.8 asks for is met by
   the SPACING exception instead — consecutive rows' centres at least 24px
   apart, from the shared box constant plus the containers' row gap, on
   every one of the five containers (axe target-size; the default control
   renders 13x13);
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
TOOL_PANEL = ROOT / "src" / "tool-error-panel.jsx"
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


def _legend_srcs() -> list[str]:
    """The files that may hold legend checkbox rows: dashboard-charts
    -extra.jsx and, since #652 moved the Tool Error Rate panel into its
    own module, that module too. A hand-rolled row cannot hide in
    either."""
    return [
        _strip_line_comments(EXTRA.read_text(encoding="utf-8")),
        _strip_line_comments(TOOL_PANEL.read_text(encoding="utf-8")),
    ]


def _checkbox_rows() -> list[tuple[str, str, str]]:
    """The legend row component's (label tag, input tag, file) triples.

    The seven legend rows are one shared component, so exactly one row
    shape exists across the two files; the seven <LegendCheckboxRow>
    call sites are counted separately (a future row hand-rolls it and
    the guard fires)."""
    rows = []
    for src in _legend_srcs():
        for m in re.finditer(r'<input type="checkbox"', src):
            open_lt = src.rindex("<label", 0, m.start())
            label_tag = _jsx_opening_tag(src, open_lt)
            between = src[open_lt + len(label_tag):m.start()]
            if not between.strip():
                rows.append((label_tag, _jsx_opening_tag(src, m.start()), src))
    return rows


def _legend_call_sites() -> int:
    # The bare spelling is a same-file component call; the window
    # -qualified one is how the tool module (and every other file)
    # reaches the shared row. Both are the shared component.
    return sum(src.count("<LegendCheckboxRow")
               + src.count("<window.LegendCheckboxRow")
               for src in _legend_srcs())


# -- Finding 4: legend text is never dimmed below AA -------------------

def test_legend_rows_never_dim_their_text():
    """The checkbox rows' counts ('(70)') computed 2.51:1 over --bg-card:
    the label carried `opacity: checked ? 1 : 0.6`, compositing --muted
    (4.84:1 at full strength) below the AA floor whenever a series was
    unchecked. The dimming must not touch the row's text: any `opacity`
    on the row <label> is banned outright, because no opacity over
    --bg-card keeps --muted at 4.5:1 (full opacity computes 4.84:1; even
    0.95 drops to 4.51:1 and 0.9 to 4.15:1)."""
    rows = _checkbox_rows()
    assert len(rows) == 1, (
        f"expected the 1 shared LegendCheckboxRow shape across "
        f"dashboard-charts-extra.jsx and tool-error-panel.jsx, found "
        f"{len(rows)} -- a hand-rolled legend row appeared; reuse the "
        f"shared component")
    # rows[0] after the length assert: the same single row, without the
    # sequence-balance inference pylint cannot make over the helper.
    label_tag, _, _ = rows[0]
    assert "opacity" not in label_tag, (
        f"a legend row label dims its text: {label_tag.strip()!r} -- "
        f"no opacity over --bg-card keeps --muted at 4.5:1; the on/off "
        f"affordance belongs on the checkbox itself (non-text)")
    assert _legend_call_sites() == 7, (
        f"{_legend_call_sites()} of the 7 legend rows use the shared "
        f"LegendCheckboxRow -- the guard would pass vacuously if a site "
        f"hand-rolled its label")


def test_legend_rows_keep_a_nontext_state_affordance():
    """The affordance the label opacity provided (which series are
    toggled on) survives on the checkbox itself -- a non-text element
    the AA contrast floor does not reach -- rather than on the text.

    Since issue #474 the checkbox IS the colour key, so the row holds
    exactly TWO spans (the series name and its count): the separate 10px
    swatch is gone, because a tinted checkbox beside a swatch of the
    same hue showed the series colour twice and made the 24px box the
    largest thing in an 11px row. Counting spans is the exclusive
    claim -- re-adding any swatch (or any other decorative span) makes
    the count 3 and this fires."""
    body = _panel_src("LegendCheckboxRow",
                      _strip_line_comments(EXTRA.read_text(encoding="utf-8")))
    assert body.count("<span") == 2, (
        f"the shared legend row holds {body.count('<span')} spans, expected "
        f"2 (the series name and its count) -- a swatch or another "
        f"decorative span came back, so the row shows its colour twice")
    # The checkbox, not a swatch beside it, carries the series hue: the
    # accent binding must sit on the INPUT tag itself. A substring search
    # over the whole body would also match a swatch's background.
    rows = _checkbox_rows()
    assert len(rows) == 1, (
        f"expected the 1 shared LegendCheckboxRow shape, found {len(rows)}"
        f" -- a hand-rolled legend row appeared; reuse the shared "
        f"component")
    # rows[0] after the length assert: the same single row, without the
    # sequence-balance inference pylint cannot make over the helper.
    input_tag = rows[0][1]
    assert "accentColor: color" in input_tag, (
        f"the legend checkbox does not carry the series colour: "
        f"{input_tag.strip()!r} -- the tint belongs on the input, which is "
        f"now the row's only colour key")
    # The checked state is the affordance the old label opacity gave, so
    # the row must still bind `checked` onto the control and must not
    # reintroduce an opacity anywhere (finding 4).
    assert "checked={checked}" in input_tag, (
        f"the legend checkbox no longer binds `checked`: {input_tag.strip()!r} "
        f"-- the on/off affordance is gone with the swatch's dimming")
    assert "opacity" not in body, (
        "the shared legend row dims something -- text and count hold "
        "4.84:1 over --bg-card at full strength and no opacity keeps "
        "--muted at the 4.5:1 AA floor")


# -- Finding 5: checkbox targets are at least 24x24 --------------------

# SC 2.5.8's spacing exception: an undersized target passes when the
# 24px circles centred on two targets do not MEET, so their centres
# must be 25px apart. 24 is the tangent case, where the circles touch;
# issue #474 takes the margin. Measured (headless Chromium, DPR 1): at a
# 25px separation axe is clean, at 19px it reports 46-66 violations.
_LEGEND_MIN_CENTRE_SEP_PX = 25
# The panel title the checkbox must not exceed, measured on the deployed
# Overview (issue #474's own table): 14px font.
_LEGEND_TITLE_PX = 14


def _legend_box_px(input_tag: str) -> int:
    """The legend checkbox's rendered edge, read off its inline style.

    Both dimensions must be one literal: a `width` and a `height` that
    disagree render a non-square target and the spacing arithmetic below
    is computed from the wrong edge."""
    w = re.search(r"width: (\d+)", input_tag)
    h = re.search(r"height: (\d+)", input_tag)
    assert w and h, (
        f"the legend checkbox {input_tag.strip()!r} sets no explicit px "
        f"width/height -- no target-size arithmetic is possible over the "
        f"browser's default box")
    assert w.group(1) == h.group(1), (
        f"the legend checkbox is {w.group(1)}x{h.group(1)} -- a "
        f"non-square target")
    return int(w.group(1))


def _legend_container_gaps(src: str) -> list[tuple[int, int]]:
    """Every legend container's (row, column) gap pair, in source order.

    A legend container is the BLOCK-level wrapping flex row that carries
    the legend's 11px monospace text -- `display: 'flex'` (the panel
    toolbars beside it are `inline-flex`, and a panel title is not a flex
    row at all) together with `flexWrap: 'wrap'` and `fontSize: 11`. The
    gap is read from THAT style object's own braces -- `{` back to the
    nearest one before the anchor, `}` to the first after -- so the
    properties may appear in any order and no sibling container's `gap`
    can leak in. Anchoring on the property ORDER instead was a false
    green: a legend container written `display, gap, flexWrap` with a
    genuine 6px row gap left the spacing pin green, because the regex
    never matched it at all and the count of five was met by four.
    """
    pairs = []
    for m in re.finditer(re.escape("flexWrap: 'wrap'"), src):
        open_brace = src.rindex("{", 0, m.start())
        close_brace = src.index("}", m.end())
        style = src[open_brace:close_brace]
        if "display: 'flex'" not in style or "fontSize: 11" not in style:
            continue  # an inline-flex toolbar, not a legend container
        g = re.search(r"gap: '(\d+)px(?:\s+(\d+)px)?'", style)
        assert g, (
            f"a legend container sets no explicit px gap: {style.strip()!r} "
            f"-- its rows stack at the spacing exception's threshold")
        row, col = g.group(1), g.group(2) or g.group(1)
        pairs.append((int(row), int(col)))
    return pairs


def test_legend_checkboxes_are_proportionate_to_the_legend_text():
    """The visible box was 24x24 while the legend text beside it is 11px
    and the panel title above it 14px (measured, deployed Overview, DPR
    1), so the checkbox was the largest element in its row. The box must
    not exceed the title, and both edges must come from one literal."""
    assert len(_checkbox_rows()) == 1, (
        "expected the 1 shared LegendCheckboxRow shape -- a hand-rolled "
        "legend row appeared; reuse the shared component")
    assert _legend_call_sites() == 7, (
        f"{_legend_call_sites()} of the 7 legend rows use the shared "
        f"component -- the guard would pass vacuously")
    for _, input_tag, _ in _checkbox_rows():
        box = _legend_box_px(input_tag)
        assert box <= _LEGEND_TITLE_PX, (
            f"the legend checkbox is {box}x{box}px -- larger than the "
            f"{_LEGEND_TITLE_PX}px panel title above it")


def test_legend_rows_keep_the_sc_2_5_8_spacing_exception():
    """With the box back at 13px the row spacing carries the rule: two
    undersized targets pass when the 24px circles centred on them do not
    meet, so consecutive rows' centres must sit at least 25px apart --
    the band height plus the container's row gap.

    This is the load-bearing half of the fix and it is measured, not
    assumed: at a 6px gap the centres measured 19px and axe reported 46-66
    `target-size` violations on a wrapped legend, from 1718px down to
    320px; at 14 they measure 27px and axe is clean at every width, and a
    25px separation is clean too (issue #474, headless Chromium, DPR 1).

    The band is taken as the checkbox's own edge. The legend text's line
    box is the same 13px, and any taller child (a longer wrapped name)
    only pushes the band UP, so this is the conservative direction.

    The COLUMN gap is controlled too, not left as an uncontrolled limb
    of the same literal. Two checkboxes in one row are separated by the
    row gap, the label's text, and the column gap; with the text
    measured at zero width the floor on their centre separation is
    `box + column`. Asserting that floor rather than the measured 37px
    can only fail when the geometry is definitely too tight -- a real
    label is always wider than nothing -- so it never passes a layout
    that violates the exception."""
    srcs = _legend_srcs()
    rows = _checkbox_rows()
    assert len(rows) == 1, (
        "expected the 1 shared LegendCheckboxRow shape -- a hand-rolled "
        "legend row appeared; reuse the shared component")
    box = _legend_box_px(rows[0][1])
    gaps = [g for src in srcs for g in _legend_container_gaps(src)]
    # Seven call sites share six containers (ToolUsagePanel renders one
    # container for its per-tool rows and its Other row; the tool panel
    # renders the model row and the tool picker).
    assert len(gaps) == 6, (
        f"{len(gaps)} of the 6 legend containers read as a wrapping flex "
        f"row with an explicit px gap -- one moved or lost its spacing")
    for row_gap, col_gap in gaps:
        centres = box + row_gap
        assert centres >= _LEGEND_MIN_CENTRE_SEP_PX, (
            f"a legend container spaces its rows {row_gap}px, putting row "
            f"centres {centres}px apart ({box}px box + {row_gap}px gap), "
            f"under the {_LEGEND_MIN_CENTRE_SEP_PX}px SC 2.5.8 spacing "
            f"exception -- their target circles overlap and axe "
            f"target-size fails")
        same_row = box + col_gap
        assert same_row >= _LEGEND_MIN_CENTRE_SEP_PX, (
            f"a legend container spaces its columns {col_gap}px, putting "
            f"same-row centres at a {same_row}px floor ({box}px box + "
            f"{col_gap}px gap) before the series name between them -- under "
            f"the {_LEGEND_MIN_CENTRE_SEP_PX}px SC 2.5.8 spacing exception")


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
    for panel in ("ToolUsagePanel", "ActivityHeatmapPanel",
                  "CostByContextPanel"):
        body = _panel_src(panel, src)
        assert _has_flexwrap(body), (
            f"{panel} carries no flexWrap: 'wrap' anywhere -- one of its "
            f"rows refuses to wrap and overflows 320 px")
        # Both halves of the header: the flex row holding title+toolbar,
        # and the toolbar itself.
        header_m = re.search(
            r"borderBottom: `1px solid \$\{TH_X\.border\}`, display: 'flex'"
            r", alignItems: 'center', gap: 16[^}]*", body)
        assert header_m, (
            f"{panel}'s header row moved -- relocate this guard with it")
        assert _has_flexwrap(header_m.group(0)), (
            f"{panel}'s header row does not wrap -- the toolbar cannot "
            f"drop below the title and overflows 320 px")
        # The panel's model-select toolbar: the inline-flex row holding
        # the toggle buttons and the filter.
        tool_m = re.search(
            r"display: 'inline-flex'[^{}]*gap: 6,[^{}]*", body)
        assert tool_m, (
            f"{panel}'s toolbar moved -- relocate this guard with it")
        assert "flexWrap: 'wrap'" in tool_m.group(0), (
            f"{panel}'s toolbar does not wrap -- its buttons plus model "
            f"select overflow 320 px")
    # Token Breakdown's model filter sits in a justify-end flex row in
    # app.jsx with the same defect.
    app = _strip_line_comments(APP.read_text(encoding="utf-8"))
    body = _panel_src("TokenBreakdownPanel", app)
    row_m = re.search(
        r"display: 'flex', alignItems: 'center', justifyContent: "
        r"'flex-end'[^{}]*", body)
    assert row_m, "TokenBreakdownPanel's filter row moved; relocate this guard"
    assert "flexWrap: 'wrap'" in row_m.group(0), (
        "TokenBreakdownPanel's model filter row does not wrap -- it "
        "overflows 320 px on a phone-width window")


def test_context_growth_panel_is_one_chart_on_a_measured_width():
    """The per-model sub-panel grid is gone (#649): the panel measures
    its own box (its chart takes the card's width, whatever the
    viewport) and renders exactly one ComparisonRow, with no per-model
    cell geometry left to overflow a 320 px card. The two mechanisms
    that failed there (#395: a prop-sized svg inside a flexed cell, and
    a row that refused a second line) belonged to the grid's cells; with
    the grid gone the shape itself is the guard."""
    src = _strip_line_comments(EXTRA.read_text(encoding="utf-8"))
    assert "ContextSubPanel" not in src, (
        "the per-model sub-panel grid is back -- relocate the 320 px "
        "guards onto it (#649)")
    start = src.index("function ContextGrowthPanel(")
    nxt = re.search(r"^(?:function |window\.)", src[start + 1:], re.M)
    panel = src[start:start + 1 + nxt.start()] if nxt else src[start:]
    assert "cellW" not in panel and "cellH" not in panel, (
        "per-model cell geometry survives the grid's removal")
    assert "ResizeObserver(" in panel, (
        "ContextGrowthPanel no longer measures its own box -- the chart "
        "width comes from somewhere else; relocate this guard with it")
    assert panel.count("<window.ComparisonRow") == 1, (
        "the comparison chart is not mounted exactly once")


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
