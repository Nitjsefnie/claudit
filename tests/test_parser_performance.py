"""Keep shared analysis single-pass while checking its actual stored output."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from backend.parse import parse_file
from backend import bash_literals
from backend.bash_churn import bash_churn, python_write_paths
from backend.bash_churn_errors import churn_survives_error
from backend.bash_reads import scan
from scripts.bench_parser import _check_outputs


_HEREDOC_PERFORMANCE_WORKER = '''
import json
import sys

from backend.bash_churn import bash_churn, python_write_paths
from backend.bash_churn_errors import churn_survives_error
from backend.bash_reads import scan

results = []
for command in json.load(sys.stdin):
    results.append([
        list(bash_churn(command)),
        python_write_paths(command),
        churn_survives_error(command, 'Exit code 1'),
        list(scan(command)),
    ])
json.dump(results, sys.stdout)
'''


def _heredoc_write_command(count):
    return 'cat >out.txt ' + '<<A ' * count + '\nx\nA' * count + '\n&& true'


@pytest.mark.parametrize(('count', 'expected_churn'), [(3, (3, 0)), (50, (50, 0))])
def test_repeated_heredoc_write_outputs(count, expected_churn):
    command = _heredoc_write_command(count)
    assert (bash_churn(command), python_write_paths(command),
            churn_survives_error(command, 'Exit code 1'), scan(command)) == (
                expected_churn, [], True, (None, [], ['out.txt']))


def test_many_same_line_heredoc_openers_stay_within_budget():
    count = 20_000
    opener_command = 'cat ' + '<<A ' * count
    write_command = _heredoc_write_command(count)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, '-c', _HEREDOC_PERFORMANCE_WORKER],
            input=json.dumps([opener_command, write_command]),
            capture_output=True, check=False, cwd=Path(__file__).resolve().parents[1],
            text=True, timeout=10.0)
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - started
        pytest.fail(f'20,000 heredoc openers exceeded 10.0 seconds ({elapsed:.2f}s)')
    elapsed = time.perf_counter() - started

    assert completed.returncode == 0, completed.stderr
    assert elapsed < 10.0
    assert json.loads(completed.stdout) == [
        [[0, 0], [], False, [None, [], []]],
        [[count, 0], [], True, [None, [], ['out.txt']]],
    ]


def test_bash_shared_scan():
    blob = (Path(__file__).resolve().parents[1] / 'fixtures/parser/bash_shared_scan.jsonl').read_bytes()
    calls: Counter[str] = Counter()

    def count(frame, event, _arg):
        if event == 'call' and frame.f_code.co_name in (
                'shell_tokens', '_split_heredocs', '_dash_c_sources'):
            calls[frame.f_code.co_name] += 1

    previous = sys.getprofile()
    sys.setprofile(count)
    try:
        result = parse_file('shared.jsonl', blob)
    finally:
        sys.setprofile(previous)
    assert len(result['tool_uses']) == 1
    tool = result['tool_uses'][0]
    assert (tool['lines_added'], tool['lines_deleted']) == (2, 0)
    assert tool['read_targets'] == ['/work/x.txt']
    assert tool['write_targets'] == ['/work/y.txt', '/work/x.txt']
    assert tool['read_kind'] == 'whole'
    assert calls == {'shell_tokens': 1, '_split_heredocs': 1, '_dash_c_sources': 1}


def test_analysis_does_not_leak_between_files():
    blob = (Path(__file__).resolve().parents[1] / 'fixtures/parser/bash_shared_scan.jsonl').read_bytes()
    first = parse_file('shared.jsonl', blob)
    first['tool_uses'][0]['write_targets'].append('/injected')
    other = parse_file('other.jsonl', blob.replace(b'/work', b'/other'))
    assert other['tool_uses'][0]['write_targets'] == ['/other/y.txt', '/other/x.txt']
    assert parse_file('shared.jsonl', blob)['tool_uses'][0]['write_targets'] == ['/work/y.txt', '/work/x.txt']


def test_plain_shell_words_skip_quote_scanning():
    calls = []

    def count(_frame, event, arg):
        if event == 'c_call' and getattr(arg, '__self__', None) is bash_literals._PART:  # pylint: disable=protected-access
            calls.append(arg.__name__)

    previous = sys.getprofile()
    sys.setprofile(count)
    try:
        tokens = bash_literals.shell_tokens('cat src.py $ROOT/file.txt *.md > out.txt')
    finally:
        sys.setprofile(previous)
    assert [(str(t), t.literal, t.operator, t.expansion, t.unquoted_expansion) for t in tokens] == [
        ('cat', True, False, 'cat', False),
        ('src.py', True, False, 'src.py', False),
        ('$ROOT/file.txt', False, False, '$ROOT/file.txt', True),
        ('*.md', False, False, '*.md', False),
        ('>', True, True, '>', False),
        ('out.txt', True, False, 'out.txt', False),
    ]
    assert not calls, 'plain words need neither quote decoding nor brace masking'


@pytest.mark.parametrize('actual', [
    [],
    [{'path': 'file.jsonl', 'sha256': 'changed'}],
    [{'path': 'other.jsonl', 'sha256': 'original'}],
    [{'path': 'file.jsonl', 'sha256': 'original'}, {'path': 'extra.jsonl', 'sha256': 'original'}],
])
def test_benchmark_rejects_changed_missing_extra_or_misidentified_outputs(actual):
    with pytest.raises(ValueError):
        _check_outputs([{'path': 'file.jsonl', 'sha256': 'original'}], actual)


@pytest.mark.parametrize('crlf', [False, True],
                         ids=['lf', 'crlf'])
def test_line_input_avoids_materialising_all_lines(crlf):
    # The CR/CRLF branch must stay lazy too: a Windows checkout carries
    # CRLF fixtures, and an eager splitlines there would hold every line
    # of every file in memory at once.
    class StreamingOnly(bytes):
        def splitlines(self, keepends=False):
            raise AssertionError('eager whole-file line copies')

    blob = (Path(__file__).resolve().parents[1] / 'fixtures/parser/bash_shared_scan.jsonl').read_bytes()
    if crlf:
        blob = blob.replace(b'\n', b'\r\n')
    assert parse_file('shared.jsonl', StreamingOnly(blob)) == parse_file('shared.jsonl', blob)


@pytest.mark.parametrize('separator', [b'\n', b'\r', b'\r\n'])
def test_line_endings_preserve_skipped_lines_and_record_positions(separator):
    blob = (Path(__file__).resolve().parents[1] / 'fixtures/parser/bash_shared_scan.jsonl').read_bytes().rstrip(b'\n')
    lines = [b'', b'   ', b'bad json', blob, b'', b'']
    expected = parse_file('shared.jsonl', b'\n'.join(lines))
    assert expected['records'][0]['line_num'] == 4
    assert parse_file('shared.jsonl', separator.join(lines)) == expected
    assert parse_file('shared.jsonl', b'\r\n'.join(lines[:2]) + b'\r' + b'\n'.join(lines[2:])) == expected
