from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'ci'))
suite_phases = importlib.import_module('suite_phases')


def _work():
    return sum(range(8))


def _measure():
    counter = suite_phases.InstructionCounter()
    assert counter.available
    try:
        with counter.window('run'):
            assert _work() == 28
        return counter.total
    finally:
        counter.close()


def test_profile_partitions_the_same_instruction_events(tmp_path, monkeypatch):
    monkeypatch.delenv('CLAUDIT_DEBUG_SUITE_INSTRUCTIONS', raising=False)
    expected = _measure()
    target = tmp_path / 'profile.json'
    monkeypatch.setenv('CLAUDIT_DEBUG_SUITE_INSTRUCTIONS', str(target))
    assert _measure() == expected
    rows = json.loads(target.read_text())
    assert sum(sum(row['counts'].values()) for row in rows) == expected
    assert any(row['function'] == '_work' and row['counts']['run'] for row in rows)


def test_profile_keeps_identical_code_in_different_files_separate(tmp_path):
    module = importlib.import_module('suite_instruction_profile')
    target = tmp_path / 'profile.json'
    profile = module.InstructionProfile(str(target))
    for filename in ('first.py', 'second.py'):
        code = compile('def work():\n    return 1\n', filename, 'exec').co_consts[0]
        profile.count(code, 'run')
    profile.write()
    rows = json.loads(target.read_text())
    assert {row['file'] for row in rows} == {'first.py', 'second.py'}


def test_unset_profile_flag_creates_imports_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv('CLAUDIT_DEBUG_SUITE_INSTRUCTIONS', raising=False)
    script = """
import json, sys
sys.path.insert(0, sys.argv[1])
import suite_phases
counter = suite_phases.InstructionCounter()
try:
    created = counter._profile is not None
    with counter.window('run'):
        sum(range(8))
finally:
    counter.close()
print(json.dumps([created, 'suite_instruction_profile' in sys.modules]))
"""
    target = tmp_path / 'enabled.json'
    for enabled, expected in ((False, [False, False]), (True, [True, True])):
        env = dict(os.environ)
        if enabled:
            env['CLAUDIT_DEBUG_SUITE_INSTRUCTIONS'] = str(target)
        result = subprocess.run([sys.executable, '-c', script,
                                 str(Path(__file__).resolve().parents[1] / 'scripts/ci')], cwd=tmp_path,
                                env=env, text=True, capture_output=True, check=True)
        assert json.loads(result.stdout) == expected
        assert sorted(path.name for path in tmp_path.iterdir()) == (['enabled.json'] if enabled else [])
        if enabled:
            assert json.loads(target.read_text())
