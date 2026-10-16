"""Thresholds-only changes run the document checks and the canary."""
from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.ci import aggregate_gate, classify_changes

ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS = '.github/ci-thresholds.json'


def _workflow(name):
    return yaml.load((ROOT / '.github/workflows' / name).read_text(
        encoding='utf-8'), Loader=yaml.BaseLoader)


@pytest.mark.parametrize('paths,expected', [
    ([THRESHOLDS], True),
    ([THRESHOLDS, 'backend/app.py'], False),
    ([THRESHOLDS, 'docs/guide.md'], False),
    ([THRESHOLDS, '.github/old-thresholds.json'], False),
    (['docs/guide.md'], False),
    ([], False),
])
def test_classify_thresholds_exact_path_set(paths, expected, tmp_path):
    event = {'name': 'pull_request', 'repository': 'o/r', 'pull_request': '17'}
    result = classify_changes.classify(event, lambda _argv: '\n'.join(paths))
    out = tmp_path / 'outputs'
    classify_changes.write_outputs(str(out), *result)
    outputs = dict(line.split('=', 1) for line in out.read_text().splitlines())
    assert outputs['thresholds_only'] == str(expected).lower()
    if THRESHOLDS in paths:
        assert result[:3] == (False, False, False)
    if expected:
        assert 'thresholds-only' in outputs['reason']


def test_main_emits_thresholds_only(tmp_path, monkeypatch):
    out = tmp_path / 'outputs'
    monkeypatch.setenv('GITHUB_OUTPUT', str(out))
    monkeypatch.setattr(classify_changes, 'changed_paths', lambda *_: [THRESHOLDS])
    assert classify_changes.main() == 0
    outputs = dict(line.split('=', 1) for line in out.read_text().splitlines())
    assert outputs['thresholds_only'] == 'true'


def _needs(flag='true', tests='success'):
    needs: dict = {name: {'result': 'skipped'}
                   for name in aggregate_gate.EXPECTED_LEGS}
    needs['classify'] = {'result': 'success', 'outputs': {'thresholds_only': flag}}
    needs['tests'] = {'result': tests}
    return needs


def test_thresholds_only_accepts_other_skips_and_reports_narrowed_tests():
    needs = _needs()
    verdict, message = aggregate_gate.decide(needs)
    assert verdict == aggregate_gate.PASSED
    assert 'thresholds-only' in message
    assert '11 gate legs skipped by classification' in message
    assert 'tests ran narrowed' in message
    rendered = aggregate_gate.render(needs, verdict, message)
    assert 'thresholds-only narrowing: true' in rendered
    assert '| lint | skipped (thresholds-only narrowing) |' in rendered


@pytest.mark.parametrize('result', ['failure', 'cancelled', 'skipped'])
def test_thresholds_only_requires_tests_success(result):
    verdict, message = aggregate_gate.decide(_needs(tests=result))
    assert verdict == aggregate_gate.FAILED
    assert f'tests={result}' in message


def test_thresholds_only_missing_tests_fails():
    needs = _needs()
    del needs['tests']
    verdict, message = aggregate_gate.decide(needs)
    assert verdict == aggregate_gate.FAILED
    assert 'missing legs: tests' in message


@pytest.mark.parametrize('flag', ['false', None, True, 'TRUE', ''])
def test_thresholds_narrowing_is_fail_closed(flag):
    needs = _needs(flag=flag)
    verdict, message = aggregate_gate.decide(needs)
    assert verdict == aggregate_gate.FAILED
    assert 'lint=skipped' in message


def test_thresholds_narrowing_does_not_hide_classifier_failure():
    needs = _needs()
    needs['classify']['result'] = 'failure'
    verdict, message = aggregate_gate.decide(needs)
    assert verdict == aggregate_gate.FAILED
    assert 'classify=failure' in message


def test_workflows_declare_thresholds_output_and_input():
    gate = _workflow('ci-gate.yml')['jobs']
    assert gate['classify']['outputs']['thresholds_only'] == (
        '${{ steps.classify.outputs.thresholds_only }}')
    inputs = _workflow('tests.yml')['on']['workflow_call']['inputs']
    assert inputs['thresholds_only']['type'] == 'string'
    assert inputs['thresholds_only']['default'] == 'false'


def test_every_non_tests_leg_skips_thresholds_only():
    jobs = _workflow('ci-gate.yml')['jobs']
    for name in aggregate_gate.EXPECTED_LEGS[1:]:
        condition = jobs[name]['if']
        if name == 'setup-docs':
            assert condition == "needs.classify.outputs.setup_only == 'true'"
        elif name == 'tests':
            assert condition == (
                "needs.classify.outputs.docs_only != 'true' "
                "&& needs.classify.outputs.data_only != 'true' "
                "&& needs.classify.outputs.setup_only != 'true'")
        else:
            assert "&& needs.classify.outputs.thresholds_only != 'true'" in condition


SKIPPED_STEPS = {
    'Coverage summary', 'Upload coverage.xml',
    'Patch coverage against the merge base', 'Stage the patch-coverage comment',
    'Upload the patch-coverage comment', 'Coverage gate',
    'Install the JS toolchain from the lockfile', 'Comment share baseline',
    'JavaScript coverage gate', 'Module size baseline',
    'Pylint suppression baseline',
}


def test_canary_skips_only_the_unrelated_gates():
    jobs = _workflow('tests.yml')['jobs']
    steps = {step.get('name'): step for step in jobs['pytest']['steps']}
    for name in SKIPPED_STEPS:
        condition = steps[name]['if']
        assert "&& inputs.thresholds_only != 'true'" in condition, name
        assert "!cancelled() && steps.pytest.conclusion != 'skipped'" in condition
    for name in ('Reparse CPU gate', 'Threshold direction guard',
                 'Upload the suite measurement', 'Ratchet the thresholds'):
        assert 'thresholds_only' not in steps[name]['if'], name
    assert jobs['pytest-portable']['if'] == (
        "inputs.comment_only != 'true' && inputs.thresholds_only != 'true'")


@pytest.mark.skipif(os.name == 'nt', reason='the canary runs under POSIX Bash')
@pytest.mark.parametrize('narrowed', [True, False])
def test_canary_run_tests_selects_threshold_checks_without_coverage(
        narrowed, tmp_path):
    step = next(step for step in _workflow('tests.yml')['jobs']['pytest']['steps']
                if step.get('name') == 'Run tests')
    assert step.get('env', {}).get('CLAUDIT_THRESHOLDS_ONLY') == (
        '${{ inputs.thresholds_only }}')
    fake_python = tmp_path / 'python'
    fake_python.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGV_CAPTURE"\n')
    fake_python.chmod(0o755)
    capture = tmp_path / 'argv'
    env = dict(os.environ, PATH=f'{tmp_path}{os.pathsep}{os.environ["PATH"]}',
               CLAUDIT_THRESHOLDS_ONLY=str(narrowed).lower(), ARGV_CAPTURE=str(capture))
    subprocess.run(['bash', '-e', '-c', step['run']], cwd=ROOT, env=env,
                   check=True, capture_output=True, text=True)
    args = capture.read_text().splitlines()
    if narrowed:
        listed = (ROOT / 'scripts/ci/thresholds_test_files.txt').read_text().splitlines()
        files = [line for line in listed if line and not line.startswith('#')]
        assert files
        assert args == ['-m', 'pytest', *files, '-q', '--tb=short', '-ra']
    else:
        assert args == ['-m', 'pytest', 'tests/', '-q', '--tb=short', '-ra',
                        '--cov=backend', '--cov-report=term']


def test_thresholds_file_list_matches_tests_using_the_loader():
    loader = (ROOT / 'scripts/ci/thresholds.py').stem
    derived = []
    for path in sorted((ROOT / 'tests').glob('test_*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        uses_loader = any(
            (isinstance(node, ast.Constant) and node.value == loader)
            or (isinstance(node, ast.Import)
                and any(alias.name.split('.')[-1] == loader for alias in node.names))
            or (isinstance(node, ast.ImportFrom)
                and ((node.module or '').split('.')[-1] == loader
                     or any(alias.name == loader for alias in node.names)))
            for node in ast.walk(tree))
        exercises_document = any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in {'load', 'write', 'normalise', 'in_flight'}
            for node in ast.walk(tree))
        if uses_loader and exercises_document:
            derived.append(path.relative_to(ROOT).as_posix())
    listed = (ROOT / 'scripts/ci/thresholds_test_files.txt').read_text().splitlines()
    files = [line for line in listed if line and not line.startswith('#')]
    assert files == derived
