"""Regression tests for Python command recognition and inline-script lexing."""
from __future__ import annotations

import itertools
import random
import re
import shlex
from pathlib import Path
import subprocess
import sys

from backend import bash_churn


# Pre-change reference: keep these definitions verbatim as the output oracle.
_PYTHON_WORD = r"(?:^|[|;&(]|\s)(?:\S*/)?python(?:3(?:\.\d+)?)?"
_PYTHON_STDIN = re.compile(_PYTHON_WORD + r"\s+-(?:\s|$)")
_PYTHON_DASH_C = re.compile(_PYTHON_WORD + r"\s+-c\s")


def _dash_c_sources(text: str) -> list[str]:
    """Script bodies passed as `python3 -c '<code>'`."""
    out: list[str] = []
    for m in _PYTHON_DASH_C.finditer(text):
        try:
            tokens = shlex.split(text[m.end():], comments=False, posix=True)
        except ValueError:
            continue
        if tokens:
            out.append(tokens[0])
    return out


_EXHAUSTIVE_TOKENS = (
    "python", "python3", "/", "(", ";", " ", "-c", "x",
)
_RANDOM_TOKENS = _EXHAUSTIVE_TOKENS + (
    "|", "&", "\t", "\u2003", "\u3000", "\x1c", "\xa0", "\u2028",
    "python3.12", ".venv/bin/python", "print(1)", "'a b'", '"c d"',
    "\\'", '\\"', "\n", "-", "'", '"', "\\",
)
_RANDOM_INPUT_COUNT = 3_000
_INVALID_SUFFIX_INPUT_COUNT = 300
_INVALID_PREFIX_TOKENS = tuple(
    token for token in _RANDOM_TOKENS
    if not any(char in token for char in "'\"\\"))


def _differential_inputs():
    """Exhaust token sequences, then cover less common forms deterministically."""
    for length in range(7):
        for pieces in itertools.product(_EXHAUSTIVE_TOKENS, repeat=length):
            yield "".join(pieces)

    rng = random.Random(25_529_729)
    for _ in range(_RANDOM_INPUT_COUNT):
        yield "".join(
            rng.choice(_RANDOM_TOKENS)
            for _ in range(rng.randrange(41)))
    for _ in range(_INVALID_SUFFIX_INPUT_COUNT):
        prefix = "".join(
            rng.choice(_INVALID_PREFIX_TOKENS)
            for _ in range(rng.randrange(41)))
        yield prefix + rng.choice(("'", '"', "\\"))


def test_matchers_and_scripts_match_the_pre_change_reference():
    checked = 0
    for text in _differential_inputs():
        expected_stdin = _PYTHON_STDIN.search(text) is not None
        actual_stdin = bash_churn._PYTHON_STDIN.search(text) is not None  # pylint: disable=protected-access
        assert actual_stdin == expected_stdin, f"stdin mismatch for {text!r}"
        assert bash_churn._dash_c_sources(text) == _dash_c_sources(text), (  # pylint: disable=protected-access
            f"script mismatch for {text!r}")
        checked += 1

    exhaustive_count = sum(len(_EXHAUSTIVE_TOKENS) ** length
                           for length in range(7))
    assert checked == (exhaustive_count + _RANDOM_INPUT_COUNT
                       + _INVALID_SUFFIX_INPUT_COUNT)


def test_unicode_whitespace_invalid_suffixes_match_the_reference():
    separators = ("\u3000", "\x1c", "\xa0", "\u2028")
    for separator in separators:
        for ending in ("'", '"', "\\"):
            for count in (1, 2, 4, 8, 16, 32):
                text = f"python{separator}-c{separator}x{separator}" * count
                text += ending
                assert bash_churn._dash_c_sources(text) == _dash_c_sources(text), (  # pylint: disable=protected-access
                    f"script mismatch for invalid suffix {text!r}")
        for script in ("value", "'a b'", '"c d"', "\\'", '\\"'):
            text = f"python{separator}-c{separator}{script}{separator}"
            assert bash_churn._dash_c_sources(text) == _dash_c_sources(text), (  # pylint: disable=protected-access
                f"script mismatch for token {text!r}")


def test_prefix_consumption_and_shlex_suffix_edges():
    assert bash_churn._dash_c_sources(  # pylint: disable=protected-access
        "python -c python -c x") == ["python"]
    assert bash_churn._dash_c_sources(  # pylint: disable=protected-access
        "python -c ''") == [""]
    assert not bash_churn._dash_c_sources(  # pylint: disable=protected-access
        "python -c ")
    assert not bash_churn._dash_c_sources(  # pylint: disable=protected-access
        "python -c x 'unclosed")
    assert not bash_churn._dash_c_sources(  # pylint: disable=protected-access
        "python -c x python -c y 'unclosed")
    assert not bash_churn._dash_c_sources(  # pylint: disable=protected-access
        "python -c x " + "\\")
    assert bash_churn._dash_c_sources(  # pylint: disable=protected-access
        "/usr/bin/python -c x") == ["x"]
    assert bash_churn._dash_c_sources(  # pylint: disable=protected-access
        "cmd;dir/python -c x") == ["x"]


def _run_timing_case(name: str, program: str):
    try:
        completed = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, check=False,
            cwd=Path(__file__).resolve().parents[1], text=True, timeout=120.0)
    except subprocess.TimeoutExpired:
        return name, None
    return name, completed


def test_quadratic_reproducers_finish_within_the_bounded_budget():
    cases = (
        (
            "long delimiter run",
            "from time import process_time; "
            "from backend.bash_churn import _dash_c_sources; "
            "start = process_time(); result = _dash_c_sources('(' * 200_000); "
            "elapsed = process_time() - start; assert result == []; print(elapsed)",
        ),
        (
            "Python stdin matcher",
            "from time import process_time; "
            "from backend.bash_churn import _PYTHON_STDIN; "
            "text = 'cat <<A ' + '(' * 200_000; start = process_time(); "
            "result = _PYTHON_STDIN.search(text); "
            "elapsed = process_time() - start; assert result is None; print(elapsed)",
        ),
        (
            "repeated python -c scripts",
            "from time import process_time; "
            "from backend.bash_churn import _dash_c_sources; "
            "text = 'python3 -c x ' * 20_000; start = process_time(); "
            "result = _dash_c_sources(text); "
            "elapsed = process_time() - start; "
            "assert result == ['x'] * 20_000; print(elapsed)",
        ),
        (
            "unicode whitespace with unmatched quote",
            "from time import process_time; "
            "from backend.bash_churn import _dash_c_sources; "
            "text = ('python\\u3000-c\\u3000x\\u3000' * 20_000) + \"'\"; "
            "start = process_time(); result = _dash_c_sources(text); "
            "elapsed = process_time() - start; assert result == []; print(elapsed)",
        ),
    )
    for name, program in cases:
        _, completed = _run_timing_case(name, program)
        if completed is None:
            raise AssertionError(f"{name}: 120-second hang guard tripped")
        assert completed.returncode == 0, f"{name} failed: {completed.stderr}"
        cpu_seconds = float(completed.stdout.strip())
        assert cpu_seconds < 5.0, f"{name} used {cpu_seconds:.3f}s CPU"
