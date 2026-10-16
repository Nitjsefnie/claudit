"""Synthetic verdicts and real disposable git trees for the cost gate."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ci/gate_base_head.py'


@pytest.fixture(name='gate')
def gate_fixture(monkeypatch):
    assert SCRIPT.is_file(), 'the base/head gate has not been implemented'
    spec = importlib.util.spec_from_file_location('gate_base_head', SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Arithmetic fixtures keep the maintainer's proof-calibrated dials free.
    monkeypatch.setattr(module, 'SCAN_RATE_CAP', 2000)
    monkeypatch.setattr(module, 'NOISE', Decimal('0.2'))
    monkeypatch.setattr(module, 'FIXED_MARGIN', Decimal('1.5'))
    monkeypatch.setattr(module, 'REPARSE_MARGIN_PCT', Decimal('0.02'))
    return module


def _draw(lines=100, run='10.0'):
    return {
        'suite': {
            'instrument': 'instruction_count', 'hash_seed': '0',
            'tests_tree_lines': lines,
            'phases': {name: {'million_instructions': value}
                       for name, value in (('collection', '5.0'),
                                           ('run', run), ('residual', '2.0'))},
        },
        'reparse': {
            'counts': {'available': True},
            'phases': {name: {'bytecode_hundreds_per_file': '10.0',
                              'share': '25.0'}
                       for name in ('sniff', 'parse_body', 'sidecar',
                                    'residual')},
        },
    }


@pytest.mark.parametrize(('run', 'passed'), [('12.2', True), ('12.3', False)])
def test_scan_absolute_cap_boundary(gate, run, passed):
    # 1000 added lines allow 2.0M + 0.2M noise; the head adds 0.5M
    # fixed work, within its own margin and outside the SCAN comparison.
    draws = [_draw(), _draw(1100, run),
             _draw(1100, str(Decimal(run) + Decimal('0.5')))]
    result = gate.verdict(draws)
    assert result['passed'] is passed
    if not passed:
        assert 'SCAN run' in result['failures'][0]
        assert '12.3' in result['failures'][0]
        assert '2000' in result['failures'][0]


@pytest.mark.parametrize('phase', ['collection', 'run', 'residual'])
@pytest.mark.parametrize(('delta', 'passed'), [('1.5', True), ('1.6', False)])
def test_fixed_margin_each_phase(gate, phase, delta, passed):
    draws = [_draw(), _draw(1100), _draw(1100)]
    # Draw2 adds 2.0M in every phase (run permits 2.2M for 1000 lines).
    # Only draw3 - draw2 is fixed work: 1.5M passes, 1.6M fails.
    for name, data in draws[0]['suite']['phases'].items():
        value = str(Decimal(data['million_instructions']) + Decimal('2.0'))
        draws[1]['suite']['phases'][name]['million_instructions'] = value
        draws[2]['suite']['phases'][name]['million_instructions'] = value
    value = draws[2]['suite']['phases'][phase]['million_instructions']
    draws[2]['suite']['phases'][phase]['million_instructions'] = str(
        Decimal(value) + Decimal(delta))
    result = gate.verdict(draws)
    assert result['passed'] is passed
    if not passed:
        assert f'FIXED {phase}' in result['failures'][0]
        assert '1.5' in result['failures'][0]


@pytest.mark.parametrize('phase', ['sniff', 'parse_body', 'sidecar', 'residual'])
@pytest.mark.parametrize(('value', 'passed'), [('10.2', True), ('10.3', False)])
def test_reparse_bytecodes_boundary(gate, phase, value, passed):
    draws = [_draw(), _draw(), _draw()]
    draws[2]['reparse']['phases'][phase]['bytecode_hundreds_per_file'] = value
    draws[2]['reparse']['phases'][phase]['share'] = '99.0'
    result = gate.verdict(draws)
    assert result['passed'] is passed
    if not passed:
        assert f'REPARSE {phase}' in result['failures'][0]
        assert '2%' in result['failures'][0]


def test_missing_or_nonfinite_counts_fail_closed(gate):
    for value in (None, 'NaN', 'Infinity'):
        draws = [_draw(), _draw(), _draw()]
        draws[0]['suite']['phases']['run']['million_instructions'] = value
        with pytest.raises(ValueError, match='count'):
            gate.verdict(draws)


def _git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture(name='repo')
def repo_fixture(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-b', 'base')
    _git(repo, 'config', 'user.email', 'fixture@example.invalid')
    _git(repo, 'config', 'user.name', 'Fixture')
    (repo / 'tests').mkdir()
    (repo / 'tests/test_old.py').write_text('assert True\n', encoding='utf-8')
    (repo / 'code.py').write_text('VALUE = 1\n', encoding='utf-8')
    _git(repo, 'add', 'tests', 'code.py')
    _git(repo, 'commit', '-m', 'base')
    _git(repo, 'switch', '-c', 'head')
    (repo / 'tests/test_old.py').unlink()
    (repo / 'tests/test_new.py').write_text('assert 2 == 2\n', encoding='utf-8')
    (repo / 'tests/data.bin').write_bytes(bytes(range(256)))
    (repo / 'code.py').write_text('VALUE = 2\n', encoding='utf-8')
    _git(repo, 'add', 'tests', 'code.py')
    _git(repo, 'commit', '-m', 'head')
    return repo


def test_three_real_trees_apply_only_tests_and_cleanup(gate, repo, tmp_path):
    with gate.trees(repo, tmp_path / 'draws', 'base', 'head', repo) as roots:
        assert len(roots) == 3
        assert (roots[0] / 'tests/test_old.py').exists()
        assert not (roots[1] / 'tests/test_old.py').exists()
        assert (roots[1] / 'tests/data.bin').read_bytes() == bytes(range(256))
        assert (roots[1] / 'tests/test_new.py').read_bytes() == (
            roots[2] / 'tests/test_new.py').read_bytes()
        assert (roots[1] / 'code.py').read_text() == 'VALUE = 1\n'
        assert (roots[2] / 'code.py').read_text() == 'VALUE = 2\n'
    assert len(_git(repo, 'worktree', 'list').splitlines()) == 1
    assert all(not root.exists() for root in roots)


@pytest.mark.parametrize('failure', ['measure', 'apply', 'add', 'after_add'])
def test_cleanup_on_failure(gate, repo, tmp_path, monkeypatch, failure):
    original = gate.git

    def git(root, *args, **kwargs):
        if ((failure == 'apply' and args[0] == 'apply')
                or (failure == 'add' and args[:2] == ('worktree', 'add')
                    and 'draw2' in str(args))):
            raise ValueError('synthetic failure')
        result = original(root, *args, **kwargs)
        if (failure == 'after_add' and args[:2] == ('worktree', 'add')
                and 'draw2' in str(args)):
            raise ValueError('synthetic failure')
        return result

    monkeypatch.setattr(gate, 'git', git)
    with pytest.raises(ValueError, match='synthetic failure'):
        with gate.trees(repo, tmp_path / 'draws', 'base', None, repo):
            raise ValueError('synthetic failure')
    assert len(_git(repo, 'worktree', 'list').splitlines()) == 1


@pytest.mark.parametrize('primary', ['pass', 'clause', 'measurement'])
def test_cleanup_refusals_preserve_primary_result(gate, repo, tmp_path, monkeypatch, capsys, primary):
    original = gate.git
    removals = []
    calls = []

    def git(root, *args, **kwargs):
        if args[:2] == ('worktree', 'remove'):
            name = Path(args[-1]).name
            removals.append(name)
            if name != 'draw1':
                raise ValueError(f'synthetic removal refusal {name}')
        return original(root, *args, **kwargs)

    def measure(root, scratch):
        calls.append(root)
        if primary == 'measurement':
            raise ValueError('primary measurement failure')
        return _draw(run='20.0' if primary == 'clause' and len(calls) == 3
                     else '10.0')

    monkeypatch.setattr(gate, 'git', git)
    monkeypatch.setattr(gate, 'measure_draw', measure)
    monkeypatch.chdir(repo)
    output = tmp_path / 'verdict.json'
    try:
        assert gate.main(['--base-ref', 'base', '--head-ref', 'head',
                          '--write', str(output)]) == (0 if primary == 'pass' else 1)
        assert removals == ['draw3', 'draw2', 'draw1']
        assert '/draw1\n' not in _git(repo, 'worktree', 'list', '--porcelain')
        result = json.loads(output.read_text())['verdict']
        if primary == 'measurement':
            assert result['failures'] == ['primary measurement failure']
        elif primary == 'clause':
            assert result['failures'][0].startswith('FIXED run:')
        else:
            assert result['passed']
        errors = capsys.readouterr().err
        assert 'synthetic removal refusal draw3' in errors
        assert 'synthetic removal refusal draw2' in errors
    finally:
        # Only the injected refusals leave these disposable registrations.
        for line in _git(repo, 'worktree', 'list', '--porcelain').splitlines():
            if (line.startswith('worktree ')
                    and Path(line[9:]).name in ('draw3', 'draw2', 'draw1')):
                original(repo, 'worktree', 'remove', '--force', line[9:])


def test_head_tree_includes_tracked_uncommitted_tests(gate, repo, tmp_path):
    (repo / 'tests/test_new.py').write_text('assert 3 == 3\n', encoding='utf-8')
    with gate.trees(repo, tmp_path / 'draws', 'base', None, repo) as roots:
        assert roots[2] == repo
        assert (roots[1] / 'tests/test_new.py').read_text() == 'assert 3 == 3\n'


def test_self_check_prints_two_counts_and_deltas(gate, repo, tmp_path, monkeypatch, capsys):
    calls = []

    def measure(root, scratch):
        calls.append(root)
        return _draw(run='10.0' if len(calls) == 1 else '10.1')

    monkeypatch.setattr(gate, 'measure_draw', measure)
    output = tmp_path / 'noise.json'
    summary = tmp_path / 'summary.md'
    monkeypatch.chdir(repo)
    assert gate.main(['--self-check', '--write', str(output),
                      '--summary', str(summary)]) == 0
    result = json.loads(output.read_text())
    assert len(calls) == 2
    assert len(result['draws']) == 2
    assert result['deltas']['suite']['run'] == '0.1'
    assert '10.0' in capsys.readouterr().out
    assert '10.1' in summary.read_text()
    assert '0.1' in summary.read_text()
    assert len(_git(repo, 'worktree', 'list').splitlines()) == 1


@pytest.mark.parametrize('broken_instrument', [False, True])
def test_failure_always_writes_json(gate, repo, tmp_path, monkeypatch, broken_instrument):
    calls = []

    def measure(root, scratch):
        calls.append(root)
        if broken_instrument:
            raise ValueError('unavailable instruction counter')
        return _draw(run='20.0' if len(calls) == 3 else '10.0')

    monkeypatch.setattr(gate, 'measure_draw', measure)
    monkeypatch.chdir(repo)
    output = tmp_path / 'verdict.json'
    assert gate.main(['--base-ref', 'base', '--write', str(output)]) == 1
    result = json.loads(output.read_text())
    assert not result['verdict']['passed']
    assert result['verdict']['failures']
    assert len(_git(repo, 'worktree', 'list').splitlines()) == 1


def test_reparse_missing_counter_rejected(gate):
    draws = [_draw(), _draw(), _draw()]
    draws[2]['reparse']['counts']['available'] = False
    with pytest.raises(ValueError, match='counter'):
        gate.verdict(draws)


def test_randomized_suite_measurement_is_refused(gate):
    draws = [_draw(), _draw(), _draw()]
    draws[1]['suite']['hash_seed'] = 'randomized'
    with pytest.raises(ValueError, match='hash.seed'):
        gate.verdict(draws)


def test_failed_draw_diagnostics_reach_json_and_summary(gate, repo, tmp_path, monkeypatch):
    instrument = tmp_path / 'instruments/scripts/ci'
    instrument.mkdir(parents=True)
    (instrument / 'suite_bench.py').write_text(
        'import sys\n'
        'print("draw stdout detail" + "x" * 100000)\n'
        'print("y" * 100000 + "draw stderr cause", file=sys.stderr)\n'
        'raise SystemExit(1)\n', encoding='utf-8')
    monkeypatch.setattr(gate, 'ROOT', instrument.parents[1])
    monkeypatch.chdir(repo)
    output = tmp_path / 'verdict.json'
    summary = tmp_path / 'summary.md'
    assert gate.main(['--base-ref', 'base', '--write', str(output),
                      '--summary', str(summary)]) == 1
    for text in (output.read_text(), summary.read_text()):
        assert 'draw stdout detail' in text
        assert 'draw stderr cause' in text
        assert 'suite' in text
    assert 'not measured' in summary.read_text().lower()
    assert len(json.loads(output.read_text())['verdict']['failures'][0]) < 40_000


def test_instrument_without_scratch_refuses_cleanly(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), '--instrument', 'suite',
         '--write', str(tmp_path / 'suite.json')],
        capture_output=True, text=True, check=False, timeout=30)
    assert proc.returncode == 2
    assert '--scratch' in proc.stderr
    assert 'Traceback' not in proc.stderr


def test_scan_deletions_keep_signed_line_delta(gate):
    draws = [_draw(1100, '12.0'), _draw(100, '10.3'), _draw(100, '10.3')]
    assert not gate.verdict(draws)['passed']


@pytest.mark.parametrize(('base_ref', 'head_ref'), [('base', None), (None, 'base')])
def test_self_check_does_not_apply_tests_diff(gate, repo, tmp_path, base_ref, head_ref):
    with gate.trees(repo, tmp_path / 'draws', base_ref, head_ref, repo,
                    self_check=True) as roots:
        assert len(roots) == 2
        assert (roots[0] / 'tests/test_old.py').read_bytes() == (
            roots[1] / 'tests/test_old.py').read_bytes()
        assert roots[0] != roots[1]


def test_workflow_cost_gate_follows_successful_suite_and_jscov():
    path = SCRIPT.parents[2] / '.github/workflows/tests.yml'
    workflow = yaml.safe_load(path.read_text(encoding='utf-8'))
    steps = workflow['jobs']['pytest']['steps']
    cost = next(step for step in steps if step.get('id') == 'cost_gate')
    assert "steps.pytest.outcome == 'success'" in cost['if']
    assert "steps.jscov.outcome == 'success'" in cost['if']
    assert "github.event_name == 'pull_request'" in cost['if']
    assert "github.event_name == 'push'" in cost['if']
    assert '--self-check' not in cost['run']
    artifact = next(step for step in steps
                    if step.get('with', {}).get('name') == 'cost-gate-verdict')
    assert artifact['with']['retention-days'] == 1
    assert "steps.cost_gate.conclusion != 'skipped'" in artifact['if']


def test_suite_child_uses_draw_backend_and_private_basetemps(gate, tmp_path, monkeypatch, capsys):
    root = tmp_path / 'mini'
    (root / 'backend').mkdir(parents=True)
    (root / 'backend/__init__.py').write_text(
        'MARKER = "draw backend"\n__path__.append('
        + repr(str(SCRIPT.parents[2] / 'backend')) + ')\n', encoding='utf-8')
    (root / 'tests').mkdir()
    test = root / 'tests/test_mini.py'
    test.write_text(
        'import os, subprocess, sys\n'
        'from backend import MARKER\n'
        'def test_draw(tmp_path):\n'
        '    assert MARKER == "draw backend"\n'
        '    assert "pytest-count" in str(tmp_path)\n'
        '    nested = tmp_path / "test_nested.py"\n'
        '    nested.write_text("def test_ok():\\n    pass\\n")\n'
        '    proc = subprocess.run([sys.executable, "-m", "pytest", "-q",\n'
        '        "-p", "no:cacheprovider", str(nested)], cwd=tmp_path,\n'
        '        env=dict(os.environ), capture_output=True, text=True)\n'
        '    assert proc.returncode == 0, proc.stderr + proc.stdout\n', encoding='utf-8')
    (root / 'pytest.ini').write_text(
        '[pytest]\nasyncio_default_fixture_loop_scope = function\n',
        encoding='utf-8')
    fixture = tmp_path / 'fixture.txt'
    fixture.write_text('tests/test_mini.py\n', encoding='utf-8')
    output = tmp_path / 'suite.json'
    scratch = tmp_path / 'scratch with spaces'
    scratch.mkdir()
    instrument = tmp_path / 'instruments/scripts/ci'
    instrument.mkdir(parents=True)
    for name in ('suite_bench.py', 'suite_phases.py', 'suite_report.py',
                 'suite_pricing_doc.json'):
        shutil.copyfile(SCRIPT.parent / name, instrument / name)
    shutil.copyfile(fixture, instrument / 'suite_bench_files.txt')
    monkeypatch.setattr(gate, 'ROOT', instrument.parents[1])
    monkeypatch.setenv('PYTHONHASHSEED', '123')
    assert gate.main(['--instrument', 'suite', '--head-tree', str(root),
                      '--scratch', str(scratch), '--write', str(output)]) == 0
    data = json.loads(output.read_text())
    assert data['tests'] == 1
    assert data['hash_seed'] == '0'
    assert '1 passed' in capsys.readouterr().out
    assert data['tests_tree_lines'] == len(test.read_text().splitlines())
    assert (scratch / 'pytest-count').is_dir()
    assert list((root / 'tests/__pycache__').glob('test_mini.*.pyc'))


def test_reparse_child_pins_corpus_and_imports_draw_backend(tmp_path):
    root = tmp_path / 'mini'
    (root / 'backend').mkdir(parents=True)
    (root / 'backend/__init__.py').write_text(
        '__path__.append(' + repr(str(SCRIPT.parents[2] / 'backend')) + ')\n',
        encoding='utf-8')
    output = tmp_path / 'reparse.json'
    # Real counting and corpus walk; bound only the time-share repetitions.
    code = (
        'import sys; from pathlib import Path; '
        'sys.path.insert(0, sys.argv[1]); '
        'import gate_base_head as gate; '
        'sys.path.insert(0, sys.argv[2]); import backend; '
        'import reparse_bench as bench; '
        'original = bench.measure; '
        'bench.measure = lambda entries, **kw: '
        'original(entries, passes=2, warmup=1, **kw); '
        'gate._instrument("reparse", Path(sys.argv[2]), '
        'Path(sys.argv[3]).parent, Path(sys.argv[3]))')
    proc = subprocess.run(
        [sys.executable, '-c', code, str(SCRIPT.parent), str(root), str(output)],
        cwd=root, env={**os.environ, 'PYTHONHASHSEED': '0',
                       'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(root)},
        capture_output=True, text=True, check=False, timeout=60)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    data = json.loads(output.read_text())
    assert data['files'] > 0
    assert data['counts']['available']
    assert all(value['bytecode_hundreds_per_file'] is not None
               for value in data['phases'].values())
    assert 'share is telemetry' in proc.stdout
