from __future__ import annotations

import copy
import importlib
import json
import shlex
import sys
from pathlib import Path

import pytest
import yaml

from tests.thresholds_history import stub_history

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'ci'))
thresholds = importlib.import_module('thresholds')
guard = importlib.import_module('thresholds_guard')


def policy():
    return importlib.import_module('comment_baseline')


@pytest.mark.parametrize('counts,baseline,want', [
    ((15, 100), {}, False),
    ((149999999999999999, 1000000000000000000), {}, False),
    ((150000000000000001, 1000000000000000000), {}, True),
    ((20, 100), {'x.py': [19, 100]}, True),
    ((25, 100), {'x.py': [30, 100]}, False),
    ((35, 200), {'x.py': [20, 100]}, False),
    ((15, 100), {'x.py': [20, 100]}, False),
    ((40, 200), {'x.py': [20, 100]}, False),
    ((14, 100), {'x.py': [20, 100]}, False),
    ((0, 0), {}, False),
])
def test_exact_boundary_and_rise(counts, baseline, want):
    assert bool(policy().violations({'x.py': counts}, baseline)) is want


def test_tighten_lowers_drops_and_never_raises():
    baseline = {'lower.py': [30, 100], 'drop.py': [20, 100], 'rise.py': [20, 100]}
    measured = {'lower.py': (25, 100), 'drop.py': (15, 100), 'rise.py': (21, 100)}
    assert policy().tightened(baseline, measured) == {
        'lower.py': [25, 100], 'rise.py': [20, 100]}


@pytest.mark.parametrize('entry,recorded,sibling,status', [
    ('comment_share_baseline.backend/api.py', True, False, 0),
    ('comment_share_baseline', True, False, 0),
    ('', True, False, 1),
    ('comment_share_baseline.backend/other.py', True, False, 1),
    ('comment_share_baseline', True, True, 1),
    ('comment_share_baseline.backend/api.py', False, False, 1),
])
def test_declared_removal_check(tmp_path, monkeypatch, capsys, entry, recorded, sibling, status):
    module = policy()
    target = tmp_path / 'thresholds.json'
    thresholds.write(target, {'schema_version': 1, 'comment_share_baseline': {}})
    measured = {'backend/api.py': (20, 100)}
    if sibling:
        measured['backend/new.py'] = (20, 100)
    monkeypatch.setattr(module.comment_share, 'measure_files', lambda _root: measured)
    base = {'schema_version': 1, 'comment_share_baseline':
            {'backend/api.py': [30, 100]} if recorded else {}}
    stub_history(monkeypatch, module, ROOT, f'Thresholds-Removed: {entry}=bug', base)
    assert module.main(['--thresholds', str(target),
                        '--base-rev', 'base', '--head-rev', 'HEAD']) == status
    captured = capsys.readouterr()
    if status == 0:
        assert 'declared-removed: backend/api.py' in captured.out
        assert '(20, 100)' in captured.out
    else:
        assert 'backend/' in captured.err
        if sibling:
            assert 'backend/new.py' in captured.err
            assert 'declared-removed: backend/api.py' in captured.out


def test_declaration_does_not_exempt_recorded_rise():
    assert policy().violations(
        {'backend/api.py': (30, 100)}, {'backend/api.py': [20, 100]},
        declared_removed={'backend/api.py'}) == {
            'backend/api.py': '(30, 100) exceeds [20, 100]'}


@pytest.mark.parametrize('after', [{'x.py': [21, 100]}, {'run.sh': [20, 100]}])
def test_guard_refuses_rise_and_added_entry(after):
    base = thresholds.load()
    base['comment_share_baseline'] = {'x.py': [20, 100]}
    head = copy.deepcopy(base)
    head['comment_share_baseline'] = after
    assert guard.forbidden_moves(base, head, set(thresholds.BASELINE_MEMBERS))


def test_guard_allows_equal_fraction_and_initial_seed():
    base = thresholds.load()
    base['comment_share_baseline'] = {'x.py': [20, 100]}
    head = copy.deepcopy(base)
    head['comment_share_baseline'] = {'x.py': [40, 200]}
    assert not guard.forbidden_moves(base, head, set(thresholds.BASELINE_MEMBERS))
    base['comment_share_baseline'] = {}
    head['comment_share_baseline'] = {'run.sh': [20, 100]}
    assert not guard.forbidden_moves(
        base, head, set(thresholds.BASELINE_MEMBERS) - {'comment_share_baseline'})


@pytest.mark.parametrize('entry', [[15, 100], [101, 100], [20.5, 100],
                                   [True, 100], [20, 0], [20], '20%', [20, 100, 1]])
def test_invalid_entry_refused(entry):
    data = thresholds.load()
    data['comment_share_baseline'] = {'x.py': entry}
    with pytest.raises(ValueError):
        thresholds.normalise(data)


@pytest.mark.parametrize('absent_family', [False, True])
def test_section_is_optional_and_paths_safe(absent_family):
    data = (thresholds.normalise({'schema_version': 1})
            if absent_family else thresholds.load())
    data.pop('comment_share_baseline', None)
    assert 'comment_share_baseline' not in thresholds.normalise(data)
    data['comment_share_baseline'] = {'../x.py': [20, 100]}
    with pytest.raises(ValueError):
        thresholds.normalise(data)


def test_pr_check_accepts_lower_shares_and_master_tightens(tmp_path, monkeypatch):
    module = policy()
    target = tmp_path / 'thresholds.json'
    data = thresholds.load()
    data['comment_share_baseline'] = {'x.py': [30, 100]}
    thresholds.write(target, data)
    monkeypatch.setattr(module.comment_share, 'measure_files', lambda _root: {'x.py': (20, 100)})
    steps = yaml.load((ROOT / '.github/workflows/tests.yml').read_text(), Loader=yaml.BaseLoader)['jobs']['pytest']['steps']
    check = next(step for step in steps if step.get('name') == 'Comment share baseline')
    args = [arg.replace('$CLAUDIT_THRESHOLDS_BASE_REV', '')
            for arg in shlex.split(check['run'])[2:]] + ['--thresholds', str(target)]
    assert module.main(args) == 0
    assert module.main([*args, '--tighten']) == 0
    assert thresholds.comment_share_baseline(thresholds.load(target)) == {'x.py': [20, 100]}
    assert module.main(args) == 0
    monkeypatch.setattr(module.comment_share, 'measure_files', lambda _root: {'x.py': (40, 200)})
    assert module.main(args) == 0
    assert module.main([*args, '--tighten']) == 0
    assert thresholds.comment_share_baseline(thresholds.load(target)) == {'x.py': [40, 200]}
    monkeypatch.setattr(module.comment_share, 'measure_files', lambda _root: {'x.py': (15, 100)})
    assert module.main(args) == 0
    assert module.main([*args, '--tighten']) == 0
    assert thresholds.comment_share_baseline(thresholds.load(target)) == {}
    assert module.main(args) == 0


def test_seed_uses_writer_and_refuses_second_seed(tmp_path, monkeypatch):
    module = policy()
    target = tmp_path / 'thresholds.json'
    data = json.loads(thresholds.THRESHOLDS.read_bytes())
    data.pop('comment_share_baseline', None)
    target.write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setattr(module.comment_share, 'measure_files', lambda _root: {
        'x.py': (20, 100), 'small.py': (15, 100)})
    args = ['--thresholds', str(target), '--seed']
    assert module.main(args) == 0
    assert thresholds.load(target)['comment_share_baseline'] == {'x.py': [20, 100]}
    assert module.main(args) == 1


def test_comment_only_runs_check_with_dependencies():
    def workflow(name):
        return yaml.load((ROOT / '.github' / 'workflows' / name).read_text(),
                         Loader=yaml.BaseLoader)

    caller = workflow('ci-gate.yml')['jobs']['tests']
    assert caller['if'] == ("needs.classify.outputs.docs_only != 'true' "
                            "&& needs.classify.outputs.data_only != 'true' "
                            "&& needs.classify.outputs.setup_only != 'true'")
    job = workflow('tests.yml')['jobs']['pytest']
    assert 'if' not in job
    steps = job['steps']
    checks = [step for step in steps if step.get('name') == 'Comment share baseline']
    assert len(checks) == 1
    check = checks[0]
    assert shlex.split(check['run']) == [
        'python3', 'scripts/ci/comment_baseline.py',
        '--base-rev', '$CLAUDIT_THRESHOLDS_BASE_REV', '--head-rev', 'HEAD']
    assert check['if'] == (
        "${{ !cancelled() && steps.pytest.conclusion != 'skipped' "
        "&& inputs.thresholds_only != 'true' }}")
    # The lockfile install step also overlays the patched converter (#1065).
    installs = [step for step in steps if next(
        (line.strip() for line in step.get('run', '').splitlines() if line.strip()),
        '') == 'npm ci --no-audit --no-fund']
    assert len(installs) == 1
    install = installs[0]
    assert install['if'] == check['if']
    assert steps.index(install) + 1 == steps.index(check)
    assert any('-r requirements-test.txt' in step.get('run', '')
               for step in steps[:steps.index(check)])
    ratchet = next(step for step in steps if step.get('id') == 'ratchet')
    assert 'python3 scripts/ci/comment_baseline.py --tighten' in ratchet['run']
    assert "github.ref == 'refs/heads/master'" in ratchet['if']
    assert "github.event_name == 'push'" in ratchet['if']


def test_deleted_entry_passes_pr_and_master_tighten_removes_it(tmp_path, monkeypatch):
    module = policy()
    target = tmp_path / 'thresholds.json'
    data = thresholds.load()
    data['comment_share_baseline'] = {'gone.py': [20, 100]}
    thresholds.write(target, data)
    monkeypatch.setattr(module.comment_share, 'measure_files', lambda _root: {})
    args = ['--thresholds', str(target)]
    assert module.main(args) == 0
    assert module.main([*args, '--tighten']) == 0
    assert thresholds.comment_share_baseline(thresholds.load(target)) == {}


@pytest.mark.parametrize('state', ['absent', 'empty', 'recorded', 'declared-removal'])
def test_tighten_seeds_above_limit_pairs(tmp_path, monkeypatch, state):
    module = policy()
    data = {'schema_version': 1, 'comment_share_baseline': {}}
    measured = {'new.py': (31, 200), 'at.py': (15, 100),
                'under.py': (14, 100), 'empty.py': (0, 0)}
    expected = {'new.py': [31, 200]}
    if state == 'absent':
        del data['comment_share_baseline']
    elif state == 'recorded':
        data['comment_share_baseline'] = {
            'shrunk.py': [30, 100], 'grown.py': [20, 100],
            'gone.py': [20, 100], 'graduated.py': [20, 100]}
        measured.update({'shrunk.py': (25, 100), 'grown.py': (21, 100),
                         'graduated.py': (15, 100)})
        expected.update({'shrunk.py': [25, 100], 'grown.py': [20, 100]})
    target = tmp_path / 'thresholds.json'
    thresholds.write(target, data)
    monkeypatch.setattr(module.comment_share, 'measure_files', lambda _root: measured)
    if state == 'declared-removal':
        base = dict(data, comment_share_baseline={'new.py': [40, 100]})
        stub_history(monkeypatch, module, ROOT, 'Thresholds-Removed: comment_share_baseline=definition', base)
        removed = module.thresholds_removed.declared('base', 'HEAD', ROOT)
        assert not guard.forbidden_moves(
            base, data, set(thresholds.BASELINE_MEMBERS), removal_declarations=removed)
        assert module.main(['--thresholds', str(target), '--base-rev', 'base']) == 0
    assert module.main(['--tighten', '--thresholds', str(target)]) == 0
    assert thresholds.comment_share_baseline(thresholds.load(target)) == expected
