"""Source-level guards for the issue #179 stale-dashboard-response race.

Same boundary as test_panel_wiring.py / test_a11y_wiring.py: node cannot
parse JSX and nothing here renders React, so the fetch-race wiring can
regress while the whole suite stays green. These guards read app.jsx
directly and pin the dashboard refetch effect's request-currency
mechanism.

The effect re-fires on project/range/nonce changes while an earlier
/api/dashboard request may still be in flight, and the response that
landed LAST used to win -- a slow stale response could overwrite a
fresher one (issue #179). Each effect run now owns its request through
a per-run AbortController: the effect's cleanup aborts the superseded
request, the success path applies its response only while its run is
current, and the catch tells an abort from a real failure -- an aborted
run applies nothing and leaves the pending live-region line for the run
that replaced it, while a real failure still drops that line (the
4bf3e66 behaviour).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "app.jsx"

DEPS = "[backendOn, activeProject, activeRange, dashNonce]);"


def _strip_line_comments(src: str) -> str:
    """Drop `//` line comments so prose ABOUT the wiring is not read as
    the wiring. Same shape as test_a11y_wiring's stripper: the lookbehind
    spares `https://`, the only mid-expression // here.
    """
    return re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)


def _dashboard_effect() -> str:
    """The dashboard refetch effect's source, comments stripped."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    i = src.index("/api/dashboard?range=")
    start = src.rindex("useEffect(", 0, i)
    m = re.search(re.escape(DEPS), src[i:])
    assert m, (
        "the dashboard effect's dependency array moved; relocate these "
        "guards with it")
    return src[start:i + m.end()]


def _then_block(effect: str) -> str:
    """The success handler, from `.then(b => {` to the `.catch`."""
    m = re.search(r"\.then\(b => \{(.*?)\.catch\(", effect, re.S)
    assert m, "the dashboard effect's .then(b => { ... }) handler moved"
    return m.group(1)


def _catch_body(effect: str) -> str:
    """The failure handler's block body."""
    m = re.search(r"\.catch\(err => \{(.*?)\}\);", effect, re.S)
    assert m, "the dashboard effect lost its block-bodied catch"
    return m.group(1)


def test_exactly_one_dashboard_fetch_site():
    """The guards slice the one /api/dashboard fetch's effect; a second
    site would make the slice ambiguous.
    """
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    assert src.count("/api/dashboard?range=") == 1


def test_each_effect_run_owns_its_request():
    """The mechanism (issue #179): every run creates its own
    AbortController, hands the signal to its fetch, and aborts the
    request in the effect's cleanup -- a dep re-fire tears down the
    superseded request instead of leaving it to land after (and
    overwrite) the newer response.
    """
    effect = _dashboard_effect()
    assert "new AbortController()" in effect, (
        "each dashboard effect run must own a fresh AbortController")
    m = re.search(r"fetch\(`[^`]*`, \{([^}]*)\}\)", effect)
    assert m, "the dashboard fetch call moved"
    assert "signal:" in m.group(1), (
        "the run's fetch must carry its AbortController's signal")
    assert re.search(r"return \(\) => \w+\.abort\(\);", effect), (
        "the effect must abort its request in a cleanup, so a dep "
        "change kills the superseded request")


def test_a_stale_response_never_applies():
    """(a) The success path may apply its response only while its run is
    still the current request: the guard reads the run's own controller
    and returns before setBackendDash.
    """
    then = _then_block(_dashboard_effect())
    m = re.search(r"if \(ctrl\.signal\.aborted\) return;", then)
    assert m, (
        "the .then must return early on its run's aborted signal -- a "
        "response that arrives after the run was replaced must not call "
        "setBackendDash")
    apply_idx = then.index("setBackendDash(b)")
    assert m.start() < apply_idx, (
        "the aborted guard must sit BEFORE setBackendDash(b)")


def test_an_aborted_request_is_not_a_failure():
    """(b) The catch must tell an abort from a real failure: it returns
    on the run's aborted signal BEFORE touching the pending line, which
    by then belongs to the run that replaced this one. Clearing it here
    would kill the announcement the newer run still owes.
    """
    body = _catch_body(_dashboard_effect())
    m = re.search(r"if \(ctrl\.signal\.aborted\) return;", body)
    assert m, (
        "the catch must return early on an aborted (superseded) request "
        "-- abort is cancellation, not failure")
    clear = body.index("refreshRef.current = null")
    assert m.start() < clear, (
        "the abort early-return must sit BEFORE refreshRef.current = "
        "null, or a superseded request's rejection drops the newer "
        "run's pending announcement")


def test_a_real_failure_still_clears_the_pending_line():
    """(c) A real failure keeps the 4bf3e66 behaviour: the pending
    what-changed line is dropped so it cannot mislabel the next
    successful announcement. Green before the fix by construction --
    this pins the behaviour the fix must not break.
    """
    body = _catch_body(_dashboard_effect())
    assert "refreshRef.current = null" in body, (
        "a real refetch failure must clear refreshRef so the pending "
        "what-changed line cannot mislabel the next announcement")
