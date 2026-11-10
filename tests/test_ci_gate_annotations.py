"""A failed aggregate names its legs on stdout: one log line, one annotation."""
from __future__ import annotations

import json
import re

import pytest

from scripts.ci import aggregate_gate

LEGS = aggregate_gate.EXPECTED_LEGS
# The legs that run under each classification; every other leg skips.
RAN = {
    None: set(LEGS) - {'setup-docs'},
    'docs_only': {'classify'},
    'data_only': {'classify'} | aggregate_gate.CHEAP_LEGS,
    'comment_only': set(LEGS) - aggregate_gate.COMMENT_ONLY_SKIPPED - {'setup-docs'},
    'thresholds_only': {'classify', 'tests'},
    'setup_only': {'classify', 'setup-docs'},
}


def _passing(flag=None):
    needs: dict = {name: {'result': 'success' if name in RAN[flag] else 'skipped'}
                   for name in LEGS}
    needs['classify']['outputs'] = {flag: 'true'} if flag else {}
    return needs


@pytest.fixture(name='gate')
def gate_fixture(tmp_path, monkeypatch, capsys):
    """Run the entry point over a needs document: (exit code, stdout, summary)."""
    def run(needs):
        summary = tmp_path / 'summary.md'
        summary.write_text('', encoding='utf-8', newline='\n')
        monkeypatch.setenv('CLAUDIT_NEEDS_JSON', json.dumps(needs))
        monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(summary))
        code = aggregate_gate.main()
        return code, capsys.readouterr().out, summary.read_text(encoding='utf-8')
    return run


def _lines(out):
    return re.split(r'\r\n|\r|\n', out)


def _commands(out):
    """Each workflow command in `out` as the runner splits it: (name, raw data)."""
    found = []
    for line in _lines(out):
        line = line.lstrip()
        end = line.find('::', 2)
        if line.startswith('::') and end >= 0:
            found.append((line[2:end].split(' ')[0], line[end + 2:]))
    return found


def _unescape(data):
    return data.replace('%0D', '\r').replace('%0A', '\n').replace('%25', '%')


def _annotation(out):
    """The one `::error` message, which stdout also carries once as a plain line."""
    commands = _commands(out)
    assert [name for name, _ in commands] == ['error']
    raw = commands[0][1]
    assert _lines(out).count(raw) == 1
    return _unescape(raw)


def _sections(message):
    return {label: items.split(', ') for label, items in
            (part.split(': ', 1) for part in message.split('; '))}


def test_a_failed_verdict_names_every_failing_and_missing_leg(gate):
    results = {'lint': 'failure', 'codeql': 'cancelled', 'tests': 'abandoned',
               'types': 'timed_out'}
    needs = _passing()
    needs.update({leg: {'result': result} for leg, result in results.items()})
    del needs['audit']
    code, out, _ = gate(needs)
    assert code == 1
    assert _sections(_annotation(out)) == {
        'failing legs': sorted(f'{leg}={result}' for leg, result in results.items()),
        'missing legs': ['audit'],
        'job did not complete': ['codeql', 'tests'],
    }


@pytest.mark.parametrize('result, stopped', [
    ('cancelled', ['tests']), ('abandoned', ['tests']), ('failure', None)])
def test_only_a_cancelled_or_abandoned_leg_is_said_not_to_complete(gate, result, stopped):
    needs = _passing()
    needs['tests'] = {'result': result}
    code, out, _ = gate(needs)
    assert code == 1
    sections = _sections(_annotation(out))
    assert sections.pop('job did not complete', None) == stopped
    assert sections == {'failing legs': [f'tests={result}']}


@pytest.mark.parametrize('result', [
    '50%', 'a\rb', 'a\nb', 'a\r\nb', '%0A', '%250D', 'a::b', 'x\n::error::forged',
    'x\r::notice::forged'])
def test_any_result_string_stays_one_single_line_annotation(gate, result):
    needs = _passing()
    needs['lint'] = {'result': result}
    code, out, _ = gate(needs)
    assert code == 1
    assert _annotation(out) == f'failing legs: lint={result}'


def test_a_failed_verdict_leaves_the_step_summary_as_it_was(gate):
    needs = _passing()
    needs['tests'] = {'result': 'abandoned'}
    code, _, summary = gate(needs)
    assert code == 1
    assert summary == aggregate_gate.render(
        needs, aggregate_gate.FAILED, 'failing legs: tests=abandoned')


@pytest.mark.parametrize('flag', list(RAN))
def test_a_passing_verdict_prints_no_failure_line_and_no_annotation(gate, flag):
    needs = _passing(flag)
    code, out, _ = gate(needs)
    assert code == 0
    assert _lines(out) == [f'legs in the document: {", ".join(sorted(LEGS))}', '']
    if flag:
        needs['classify']['outputs'] = {}
        assert gate(needs)[0] == 1
    needs['classify']['result'] = 'failure'
    code, out, _ = gate(needs)
    assert code == 1 and 'classify=failure' in _sections(_annotation(out))['failing legs']
