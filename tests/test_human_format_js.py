"""Three-significant-digit count and currency formatting, driven through node."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FORMAT_JS = Path(__file__).resolve().parents[1] / "src" / "human-format.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _node(body: str):
    script = f"""
      global.window = {{}};
      require({str(FORMAT_JS)!r});
      {body}
    """
    proc = subprocess.run(
        ["node"], input=script, capture_output=True, text=True, timeout=60,
        check=False, encoding="utf-8", errors="strict",
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("value, expected", [
    (11452, "11.5K"),
    (26880, "26.9K"),
    (144588, "145K"),
    (1852065, "1.85M"),
    (341.2e9, "341B"),
    (10450000, "10.5M"),
    (10449999, "10.4M"),
    (1234, "1.23K"),
    (-10450000, "-10.5M"),
    (999600, "1.00M"),
    (999960000, "1.00B"),
    (10000000, "10.0M"),
    (10040000, "10.0M"),
    (1000000, "1.00M"),
    (999, "999"),
    (13, "13"),
    (341, "341"),
    (0, "0"),
    (12.34, "12.3"),
    (0.1234, "0.123"),
    (-12.35, "-12.4"),
    (1.005, "1.01"),
    (1.4649999999999999, "1.46"),
    (-1.4649999999999999, "-1.46"),
    (1.465, "1.47"),
    (1.4649999999999998, "1.46"),
    (13.049999999999999, "13.0"),
    (13.05, "13.1"),
    (0.00001234, "0.0000123"),
    (1.2345e-7, "0.000000123"),
    (1.2345e21, "1230000000T"),
    (999.62, "1.00K"),
    (999499999999, "999B"),
    (999500000000, "1.00T"),
    (1e12, "1.00T"),
    (999600000000000, "1000T"),
    (1234500000000000, "1230T"),
])
def test_count(value: float, expected: str):
    assert _node(f"console.log(JSON.stringify(window.humanFormat.fmt({value!r})))") == expected


@pytest.mark.parametrize("value, expected", [
    (11357.99, "$11.4K"),
    (1234, "$1.23K"),
    (1200, "$1.20K"),
    (123456, "$123K"),
    (1, "$1.00"),
    (12, "$12.0"),
    (5.32, "$5.32"),
    (789.62, "$790"),
    (0.5, "$0.500"),
    (0, "$0.00"),
    (999.62, "$1.00K"),
    (-10.45, "$-10.5"),
    (99.95, "$100"),
    (9.995, "$10.0"),
    (0.9995, "$1.00"),
    (1.4649999999999999, "$1.46"),
    (-1.4649999999999999, "$-1.46"),
    (1.465, "$1.47"),
    (1.4649999999999998, "$1.46"),
    (13.049999999999999, "$13.0"),
    (13.05, "$13.1"),
])
def test_currency(value: float, expected: str):
    got = _node(f"""
      console.log(JSON.stringify([
        window.humanFormat.currency({value!r}),
        window.humanFormat.fmt({value!r}, true),
      ]));
    """)
    assert got == [expected, expected]


def test_is_currency_distinguishes_forms():
    assert _node("""
      console.log(JSON.stringify([
        window.humanFormat.fmt(12, false),
        window.humanFormat.fmt(12, true),
      ]));
    """) == ["12", "$12.0"]


@pytest.mark.parametrize("value, expected", [
    ("NaN", "NaN"),
    ("Infinity", "Infinity"),
    ("-Infinity", "-Infinity"),
])
def test_non_finite(value: str, expected: str):
    assert _node(f"""
      console.log(JSON.stringify([
        window.humanFormat.fmt({value}),
        window.humanFormat.currency({value}),
      ]));
    """) == [expected, expected]
