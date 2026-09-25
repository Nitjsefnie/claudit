"""Source-level guards for the issue #179 stale-dashboard-response race.

Same boundary as test_panel_wiring.py / test_a11y_wiring.py: node cannot
parse JSX and nothing here renders React, so the fetch-race wiring can
regress while the whole suite stays green. These guards read app.jsx
directly and pin the request-currency mechanism shared by the
/api/dashboard and /api/projects refetch effects.

An effect re-fires on project/range/nonce changes while an earlier
request may still be in flight, and the response that landed LAST used
to win -- a slow stale response could overwrite a fresher one (issue
#179 on the dashboard, #182 on the project list). Each effect run now
owns its request through the shared mintRunSignal helper: the effect's
cleanup aborts the superseded request, the success path applies its
response only while its run is current, and the catch tells an abort
from a real failure -- an aborted run applies nothing (and logs
nothing), while a real failure still logs and, on the dashboard, still
drops the pending live-region line (the 4bf3e66 behaviour).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "app.jsx"

DASHBOARD_DEPS = "[backendOn, activeProject, activeRange, dashNonce]);"
PROJECTS_DEPS = "[backendOn, isGuest, activeRange, dashNonce]);"


def _strip_line_comments(src: str) -> str:
    """Drop `//` line comments so prose ABOUT the wiring is not read as
    the wiring. Same shape as test_a11y_wiring's stripper: the lookbehind
    spares `https://`, the only mid-expression // here.
    """
    return re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)


def _effect_for(fetch_marker: str, deps: str) -> str:
    """The refetch effect that fetches fetch_marker, comments stripped."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    try:
        i = src.index(fetch_marker)
        start = src.rindex("useEffect(", 0, i)
        end = src.index(deps, i) + len(deps)
    except ValueError:
        raise AssertionError(
            f"the {fetch_marker} effect or its dependency array moved; "
            "relocate these guards with it") from None
    return src[start:end]


def _dashboard_effect() -> str:
    return _effect_for("/api/dashboard?range=", DASHBOARD_DEPS)


def _projects_effect() -> str:
    return _effect_for("/api/projects?range=", PROJECTS_DEPS)


def _mint_run_signal_helper() -> str:
    """The shared per-run request guard's body."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    m = re.search(r"function mintRunSignal\(\) \{(.*?)\n\}", src, re.S)
    assert m, "the shared mintRunSignal helper is missing from app.jsx"
    return m.group(1)


def _then_block(effect: str) -> str:
    """The success handler, from `.then(b => {` to the `.catch`."""
    m = re.search(r"\.then\(b => \{(.*?)\.catch\(", effect, re.S)
    assert m, "the effect's .then(b => { ... }) handler moved"
    return m.group(1)


def _catch_body(effect: str) -> str:
    """The failure handler's block body."""
    m = re.search(r"\.catch\(err => \{(.*?)\}\);", effect, re.S)
    assert m, "the effect lost its block-bodied catch"
    return m.group(1)


def test_exactly_one_dashboard_fetch_site():
    """The dashboard guards slice the one /api/dashboard fetch's effect;
    a second site would make the slice ambiguous.
    """
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    assert src.count("/api/dashboard?range=") == 1


def test_exactly_one_projects_fetch_site():
    """The projects guards slice the one /api/projects fetch's effect;
    a second site would make _effect_for take the first occurrence and
    guard the wrong effect.
    """
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    assert src.count("/api/projects?range=") == 1


def test_each_effect_run_owns_its_request():
    """The mechanism (issue #179, #182): the shared mintRunSignal helper
    mints a fresh AbortController per call, and BOTH guarded effects
    hand its signal to their fetch and return the run's abort as the
    effect's cleanup -- a dep re-fire tears down the superseded request
    instead of leaving it to land after (and overwrite) the newer
    response.
    """
    helper = _mint_run_signal_helper()
    assert "new AbortController()" in helper, (
        "the shared helper must mint a fresh AbortController per call")
    assert "signal.aborted" in helper, (
        "the helper's isCurrent must read its controller's signal")
    assert "ctrl.abort()" in helper, (
        "the helper's abort must fire the controller -- a neutered "
        "abort never flips isCurrent, so both races return behind a "
        "green suite")
    for effect in (_dashboard_effect(), _projects_effect()):
        assert "const run = mintRunSignal();" in effect, (
            "each guarded effect run must mint its own run guard")
        m = re.search(r"fetch\(`[^`]*`, \{([^}]*)\}\)", effect)
        assert m, "the effect's fetch call moved"
        assert "signal: run.signal" in m.group(1), (
            "the run's fetch must carry its run guard's signal")
        assert re.search(r"return run\.abort;", effect), (
            "the effect must abort its request in a cleanup, so a dep "
            "change kills the superseded request")


def test_a_stale_response_never_applies():
    """(a) The dashboard success path may apply its response only while
    its run is still the current request: the guard reads the run's
    isCurrent and returns before setBackendDash.
    """
    then = _then_block(_dashboard_effect())
    m = re.search(r"if \(!run\.isCurrent\(\)\) return;", then)
    assert m, (
        "the .then must return early once its run is superseded -- a "
        "response that arrives after the run was replaced must not call "
        "setBackendDash")
    apply_idx = then.index("setBackendDash(b)")
    assert m.start() < apply_idx, (
        "the superseded guard must sit BEFORE setBackendDash(b)")


def test_an_aborted_request_is_not_a_failure():
    """(b) The dashboard catch must tell an abort from a real failure:
    it returns once its run is superseded BEFORE touching the pending
    line, which by then belongs to the run that replaced this one.
    Clearing it here would kill the announcement the newer run owes.
    """
    body = _catch_body(_dashboard_effect())
    m = re.search(r"if \(!run\.isCurrent\(\)\) return;", body)
    assert m, (
        "the catch must return early on a superseded run -- abort is "
        "cancellation, not failure")
    clear = body.index("refreshRef.current = null")
    assert m.start() < clear, (
        "the superseded early-return must sit BEFORE refreshRef.current "
        "= null, or a superseded request's rejection drops the newer "
        "run's pending announcement")


def test_a_real_failure_still_clears_the_pending_line():
    """(c) A real failure keeps the 4bf3e66 behaviour: the pending
    what-changed line is dropped so it cannot mislabel the next
    successful announcement.
    """
    body = _catch_body(_dashboard_effect())
    assert "refreshRef.current = null" in body, (
        "a real refetch failure must clear refreshRef so the pending "
        "what-changed line cannot mislabel the next announcement")


def test_projects_stale_response_never_applies():
    """(a) Same guard as the dashboard effect (issue #182): a
    superseded /api/projects response must not setProjects over a
    fresher project list.
    """
    then = _then_block(_projects_effect())
    m = re.search(r"if \(!run\.isCurrent\(\)\) return;", then)
    assert m, (
        "the projects .then must return early once its run is superseded")
    assert m.start() < then.index("setProjects("), (
        "the superseded guard must sit BEFORE setProjects")


def test_projects_aborted_request_is_not_a_failure():
    """(b) A superseded projects run logs nothing: its catch returns
    before console.error, matching the dashboard's abort-vs-failure
    split.
    """
    body = _catch_body(_projects_effect())
    m = re.search(r"if \(!run\.isCurrent\(\)\) return;", body)
    assert m, "the projects catch must return early on a superseded run"
    log = body.index("console.error(")
    assert m.start() < log, (
        "the superseded early-return must sit BEFORE console.error, so "
        "an aborted request logs nothing")


def test_projects_real_failure_still_logs():
    """A real projects failure keeps its console.error -- only an abort
    is silent.
    """
    body = _catch_body(_projects_effect())
    assert "console.error(" in body, (
        "a real projects failure must still be logged")
