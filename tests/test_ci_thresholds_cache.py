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


def _decoded_instructions(target, profile_file):
    counter = phases.InstructionCounter()
    assert counter.available
    try:
        with counter.window('run'):
            document = thresholds.load(target)
    finally:
        counter.close()
    rows = json.loads(profile_file.read_text())
    return sum(sum(row['counts'].values()) for row in rows
               if row['function'] == '_object_pairs'), document


def test_unchanged_bytes_decode_once_with_fresh_results_and_live_validation(tmp_path, monkeypatch):
    target = tmp_path / 'thresholds.json'
    data = thresholds.load()
    data['module_size_baseline'] = {'unique-byte-cache.py': 100}
    thresholds.write(target, data)
    profile = tmp_path / 'profile.json'
    monkeypatch.setenv('CLAUDIT_DEBUG_SUITE_INSTRUCTIONS', str(profile))
    cold, first = _decoded_instructions(target, profile)
    assert cold > 0
    first['module_size_baseline']['unique-byte-cache.py'] = 0
    warm, fresh = _decoded_instructions(target, profile)
    assert warm == 0
    rows = json.loads(profile.read_text())
    assert not any(row['function'] == 'normalise' and Path(row['file']).name == 'thresholds.py'
                   for row in rows)
    assert fresh['module_size_baseline'] == {'unique-byte-cache.py': 100}
    payload = json.loads(target.read_text())
    payload['module_size_baseline']['unique-byte-cache.py'] = 0
    target.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError, match='module_size_baseline'):
        thresholds.load(target)
