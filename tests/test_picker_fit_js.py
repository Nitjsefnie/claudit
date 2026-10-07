"""The project picker fits its chips to the strip instead of scrolling.

Issue #774: the picker showed every project of the current page and
scrolled horizontally when they did not fit. The maintainer ruling makes
the page size measured, not fixed: only the chips that fit the strip's
current width render, the rest page through the existing prev/next
controls, and no horizontal scrollbar or overflow may remain. Nothing
below the strip may move while paging or re-fitting.

Two layers, mirroring the house split for JSX the suite cannot render:

- `src/picker-fit.js` is plain JS, driven through node here: the pure
  fit arithmetic plus the DOM read that turns a strip into numbers.
  The DOM read is node-checked with a stub element whose widths come
  from the test, so the read-mapping (what computeFit reads and what it
  reserves) is pinned, not just the arithmetic.

- The JSX glue in `src/app.jsx` is pinned at source level: the measure
  row, the pre-paint hook, the refit triggers and the overflow cut-off
  are strings the rendered guard (scripts/ci/panel_layout_rules.mjs,
  added on the #772 harness) would not see the wiring of.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIT_JS = ROOT / "src" / "picker-fit.js"
APP = ROOT / "src" / "app.jsx"
PICKER = ROOT / "src" / "picker.jsx"
INDEX = ROOT / "public" / "index.html"


def _node(body: str) -> dict:
    """Run `body` against the real src/picker-fit.js in node."""
    script = f"""
      global.window = {{}};
      require({str(FIT_JS)!r});
      {body}
    """
    # Over STDIN, not -e: the payload embeds JSON, and Windows refuses a
    # CreateProcess command line over 32k (WinError 206).
    proc = subprocess.run(
        ["node"], input=script, capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# --- the pure fit arithmetic -------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_fit_counts_how_many_chips_fit_with_gaps_and_reserve():
    """Gaps count between chips only; the reserve is consumed first."""
    out = _node("""
      const f = window.pickerFit.fitCount;
      console.log(JSON.stringify({
        all: f(1000, [100, 100, 100, 100], 6, 0),
        gap: f(300, [100, 100, 100], 6, 0),
        reserve: f(300, [100, 100, 100], 6, 100),
      }));
    """)
    assert out["all"] == 4
    # 100 + 6 + 100 = 206 fits; + 6 + 100 = 312 > 300: two chips, and the
    # gap before the one that did not fit is not consumed.
    assert out["gap"] == 2
    # The pager reserve eats one chip's worth: 300-100 = 200 room, one chip
    # (100) fits, the next step to 206 breaks.
    assert out["reserve"] == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_fit_boundary_is_exact_fits_and_over_breaks():
    """A chip whose width lands exactly on the remaining room fits; one
    pixel over does not. `>` breaks, `>=` would drop exact fits and leave
    a dead margin every load."""
    out = _node("""
      const f = window.pickerFit.fitCount;
      console.log(JSON.stringify({
        exact: f(206, [100, 100], 6, 0),
        over: f(205, [100, 100], 6, 0),
        zeroAvail: f(0, [100], 6, 0),
        zeroWidth: f(50, [0], 6, 0),
      }));
    """)
    # 100 + 6 + 100 = 206 == 206: fits.
    assert out["exact"] == 2
    # One pixel less than the pair's cost: the second step 206 > 205 breaks.
    assert out["over"] == 1
    assert out["zeroAvail"] == 0
    assert out["zeroWidth"] == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_fit_of_no_chips_is_zero():
    out = _node("console.log(JSON.stringify({ n: window.pickerFit.fitCount(100, [], 6, 0) }));")
    assert out["n"] == 0


# --- the DOM read (stubbed strip) ---------------------------------------

def _stub_strip(inner=400, gap=6, widths=(100, 100, 100), all_w=40,
                pager_core=None, jump_w=0):
    """Run computeFit in node against a stub strip the way the component
    renders it, and return the page size. The stub must fail on what it
    does not model: a selector the reader names but the stub does not
    register raises, so a renamed class in the module turns a green into
    a failure."""
    node = shutil.which("node")
    assert node, "node not available"
    payload = json.dumps({
        "clientWidth": inner, "computed": {
            "paddingLeft": "10px", "paddingRight": "22px",
            "columnGap": f"{gap}px"},
        "all": all_w + 0.5,        # fractional: offsetWidth would round it
        "widths": [w + 0.5 for w in widths],
        "pager": pager_core, "jump": jump_w,
    })
    script = f"""
      global.window = {{ getComputedStyle: () => spec.computed }};
      require({str(FIT_JS)!r});
      const spec = {payload};
      // A stub strip standing in for the real one: the reader consults
      // only querySelector/querySelectorAll/getComputedStyle and reads
      // fractional widths off getBoundingClientRect.
      function rectEl(w) {{ return {{ getBoundingClientRect: () => ({{ width: w }}) }}; }}
      const chipEls = spec.widths.map(rectEl);
      const measure = {{
        querySelectorAll: (sel) => {{
          if (sel === '.pp-proj') return chipEls;
          if (sel === '.pp-jump') return spec.jump ? [rectEl(spec.jump)] : [];
          throw new Error('unexpected selector ' + sel);
        }},
        querySelector: (sel) => (sel === '.pp-jump' && spec.jump
          ? rectEl(spec.jump) : null),
      }};
      const navs = [rectEl(26.4), rectEl(26.4)];
      const countEl = rectEl(Math.max(0, spec.pager - 52.8));
      const pagerEl = spec.pager ? {{
        querySelectorAll: (sel) => {{
          if (sel === '.pp-nav, .pp-count') return [...navs, countEl];
          throw new Error('unexpected selector ' + sel);
        }},
      }} : null;
      const strip = {{
        clientWidth: spec.clientWidth,
        querySelector: (sel) => {{
          if (sel === '.pp-measure') return measure;
          if (sel === '.pp-pager') return pagerEl;
          if (sel === '.pp-all') return rectEl(spec.all);
          throw new Error('unexpected selector ' + sel);
        }},
      }};
      console.log(JSON.stringify(window.pickerFit.computeFit(strip)));
    """
    proc = subprocess.run([node], input=script, capture_output=True, text=True,
                          timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# --- the DOM read --------------------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_compute_fit_reads_padding_gap_all_and_chips():
    """The reader maps the strip onto fitCount's inputs: inner width is
    clientWidth minus BOTH paddings minus the slack, the gap comes from
    computed style, and chip widths come from the measure row, not the
    real chips (the real strip renders only the fitted slice, so its own
    widths would shrink the fit to itself)."""
    r = _stub_strip(inner=400, gap=6, widths=(100,) * 5, all_w=40)
    # inner = 400 - 10 - 22 - 1 slack = 367; room after All + gap = 321;
    # each chip costs 106 → 3 fit.
    assert r == 3


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_compute_fit_everything_fit_means_no_pager():
    """When every chip fits without a pager there is nothing to reserve:
    the page size is the whole list, so the pager never renders and can
    never eat a slot on wide screens."""
    r = _stub_strip(inner=400, gap=6, widths=(100, 100, 100), all_w=40)
    assert r == 3


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_compute_fit_reserves_the_rendered_pager_core():
    """Once paging is real the strip must still hold the pager: its nav
    buttons and count (jump excluded — the jump is reserved separately)
    are measured off the rendered pager and consumed before any chip."""
    r = _stub_strip(inner=400, gap=6, widths=(100,) * 6,
                    all_w=40, pager_core=100)
    # room after All + gap = 321; reserve = 100 core + 2 inner pager gaps
    # = 112; 209 left → chip 100.5, then 100.5+6+100.5 = 207 fits too, the
    # third does not: 2 chips. Without the pager the same widths fit 3 —
    # the reserve is what cost the slot.
    assert r == 2
    r0 = _stub_strip(inner=400, gap=6, widths=(100,) * 6, all_w=40)
    assert r0 == 3


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_compute_fit_reserves_the_jump_from_the_measure_row():
    """A selected project reserves its jump chip from the measure row's
    replica, whether or not the pager currently renders it — the reserve
    must not swing with what the current page shows, or the fit
    oscillates between jump-shown and jump-hidden states."""
    r = _stub_strip(inner=400, gap=6, widths=(100,) * 6,
                    all_w=40, pager_core=100, jump_w=90)
    # reserve = 100 core + 90 jump + 3 gaps = 208; 113 left → 1 chip.
    assert r == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_compute_fit_returns_zero_when_nothing_fits():
    """Below the width of All + pager + one chip the page size is zero:
    the fit stays truthful (0 = room for none). The component floors the
    page at one PINCHED chip (picker.jsx, #807) and the chip's pp-only
    CSS keeps it inside the strip — computeFit itself still measures 0,
    so the rendered guard's red proof keeps its overflowing shape."""
    r = _stub_strip(inner=400, gap=6, widths=(500,), all_w=40,
                    pager_core=100)
    assert r == 0


def test_picker_floors_the_degenerate_page_at_one_pinched_chip():
    """#807: a strip too narrow for any full chip still pages one project
    at a time and never shows NaN. The JSX glue floors the measured fit
    at one for paging (Math.max(1, perPage)) and gates the forced chip's
    inline shrink-to-fit style on the pinched state, so the chip takes
    exactly the room left after All and the pager. Plain chips keep
    flex-shrink: 0 — the rendered guard's red proof seeds plain chips,
    which must still overflow."""
    body = _picker_body()
    assert "Math.max(1, perPage)" in body, (
        "ProjectPicker does not floor the page size at one chip — a strip "
        "too narrow for any chip shows empty pages and a NaN pager (#807)")
    assert "pinched ? {" in body, (
        "ProjectPicker does not gate the pinched chip's style on the "
        "pinched state — the forced chip renders at its natural width and "
        "overflows the strip")
    assert "textOverflow" in body, (
        "the pinched chip's style does not ellipsize — a long project name "
        "overflows the strip instead of truncating")
    # Scoped to the .pp-btn rule, not the file: a file-global pin is
    # satisfied by a sibling occurrence and survives the rule losing it.
    btn = _css()[_css().index(".pp-btn {"):]
    assert "flex-shrink: 0" in btn, (
        "plain chips lost flex-shrink: 0 — a squeezed chip wraps (#643) "
        "and the rendered guard's seeded chips would stop overflowing")


# --- the JSX wiring (source pins) ---------------------------------------

def _app() -> str:
    return APP.read_text(encoding="utf-8")


def _picker_body() -> str:
    """The component lives in src/picker.jsx (extracted from app.jsx in
    this change: app.jsx sat at its size baseline and entries never
    rise)."""
    src = PICKER.read_text(encoding="utf-8")
    start = src.index("function ProjectPicker(")
    return src[start:]


def _css() -> str:
    return (ROOT / "public" / "app.css").read_text(encoding="utf-8")


def test_picker_measures_in_a_layout_effect():
    """The fit pass must commit before the browser paints: the measure is
    in useLayoutEffect, never useEffect. A passive effect lets one frame
    of the full chip row paint — a visible reflow on every load."""
    body = _picker_body()
    assert "useLayoutEffect" in body, (
        "ProjectPicker fits outside useLayoutEffect — the pre-fit row can "
        "paint before the fit pass runs")
    # The measure call sits inside that effect, not beside it.
    eff = body[body.index("useLayoutEffect"):]
    assert "computeFit" in eff, (
        "ProjectPicker's fit pass does not run inside its layout effect")


def test_picker_refits_on_resize_and_fonts():
    """The ruling's refit trigger: a width change re-fits. The strip is
    observed with a ResizeObserver (disconnected on unmount), and the
    webfont landing late re-measures — JetBrains Mono changes every chip
    width the fallback measured."""
    body = _picker_body()
    assert "ResizeObserver" in body, (
        "ProjectPicker does not observe the strip — a window resize keeps "
        "the stale page size and over- or under-fills")
    assert "disconnect" in body, (
        "ProjectPicker's ResizeObserver is never disconnected")
    assert "fonts.ready" in body, (
        "ProjectPicker does not re-fit when the webfonts land — every chip "
        "width was measured with the fallback face")


def test_picker_overflow_is_cut_off_not_scrolled():
    """The ruling's hard line: no horizontal scrolling remains. The
    project strip cuts overflow off (overflow-x hidden), scoped to the
    project picker alone — the shared .project-picker class keeps its
    overflow for the range strip (#755 owns that strip)."""
    body = _picker_body()
    assert "overflowX" in body and "'hidden'" in body, (
        "ProjectPicker's strip does not cut overflow off — a fit slip "
        "renders a scrollbar again")
    assert 'data-picker="projects"' in body, (
        "ProjectPicker's strip carries no data-picker marker — the "
        "rendered guard cannot tell it from the range strip")


def test_picker_uses_the_fit_module_and_index_loads_it_first():
    """The arithmetic lives in the plain-JS module this file drives, and
    index.html loads it as a classic script before the babel app that
    consumes it."""
    body = _picker_body()
    assert "pickerFit.computeFit" in body, (
        "ProjectPicker does not drive src/picker-fit.js — the node-tested "
        "arithmetic is dead code")
    html = INDEX.read_text(encoding="utf-8")
    fit_i = html.index('src="/src/picker-fit.js"')
    comp_i = html.index('src="/src/picker.jsx"')
    app_i = html.index('src="/src/app.jsx"')
    assert fit_i < comp_i < app_i, (
        "index.html loads the picker modules after app.jsx — the picker "
        "reads window.pickerFit / defines the strip before app.jsx needs them")


def test_picker_measures_a_hidden_full_row_not_the_real_chips():
    """The measure row holds every chip, hidden: the real strip renders
    only the fitted slice, so measuring it would shrink the fit to the
    current page — a resize wider could never grow the page back."""
    body = _picker_body()
    assert "pp-measure" in body, "ProjectPicker renders no hidden measure row"
    assert "aria-hidden" in body, (
        "ProjectPicker's measure row is not aria-hidden — a full hidden "
        "copy of every chip enters the accessibility tree and the tab order")


def test_picker_paging_math_survives_the_fit():
    """The pager keeps the clamped page and the off-page jump: per-page is
    measured now, but safePage still clamps a shrunken list and the jump
    still finds the selected project's page."""
    body = _picker_body()
    assert "safePage" in body, "ProjectPicker lost the clamped page"
    assert "pp-jump" in body, "ProjectPicker lost the off-page jump chip"
