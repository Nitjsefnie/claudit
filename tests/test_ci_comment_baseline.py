from __future__ import annotations

import copy
import importlib
import json
import sys
from pathlib import Path

import pytest
import yaml

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
    ((40, 200), {'x.py': [20, 100]}, False),
    ((0, 0), {}, False),
])
def test_exact_boundary_and_rise(counts, baseline, want):
    assert bool(policy().violations({'x.py': counts}, baseline)) is want


def test_tighten_lowers_drops_and_never_raises():
    baseline = {'lower.py': [30, 100], 'drop.py': [20, 100], 'rise.py': [20, 100]}
    measured = {'lower.py': (25, 100), 'drop.py': (15, 100), 'rise.py': (21, 100)}
    assert policy().tightened(baseline, measured) == {
        'lower.py': [25, 100], 'rise.py': [20, 100]}


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
        thresholds.normalise(data, False)


def test_section_required_and_paths_safe():
    data = thresholds.load()
    del data['comment_share_baseline']
    with pytest.raises(ValueError):
        thresholds.normalise(data, False)
    data['comment_share_baseline'] = {'../x.py': [20, 100]}
    with pytest.raises(ValueError):
        thresholds.normalise(data, False)


def test_cli_tighten_and_matches_tree(tmp_path, monkeypatch):
    module = policy()
    target = tmp_path / 'thresholds.json'
    data = thresholds.load()
    data['comment_share_baseline'] = {'x.py': [30, 100]}
    thresholds.write(target, data)
    monkeypatch.setattr(module.comment_share, 'measure_files', lambda _root: {'x.py': (20, 100)})
    args = ['--thresholds', str(target)]
    assert module.main(args) == 0
    assert module.main([*args, '--matches-tree']) == 1
    assert module.main([*args, '--tighten']) == 0
    assert thresholds.comment_share_baseline(thresholds.load(target)) == {'x.py': [20, 100]}
    assert module.main([*args, '--matches-tree']) == 0
    monkeypatch.setattr(module.comment_share, 'measure_files', lambda _root: {'x.py': (40, 200)})
    assert module.main([*args, '--matches-tree']) == 1
    assert module.main([*args, '--tighten']) == 0
    assert thresholds.comment_share_baseline(thresholds.load(target)) == {'x.py': [40, 200]}
    monkeypatch.setattr(module.comment_share, 'measure_files', lambda _root: {'x.py': (15, 100)})
    assert module.main([*args, '--tighten']) == 0
    assert thresholds.comment_share_baseline(thresholds.load(target)) == {}


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
                            "&& needs.classify.outputs.data_only != 'true'")
    job = workflow('tests.yml')['jobs']['pytest']
    assert 'if' not in job
    steps = job['steps']
    check = next(step for step in steps if step.get('name') == 'Comment share baseline')
    assert check['run'] == 'python3 scripts/ci/comment_baseline.py --matches-tree'
    assert check['if'] == "${{ !cancelled() && steps.pytest.conclusion != 'skipped' }}"
    install = next(step for step in steps if step.get('run') == 'npm ci --no-audit --no-fund')
    assert install['if'] == check['if']
    assert steps.index(install) < steps.index(check)
    assert any(step.get('with', {}).get('node-version') for step in steps[:steps.index(check)])
    assert any('-r requirements-test.txt' in step.get('run', '')
               for step in steps[:steps.index(check)])
    ratchet = next(step for step in steps if step.get('id') == 'ratchet')
    assert 'python3 scripts/ci/comment_baseline.py --tighten' in ratchet['run']
    assert "github.ref == 'refs/heads/master'" in ratchet['if']
