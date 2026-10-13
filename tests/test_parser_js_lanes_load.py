"""SV-PARSER-SPEC: every node harness over the live parser loads its lanes.

A pure source scan over tests/, split out of test_parser_js_mirror.py,
whose node-driven parity cases need no part of it.
"""
from __future__ import annotations

import io
import locale
import sys
from pathlib import Path

import pytest

from tests.source_scan_cache import source as cached_source

ROOT = Path(__file__).resolve().parents[1]


class TestNodeHarnessesLoadLanes:
    """A node harness that drives parser.js or rates.js over the LIVE
    pricing.json must require parser-lanes.js: rates.js's
    longContextFlagFor calls window.longContextThresholdFor, which
    parser-lanes.js defines, and the call is reached only once the grouped
    long_context_meters field includes a member, so the absence stayed invisible
    until the first live fold (issue #860). The browser always loads
    the module (public/index.html, script order); this pins the
    harnesses to the browser's own dependency, at any require order.

    The scan's grammar: a body is the text after a `global.window` line
    up to the next triple quote; one requiring parser.js or rates.js
    via the interpolated constants (str(PARSER_JS)!r /
    str(RATES_JS)!r) without a str(LANES_JS)!r require is an
    offender. Unseen: a harness shaped otherwise (no marker, tmp-dir
    or renamed-constant requires); a renamed lanes constant reads as
    a false offender."""

    def test_harnesses_driving_the_real_parser_load_lanes(self):
        """A pure source scan - no node, so no node skip (issue #812)."""
        offenders = []
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            text = cached_source(path, encoding=None)
            for chunk in text.split("global.window")[1:]:
                body = chunk.split('"""')[0]
                needs = ("str(PARSER_JS)!r" in body
                         or "str(RATES_JS)!r" in body)
                lanes = "str(LANES_JS)!r" in body
                if needs and not lanes:
                    offenders.append(path.name)
        assert not offenders, (
            "node harnesses driving the live parser/rates without loading "
            f"parser-lanes.js first: {offenders}")


def test_the_lanes_scan_reads_with_the_default_encoding(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scan decodes with Python's default encoding, not UTF-8: with
    the locale's encoding forced to latin-1, a test file holding a byte
    UTF-8 cannot decode is still scanned and its offender reported."""
    if io.text_encoding(None) != "locale":
        pytest.skip("UTF-8 mode: the default encoding is not the locale's")
    monkeypatch.setattr(locale, "getencoding", lambda: "latin-1")
    harness = ('"""\n' + "global." + "window = {};\n"
               + "require(str(" + "PARSER_JS)!r);\n" + '"""\n')
    planted = tmp_path / "tests" / "test_planted.py"
    planted.parent.mkdir()
    planted.write_bytes(b"# caf\xe9\nHARNESS = " + harness.encode("ascii"))
    with pytest.raises(UnicodeDecodeError):
        planted.read_bytes().decode("utf-8")
    assert cached_source(planted, encoding=None).startswith("# caf\u00e9\n")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    with pytest.raises(AssertionError, match="test_planted.py"):
        TestNodeHarnessesLoadLanes(
        ).test_harnesses_driving_the_real_parser_load_lanes()
