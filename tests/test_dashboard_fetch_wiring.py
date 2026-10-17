"""Pin request-currency wiring shared by dashboard and project refetch effects.
Node cannot parse JSX and the suite does not render React, so source checks
cover this boundary. Each run owns its request through mintRunSignal:
cleanup aborts it, success checks currency, and catch distinguishes abort
from real failure. Aborts apply and log nothing; real dashboard failures
still drop the pending live-region announcement."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

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
    """The mechanism: the shared mintRunSignal helper
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
        # Dashboard uses dashboardFetch.load while projects calls fetch
        # directly. Both must carry the run signal, so match either shape.
        m = re.search(r"(?:fetch|window\.dashboardFetch\.load)"
                      r"\(`[^`]*`, \{([^}]*)\}\)", effect)
        assert m, "the effect's request call moved"
        assert "signal: run.signal" in m.group(1), (
            "the run's request must carry its run guard's signal")
        assert re.search(r"return run\.abort;", effect), (
            "the effect must abort its request in a cleanup, so a dep "
            "change kills the superseded request")


@pytest.mark.parametrize("effect_helper,setter", [
    pytest.param(_dashboard_effect, "setBackendDash(b)", id="dashboard"),
    pytest.param(_projects_effect, "setProjects(", id="projects"),
])
def test_a_stale_response_never_applies(effect_helper, setter):
    """A superseded response must return before applying its payload."""
    then = _then_block(effect_helper())
    m = re.search(r"if \(!run\.isCurrent\(\)\) return;", then)
    assert m, "the .then must return early once its run is superseded"
    assert m.start() < then.index(setter), (
        f"the superseded guard must sit BEFORE {setter}")


@pytest.mark.parametrize("effect_helper,side_effect", [
    pytest.param(_dashboard_effect, "refreshRef.current = null", id="dashboard"),
    pytest.param(_projects_effect, "console.error(", id="projects"),
])
def test_an_aborted_request_is_not_a_failure(effect_helper, side_effect):
    """A superseded catch must return before clearing state or logging."""
    body = _catch_body(effect_helper())
    m = re.search(r"if \(!run\.isCurrent\(\)\) return;", body)
    assert m, "the catch must return early on a superseded run"
    assert m.start() < body.index(side_effect), (
        f"the superseded early-return must sit BEFORE {side_effect}")
