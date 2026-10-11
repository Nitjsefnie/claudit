"""Render-shape pins for the rate-limit suppression (issue #943).

Nothing here renders React and node cannot parse JSX, so both render
shapes are pinned at the source the way tests/test_panel_wiring.py pins
the panel wiring: a gate that disappears or moves off its element is a
regression the rest of the suite cannot see.

One flag drives both surfaces — the always-present boolean
`hide_rate_limits` on /api/dashboard (true when the deploy sets
CLAUDIT_HIDE_RATE_LIMITS at backend startup):

* true  -> NO "rate-limit hits" Stat at all (not a zero), and the
  Session Burn Rate panel draws no rate-limit bands and speaks no
  "N rate-limit lines marked" sentence.
* false -> exactly the pre-#943 render: the Stat, the bands, the
  sentence, all unchanged.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "app.jsx"
BURN = ROOT / "src" / "burn-rate-panel.jsx"


def _strip_line_comments(src: str) -> str:
    """Drop `//` line comments so prose ABOUT a gate is not read as the
    gate (same helper shape as test_panel_wiring.py's)."""
    return re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)


def _app() -> str:
    return _strip_line_comments(APP.read_text(encoding="utf-8"))


def _burn() -> str:
    return _strip_line_comments(BURN.read_text(encoding="utf-8"))


def test_the_payload_flag_reaches_the_dashboard():
    """The flag is read off the payload once, defaulted for the shapes
    that carry none (synthetic preview, transcript path), and there is
    exactly one rate-limit Stat to gate."""
    src = _app()
    assert "hideRateLimits: !!b.hide_rate_limits" in src, (
        "backendDashToShape does not carry hide_rate_limits off the "
        "/api/dashboard payload, so nothing downstream can suppress on it")
    assert re.search(r"hideRateLimits\s*=\s*false", src), (
        "Dashboard must default the flag off, or a shape that carries no "
        "flag (synthetic preview, transcript path) renders nothing")
    assert src.count('label="rate-limit hits"') == 1, (
        "expected exactly one rate-limit Stat site to gate")


def test_the_rate_limit_stat_is_dropped_not_zeroed_when_hidden():
    """Gate immediately before the element, like every other summary Stat:
    `{!hideRateLimits && <Stat ...>}` renders NOTHING when true, whereas a
    value-level hack would render a zero."""
    src = _app()
    idx = src.index('label="rate-limit hits"')
    window = src[max(0, idx - 160):idx]
    assert "!hideRateLimits &&" in window, (
        "the rate-limit Stat is not gated on !hideRateLimits immediately "
        "before its element -- a hidden deploy would still render the Stat")


def test_the_flag_is_handed_to_the_burn_rate_panel():
    """The panel is a separate module; without the prop it can only guess
    from an empty list and would still speak the zero-hit sentence."""
    src = _app()
    idx = src.index("<window.BurnRatePanel")
    window = src[idx:idx + 400]
    assert "hideRateLimits={hideRateLimits}" in window, (
        "BurnRatePanel is not handed hide_rate_limits")


def test_the_burn_rate_panel_binds_its_hits_through_the_flag():
    """Every rate-limit surface in the panel — the data-envelope loop, the
    hover band scan, the drawn lines — reads ONE bound name, and that
    binding is what the flag empties. The raw prop reaches only the
    binding, so no surface can read past the suppression."""
    src = _burn()
    assert ("function BurnRatePanel({ events, sessions, "
            "limitHits: rawLimitHits, range: propRange, windowBoundaries, "
            "hideRateLimits = false })") in src, (
        "BurnRatePanel no longer destructures the flag (defaulted off) "
        "and its raw limitHits under one name")
    assert "const limitHits = hideRateLimits ? [] : rawLimitHits;" in src, (
        "the flag no longer empties the panel's bound limitHits -- when "
        "false the raw list must pass through unchanged")
    assert src.count("rawLimitHits") == 2, (
        "rawLimitHits must appear only in the signature and the binding; "
        "a surface reading it directly reads past the suppression")
    assert "{limitHits.map(" in src, "the drawn bands no longer map limitHits"
    assert "for (const lh of limitHits)" in src, (
        "the hover band scan no longer reads the bound list")


def test_the_rate_limit_sentence_is_dropped_when_hidden():
    """The a11y description's "N rate-limit lines marked" sentence is
    keyed on the flag, not on the hit count: gating it on `length` would
    also drop today's "0 rate-limit lines marked" when nothing is hidden."""
    src = _burn()
    idx = src.index("rate-limit ${limitHits.length === 1 ? 'line'")
    window = src[max(0, idx - 200):idx]
    assert "hideRateLimits ? ''" in window, (
        "the rate-limit sentence is not gated on hideRateLimits -- a "
        "hidden deploy would still speak it (as a zero)")