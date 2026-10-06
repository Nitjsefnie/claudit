"""Source pins for the two cold-load legs the rendered guard cannot see.

scripts/ci/panel_interactions.mjs judges cold-load CLS in CI, where the
css2 sheet lands in ~62 ms and /api/projects is local: reverting the
font leg or the picker leg shifts nothing measurable there, and the
guard stays green (the 2026-10-06 review's mutants 3/3b). Production
latency is where those legs carry up to 0.07-0.12 CLS, so the legs are
pinned here at source level — each pin names a string the leg needs,
and each was proven red against its revert mutant before landing.

These are pins, not behaviour tests: they fail when a leg is deleted or
reworded, and say nothing about whether the leg still WORKS — the
rendered guard covers that part of the category.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "public" / "index.html"
CSS = ROOT / "public" / "app.css"
APP = ROOT / "src" / "app.jsx"
PERF = ROOT / "src" / "perf.js"

FAMILIES = ("Inter", "JetBrains Mono")


def _index() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_both_faces_are_preloaded_at_parse_time():
    """The font leg, part 1: each family's latin face preloads as a font
    before any script runs, so the file is in cache long before the first
    text paints. Deleting a preload line reverts the face to a
    lazy load that starts at first paint — the 0.07-0.12 CLS shape."""
    html = _index()
    for family in FAMILIES:
        slug = family.lower().replace(" ", "")
        preloads = [line for line in html.splitlines()
                    if 'rel="preload"' in line and f"/{slug}/" in line]
        assert preloads, (
            f"{family} has no preload link — the face loads lazily at "
            f"first paint again and reflows the page when it lands")
        line = preloads[0]
        assert 'as="font"' in line and "crossorigin" in line, (
            f"{family}'s preload lost as=\"font\"/crossorigin: a preload "
            f"that does not match the font fetch's mode warms nothing")


def test_the_css2_link_never_swaps_after_first_paint():
    """The font leg, part 2: display=block holds text invisible while the
    (preloaded) face loads, so the fallback metrics never paint a frame a
    late swap would reflow. display=swap is the regression."""
    html = _index()
    links = [line for line in html.splitlines()
             if "fonts.googleapis.com/css2" in line]
    assert links, "the css2 stylesheet link is gone"
    assert "display=block" in links[0], (
        "the css2 link dropped display=block — a slow network paints the "
        "fallback and swaps it out later, reflowing every text block")
    for family in FAMILIES:
        assert family.replace(" ", "+") in links[0], (
            f"{family} missing from the css2 request — the preloaded face "
            f"no longer has a registered family to serve")


def test_perf_js_loads_the_faces_eagerly():
    """The font leg, part 3: perf.js runs document.fonts.load() while the
    document has nothing laid out, so the faces complete pre-paint. The
    load() call naming both families is the leg; removing it (or narrowing
    it to one family) puts the face load back at first paint."""
    src = PERF.read_text(encoding="utf-8")
    assert "document.fonts.load" in src, (
        "perf.js no longer calls document.fonts.load — faces load at "
        "first paint again")
    block = src[src.index("document.fonts.load"):]
    specs = block[:block.index(");")]
    for family in FAMILIES:
        assert family in specs, (
            f"{family} dropped from the eager load specs — its face "
            f"returns to a lazy first-paint load")
    assert "'ABCXYZ" in src, (
        "the eager load lost its glyph sample — load() without the latin "
        "sample text may fetch a subset that misses the page's glyphs")


def test_the_picker_strip_mounts_before_the_list_lands():
    """The picker leg, part 1: ProjectPicker mounts on backendOn && !isGuest
    ALONE — re-adding `projects &&` unmounts the strip until the list
    lands, and its insertion pushes the whole page (the 0.03-0.18 CLS
    frame). The pin window is the call site; a gate between it and the
    element is the regression."""
    src = APP.read_text(encoding="utf-8")
    i = src.index("<ProjectPicker")
    window = src[max(0, i - 160):i]
    assert "backendOn && !isGuest && (" in window, (
        "ProjectPicker's mount gate no longer reads backendOn && !isGuest "
        "— the strip unmounts pre-projects again")
    assert "projects" not in window, (
        "ProjectPicker's mount gate names projects — the strip is absent "
        "until /api/projects lands and its insertion moves the page")


def test_the_picker_placeholder_fills_the_loaded_box():
    """The picker leg, part 2: pre-projects the picker renders from an
    empty list inside the same strip, so the list's arrival fills rather
    than inserts. Dropping the `projects || []` placeholder throws on the
    first render instead — and any shape that renders nothing pre-projects
    reopens the insertion shift."""
    src = APP.read_text(encoding="utf-8")
    start = src.index("function ProjectPicker(")
    body = src[start:start + 1600]
    assert "projects || []" in body, (
        "ProjectPicker lost its empty-list placeholder — it renders "
        "nothing (or throws) until the list lands")
    assert "pp-btn" in src[start:start + 4000], (
        "ProjectPicker renders no chip — the pre-projects strip has no "
        "box for the loaded row to fill")


def test_the_strip_is_one_fixed_height_row_at_every_width():
    """The picker leg, part 3: the strip is nowrap and scrollable and its
    chips cannot shrink, so its height is payload-independent and the
    placeholder reserves it exactly. flex-wrap:wrap re-arms the wrap (two
    rows at phone width); dropped flex-shrink/white-space lets squeezed
    chips wrap internally to two rows — both change the strip's height
    between placeholder and loaded states."""
    css = CSS.read_text(encoding="utf-8")
    strip = css[css.index(".project-picker {"):]
    strip = strip[:strip.index("}")]
    assert "flex-wrap: nowrap" in strip and "overflow-x: auto" in strip, (
        ".project-picker is not a nowrap, scrollable row — its height "
        "depends on the project count again")
    chip = css[css.index(".pp-btn {"):]
    chip = chip[:chip.index("}")]
    assert "flex-shrink: 0" in chip and "white-space: nowrap" in chip, (
        ".pp-btn can shrink or wrap internally — squeezed chips turn the "
        "one-row strip into two rows between placeholder and load")
