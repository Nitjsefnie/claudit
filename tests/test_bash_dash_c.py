"""Regression tests for Python command recognition and inline-script lexing."""
from __future__ import annotations

import io
import itertools
import json
import random
import re
import shlex
from pathlib import Path
import subprocess
import sys

from backend import bash_churn, bash_dash_c


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


def _oracle_token_span(text: str, position: int) -> int:
    """Measure the first token span using shlex's real stream position."""
    stream = io.StringIO(text)
    stream.seek(position)
    lexer = shlex.shlex(stream, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        lexer.get_token()
    except ValueError:
        pass
    return stream.tell() - position


def _budgeted_reference(text: str, budget: int) -> tuple[list[str], int]:
    """Apply the span budget to the verbatim pre-change candidate oracle."""
    scripts: list[str] = []
    kept_span_chars = 0
    for match in _PYTHON_DASH_C.finditer(text):
        position = match.end()
        stream = io.StringIO(text)
        stream.seek(position)
        lexer = shlex.shlex(stream, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        try:
            token = lexer.get_token()
            span_chars = stream.tell() - position
            while lexer.get_token() is not None:
                pass
        except ValueError:
            continue
        if token is None or kept_span_chars + span_chars > budget:
            continue
        scripts.append(token)
        kept_span_chars += span_chars
    return scripts, kept_span_chars


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
_SCRIPT_SPAN_LIMIT = 1_000_000


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


def test_first_token_spans_match_shlex_stream_positions():
    cases = (
        ("python -c plain rest", len("python -c ")),
        ("python -c 'two words' rest", len("python -c ")),
        ("python -c escaped\\ word rest", len("python -c ")),
        ("python -c '' rest", len("python -c ")),
        ("python -c plain 'unclosed", len("python -c ")),
        ("python\u3000-c\u3000value\u3000rest", len("python\u3000-c\u3000")),
    )
    for text, position in cases:
        spans: dict[int, int] = {}
        bash_dash_c._script_tokens(  # pylint: disable=protected-access
            text, [position], spans=spans)
        assert spans == {position: _oracle_token_span(text, position)}, text


def test_over_budget_inputs_match_budgeted_prechange_oracle():
    inputs = (
        "python\u3000-c\u3000x\u3000" * 500,
        ("python\u3000-c\u3000x\u3000" * 100) + ("''" * 10_000),
    )
    for text in inputs:
        expected, kept_span_chars = _budgeted_reference(
            text, _SCRIPT_SPAN_LIMIT)
        assert kept_span_chars <= _SCRIPT_SPAN_LIMIT
        assert bash_churn._dash_c_sources(text) == expected  # pylint: disable=protected-access


def _timed_source_case(
        name: str, text_expression: str, expected_count: int,
        quote_tail_chars: int = 0):
    program = (
        "import json\n"
        "from time import process_time\n"
        "from backend.bash_churn import _dash_c_sources\n"
        f"text = {text_expression}\n"
        "started = process_time()\n"
        "scripts = _dash_c_sources(text)\n"
        "cpu = process_time() - started\n"
        "script_chars = sum(map(len, scripts))\n"
        f"span_chars = script_chars + len(scripts) * {quote_tail_chars}\n"
        "print(json.dumps({'cpu': cpu, 'count': len(scripts), "
        "'script_chars': script_chars, 'span_chars': span_chars}))\n"
    )
    _, completed = _run_timing_case(name, program)
    if completed is None:
        raise AssertionError(f"{name}: 120-second hang guard tripped")
    assert completed.returncode == 0, f"{name} failed: {completed.stderr}"
    measurement = json.loads(completed.stdout)
    assert measurement["cpu"] < 5.0, (
        f"{name} used {measurement['cpu']:.3f}s CPU")
    assert measurement["span_chars"] <= _SCRIPT_SPAN_LIMIT, (
        f"{name} kept {measurement['span_chars']} script-text characters")
    assert measurement["script_chars"] <= _SCRIPT_SPAN_LIMIT, (
        f"{name} produced {measurement['script_chars']} script characters")
    assert measurement["count"] == expected_count
    return measurement


def test_unicode_separator_scripts_stay_within_span_budget():
    # Four initial spans use 959,888 characters; a later 40,106-character
    # span and the final 2-character span also fit, so six scripts are kept.
    measurement = _timed_source_case(
        "20,000 Unicode-separated Python commands",
        "('python\\u3000-c\\u3000x\\u3000' * 20_000)",
        expected_count=6)
    assert measurement["span_chars"] == 999_996


def test_empty_quote_tail_scripts_stay_within_span_budget():
    measurement = _timed_source_case(
        "Unicode-separated commands followed by empty quote pairs",
        "('python\\u3000-c\\u3000x\\u3000' * 200) + (\"''\" * 40_000)",
        expected_count=12,
        quote_tail_chars=80_000)
    assert measurement["span_chars"] == 987_888


def test_parse_file_stores_bounded_unicode_command_churn_within_cpu_budget():
    program = r'''
import json
from time import process_time

from backend.bash_churn import bash_churn
from backend.parse import parse_file

command = 'python\u3000-c\u3000x\u3000' * 20_000
record = {
    'type': 'assistant',
    'timestamp': '2026-09-28T00:00:00Z',
    'cwd': '/work',
    'requestId': 'issue301',
    'message': {
        'role': 'assistant',
        'model': 'claude-sonnet-4-5',
        'usage': {'input_tokens': 10, 'output_tokens': 5},
        'content': [{
            'type': 'tool_use',
            'id': 'bash1',
            'name': 'Bash',
            'input': {'command': command},
        }],
    },
}
blob = (json.dumps(record) + '\n').encode()
expected = bash_churn(command)
started = process_time()
parsed = parse_file('issue301.jsonl', blob)
cpu = process_time() - started
tool = parsed['tool_uses'][0]
stored = (tool['lines_added'], tool['lines_deleted'])
assert stored == expected, (stored, expected)
print(json.dumps({'cpu': cpu, 'stored': stored}))
'''
    _, completed = _run_timing_case("parse_file stored churn", program)
    if completed is None:
        raise AssertionError("parse_file stored churn: 120-second hang guard tripped")
    assert completed.returncode == 0, completed.stderr
    measurement = json.loads(completed.stdout)
    assert measurement["cpu"] < 5.0, (
        f"parse_file used {measurement['cpu']:.3f}s CPU")
