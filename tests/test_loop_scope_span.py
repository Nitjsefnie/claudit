"""Spanning control for the duplicated loop-scope literal (issue #601).

Issue #481 had to set ``asyncio_default_fixture_loop_scope = function``
at BOTH session-root shapes: setup.cfg's [tool:pytest] (repo-rooted
sessions, the real canary's nested run included) and the string
pytest.ini the mini-suite fixture inside tests/test_suite_bench.py
generates for its tmp-rooted bench sessions. Differential run proved
each site independently load-bearing -- a setup.cfg-only fix left the
tmp-rooted nested bench sessions warning -- so each site needs its own
copy, and #601's point is that nothing reds when either is dropped,
renamed or edited out of agreement: the fixture site's only oracle is
the full-suite warnings summary, human-read, and the setup.cfg site has
none at all. This module is the span: it reads BOTH files and fails
when the literal is absent from either or the two values diverge.
"""
from __future__ import annotations

import configparser
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

LOOP_SCOPE_OPTION = "asyncio_default_fixture_loop_scope"


def _setup_cfg_scope() -> str:
    """The loop scope setup.cfg's [tool:pytest] section sets."""
    parser = configparser.ConfigParser(interpolation=None)
    with open(REPO_ROOT / "setup.cfg", encoding="utf-8") as handle:
        parser.read_file(handle)
    assert parser.has_section("tool:pytest"), (
        "setup.cfg lost its [tool:pytest] section")
    assert parser.has_option("tool:pytest", LOOP_SCOPE_OPTION), (
        "setup.cfg [tool:pytest] no longer sets the default fixture loop "
        "scope: repo-rooted pytest sessions would re-enter pytest-asyncio's "
        "configure-stage deprecation warning through the bench's nested "
        "run (issue #481)")
    return parser.get("tool:pytest", LOOP_SCOPE_OPTION).strip()


def _bench_ini_scopes() -> set[str]:
    """Every loop scope the bench module's generated pytest.ini sets."""
    source = (REPO_ROOT / "tests" / "test_suite_bench.py").read_text(
        encoding="utf-8")
    found = set(re.findall(
        re.escape(LOOP_SCOPE_OPTION) + r"\s*=\s*([^\s\"',)\\]+)", source))
    assert found, (
        "tests/test_suite_bench.py no longer generates a pytest.ini "
        "carrying the default fixture loop scope: the tmp-rooted bench "
        "sessions would re-enter pytest-asyncio's configure-stage "
        "deprecation warning inside the outer run's warnings summary "
        "(issue #481)")
    return found


def test_the_fixture_loop_scope_literal_spans_both_session_roots():
    """Both session-root shapes set the same fixture loop scope."""
    assert _bench_ini_scopes() == {_setup_cfg_scope()}, (
        "the two session-root configs disagree on the default fixture "
        "loop scope")
