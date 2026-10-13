from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'ci'))
schema = importlib.import_module('thresholds_validate')
phases = importlib.import_module('suite_phases')
thresholds = importlib.import_module('thresholds')


def _path_instructions(path):
    counter = phases.InstructionCounter()
    assert counter.available
    try:
        with counter.window('run'):
            assert schema.module_path(path) == path
        return counter.total
    finally:
        counter.close()


def test_repeated_path_validation_reuses_work_without_accepting_changed_input():
    path = 'unique-cached-component-for-instruction-test/x.py'
    cold = _path_instructions(path)
    assert _path_instructions(path) < cold / 2
    for changed in ('unique-cached-component-for-instruction-test/../x.py',
                    path + '\x00', path + '/CON', path + '\\other'):
        with pytest.raises(ValueError):
            schema.module_path(changed)


def _pair_instructions():
    counter = phases.InstructionCounter()
    assert counter.available
    try:
        with counter.window('run'):
            pair = schema.comment_share_value([1, 2], 'unique-cache-byte-pair')
        return counter.total, pair
    finally:
        counter.close()


def test_pair_validation_reuses_work_and_returns_independent_values():
    cold, pair = _pair_instructions()
    pair[0] = 0
    warm, fresh = _pair_instructions()
    assert warm < cold / 2
    assert fresh == [1, 2]
    assert schema.comment_share_value([2, 3], 'unique-cache-byte-pair') == [2, 3]
    for changed in ([True, 2], [0, 2], [1, 0], [1.5, 2], [{}, 2]):
        with pytest.raises(ValueError):
            schema.comment_share_value(changed, 'unique-cache-byte-pair')


def _decoded_instructions(target, profile_file):
    counter = phases.InstructionCounter()
    assert counter.available
    try:
        with counter.window('run'):
            document = thresholds.load(target, False)
    finally:
        counter.close()
    rows = json.loads(profile_file.read_text())
    return sum(sum(row['counts'].values()) for row in rows
               if row['function'] == '_object_pairs'), document


def test_unchanged_bytes_decode_once_with_fresh_results_and_live_validation(tmp_path, monkeypatch):
    target = tmp_path / 'thresholds.json'
    data = thresholds.load()
    data['comment_share_baseline'] = {'unique-byte-cache.py': [20, 100]}
    thresholds.write(target, data, False)
    profile = tmp_path / 'profile.json'
    monkeypatch.setenv('DEBUG_SUITE_INSTRUCTIONS', str(profile))
    cold, first = _decoded_instructions(target, profile)
    assert cold > 0
    first['comment_share_baseline']['unique-byte-cache.py'][0] = 0
    warm, fresh = _decoded_instructions(target, profile)
    assert warm == 0
    rows = json.loads(profile.read_text())
    assert not any(row['function'] == 'normalise' and Path(row['file']).name == 'thresholds.py'
                   for row in rows)
    assert fresh['comment_share_baseline'] == {'unique-byte-cache.py': [20, 100]}
    payload = json.loads(target.read_text())
    payload['comment_share_baseline']['unique-byte-cache.py'] = [True, 2]
    target.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError):
        thresholds.load(target, False)

    del payload['suite_cost']
    payload['comment_share_baseline'] = {}
    target.write_text(json.dumps(payload), encoding='utf-8')
    assert 'suite_cost' not in thresholds.load(target, True)
    with pytest.raises(ValueError):
        thresholds.load(target, False)
