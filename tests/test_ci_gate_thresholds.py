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
    (['.github/comment-share.json'], True),
    ([THRESHOLDS, '.github/comment-share.json'], True),
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
        assert outputs['reason'] == f'ratchet data changed: {len(paths)} path(s)'


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


def test_canary_skips_only_the_unrelated_gates():
    jobs = _workflow('tests.yml')['jobs']
    for name in ('pytest-shards', 'js-coverage'):
        assert jobs[name]['if'] == "inputs.thresholds_only != 'true'"
    assert jobs['coverage-combine']['needs'] == ['pytest-shards']
    assert jobs['coverage-combine']['if'] == (
        "${{ !cancelled() && inputs.thresholds_only != 'true' }}")
    assert jobs['pytest-thresholds']['if'] == "inputs.thresholds_only == 'true'"
    assert jobs['reparse-gate']['if'] == "${{ !cancelled() }}"
    assert jobs['baselines']['if'] == "${{ !cancelled() }}"
    assert 'needs' not in jobs['baselines']
    assert jobs['size-baseline']['if'] == (
        "${{ !cancelled() && inputs.thresholds_only != 'true' }}")
    steps = {step.get('name'): step for step in jobs['baselines']['steps']}
    for name in ('Pylint suppression baseline',):
        assert steps[name]['if'] == "${{ !cancelled() && inputs.thresholds_only != 'true' }}"
    assert steps['Threshold direction guard']['if'] == (
        "${{ !cancelled() && (github.event_name == 'pull_request' || github.event_name == 'push') }}")
    for step in jobs['js-coverage']['steps']:
        if step.get('name') == 'JavaScript coverage gate':
            assert step['if'] == '${{ !cancelled() }}'
    assert jobs['pytest-portable']['if'] == (
        "inputs.comment_only != 'true' && inputs.thresholds_only != 'true'")


def test_shards_cover_four_partitions_and_publish_even_after_failure():
    job = _workflow('tests.yml')['jobs']['pytest-shards']
    assert job['strategy'] == {'fail-fast': 'false', 'matrix': {'shard': ['1', '2', '3', '4']}}
    run, = [step for step in job['steps'] if step.get('name') == 'Run tests']
    commands = [line for line in run['run'].splitlines() if not line.startswith('#')]
    assert commands == [
        'python -m pytest $(python3 scripts/ci/pytest_shards.py '
        '--shard ${{ matrix.shard }} --of 4) -q --tb=short -ra --cov=backend --cov-report=']
    assert run['env']['COVERAGE_FILE'] == (
        '${{ github.workspace }}/shard-coverage/.coverage.${{ matrix.shard }}')
    upload, = [step for step in job['steps']
               if step.get('with', {}).get('name') == 'coverage-shard-${{ matrix.shard }}']
    assert upload['if'] == '${{ !cancelled() }}'


def _threshold_commands(tmp_path):
    for executable in ('git', 'python3'):
        fake = tmp_path / executable
        fake.write_text(
            '#!/bin/sh\n'
            'printf "%s %s\\n" "${0##*/}" "$*" >> "$CALLS"\n'
            'if [ "$1" = fetch ]; then exit 0; fi\n'
            'printf "%s\\n" "$RESOLVED"\n'
            'if [ "$1" = scripts/ci/threshold_base.py ]; then exit "$RESOLVER_STATUS"; fi\n')
        fake.chmod(0o755)


def _threshold_base_action():
    return yaml.load((ROOT / '.github/actions/thresholds-base/action.yml').read_text(
        encoding='utf-8'), Loader=yaml.BaseLoader)


@pytest.mark.parametrize('workflow,job_name,test_step', [
    ('tests.yml', 'pytest-shards', 'Run tests'),
    ('tests.yml', 'pytest-thresholds', 'Run tests'),
    ('tests.yml', 'pytest-portable', 'Run tests'),
    ('tests.yml', 'coverage-combine', 'Combine shard coverage'),
    ('tests.yml', 'js-coverage', 'JavaScript coverage gate'),
    ('tests.yml', 'reparse-gate', 'Reparse CPU gate'),
    ('tests.yml', 'suite-bench', 'Measure and gate the suite cost'),
    ('tests.yml', 'ratchet', 'Ratchet the thresholds'),
    ('test-data.yml', 'perturbed-suite', 'Perturb and test'),
    ('test-data-explore.yml', 'perturbed-suite-explore', 'Perturb and test, per wall-clock seed'),
])
def test_threshold_base_action_runs_once_per_job_before_tests(workflow, job_name, test_step):
    job = _workflow(workflow)['jobs'][job_name]
    steps = job['steps']
    resolvers = [step for step in steps
                 if step.get('uses') == '$/.github/actions/thresholds-base']
    assert len(resolvers) == 1
    resolver = resolvers[0]
    assert resolver['env'] == {
        'CLAUDIT_REPO': '${{ github.repository }}',
        'CLAUDIT_BASE_REF': '${{ github.event.pull_request.base.ref }}',
        'CLAUDIT_EVENT': '${{ github.event_name }}',
        'CLAUDIT_BEFORE': '${{ github.event.before }}',
    }
    assert 'if' not in resolver
    assert steps.index(resolver) < next(i for i, step in enumerate(steps)
                                        if step.get('name') == test_step)
    checkout = next(step for step in steps
                    if step.get('uses', '').startswith('actions/checkout@'))
    assert steps.index(checkout) < steps.index(resolver)
    assert all('case "$CLAUDIT_EVENT"' not in step.get('run', '') for step in steps)
    action = _threshold_base_action()
    assert action['runs']['using'] == 'composite'
    assert len(action['runs']['steps']) == 1
    assert action['runs']['steps'][0]['shell'] == 'bash'
    assert '${{' not in action['runs']['steps'][0]['run']


@pytest.mark.skipif(os.name == 'nt', reason='the canary runs under POSIX Bash')
@pytest.mark.parametrize('event,status,expected', [
    ('pull_request', 0, 'a' * 40), ('push', 0, 'a' * 40),
    ('push', 3, ''), ('push', 1, None), ('workflow_dispatch', 0, ''),
])
def test_threshold_base_resolved_before_tests_and_shared(event, status, expected, tmp_path):
    steps = _workflow('tests.yml')['jobs']['baselines']['steps']
    resolver = _threshold_base_action()['runs']['steps'][0]
    _threshold_commands(tmp_path)
    exported, calls = tmp_path / 'env', tmp_path / 'calls'
    env = dict(os.environ, PATH=f'{tmp_path}{os.pathsep}{os.environ["PATH"]}',
               CLAUDIT_EVENT=event, CLAUDIT_REPO='o/r', CLAUDIT_BASE_REF='master', CLAUDIT_BEFORE='b' * 40,
               RESOLVED='a' * 40, RESOLVER_STATUS=str(status),
               GITHUB_ENV=str(exported), CALLS=str(calls), RUNNER_TEMP=str(tmp_path))
    result = subprocess.run(['bash', '-e', '-c', resolver['run']], cwd=ROOT,
                            env=env, check=False, capture_output=True, text=True)
    assert result.returncode == (1 if expected is None else 0), result.stderr
    guard_step = next(step for step in steps if step.get('name') == 'Threshold direction guard')
    if expected is None:
        assert not exported.exists()
        env.pop('CLAUDIT_THRESHOLDS_BASE_REV', None)
        result = subprocess.run(['bash', '-e', '-c', guard_step['run']], cwd=ROOT,
                                env=env, check=False, capture_output=True, text=True)
        assert result.returncode != 0
        return
    assert exported.read_text() == f'CLAUDIT_THRESHOLDS_BASE_REV={expected}\n'
    if event == 'pull_request':
        assert calls.read_text().splitlines() == [
            'git fetch --no-tags https://github.com/o/r.git refs/heads/master',
            'git merge-base HEAD FETCH_HEAD']
    elif event == 'push':
        assert calls.read_text().splitlines() == [
            f'python3 scripts/ci/threshold_base.py {"b" * 40} .github/ci-thresholds.json']
    env['CLAUDIT_THRESHOLDS_BASE_REV'] = expected
    for name, script in [('Pylint suppression baseline',
                          'suppression_baseline')]:
        step = next(step for job in _workflow('tests.yml')['jobs'].values()
                    for step in job['steps'] if step.get('name') == name)
        subprocess.run(['bash', '-e', '-c', step['run']], cwd=ROOT, env=env,
                       check=True, capture_output=True, text=True)
        assert calls.read_text().splitlines()[-1] == (
            f'python3 scripts/ci/{script}.py --base-rev {expected} --head-rev HEAD')
    previous = calls.read_text().splitlines()
    subprocess.run(['bash', '-e', '-c', guard_step['run']], cwd=ROOT, env=env,
                   check=True, capture_output=True, text=True)
    assert calls.read_text().splitlines()[len(previous):] == ([
        f'git show {expected}:.github/ci-thresholds.json',
        f'python3 scripts/ci/thresholds_guard.py --base {tmp_path}/base-thresholds.json '
        f'--head .github/ci-thresholds.json --base-rev {expected} --head-rev HEAD',
    ] if expected else [])


@pytest.mark.skipif(os.name == 'nt', reason='the canary runs under POSIX Bash')
def test_thresholds_job_selects_threshold_checks_without_coverage(tmp_path):
    step = next(step for step in _workflow('tests.yml')['jobs']['pytest-thresholds']['steps']
                if step.get('name') == 'Run tests')
    fake_python = tmp_path / 'python'
    fake_python.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGV_CAPTURE"\n')
    fake_python.chmod(0o755)
    capture = tmp_path / 'argv'
    env = dict(os.environ, PATH=f'{tmp_path}{os.pathsep}{os.environ["PATH"]}',
               ARGV_CAPTURE=str(capture))
    subprocess.run(['bash', '-e', '-c', step['run']], cwd=ROOT, env=env,
                   check=True, capture_output=True, text=True)
    listed = (ROOT / 'scripts/ci/thresholds_test_files.txt').read_text().splitlines()
    files = [line for line in listed if line and not line.startswith('#')]
    assert files
    assert capture.read_text().splitlines() == ['-m', 'pytest', *files, '-q', '--tb=short', '-ra']


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
