"""Run the coverage converter against actual Node V8 captures."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[1]
CONVERTER = REPO / 'scripts/ci/js_coverage.mjs'


def _write(root, name, code):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding='utf-8')


def _capture(root, name, *args):
    subprocess.run(["node", str(root / name), *args], cwd=root,
                   env={**os.environ, 'NODE_V8_COVERAGE': str(root / 'v8')},
                   check=True, capture_output=True, text=True, timeout=30)


def _report(root, *args):
    return subprocess.run(
        ["node", str(CONVERTER), '--temp-directory', str(root / 'v8'), *args],
        cwd=root, check=False, capture_output=True, text=True, timeout=30)


def _summary(root):
    return json.loads((root / 'coverage/coverage-summary.json').read_text())


class TestConverter:
    pytestmark = pytest.mark.skipif(
        not (REPO / 'node_modules').exists(), reason='JS toolchain not installed')

    def test_selects_only_executed_included_files_with_default_excludes(self, tmp_path):
        for name in ['src/main.js', 'src/nested/chosen.js', 'src/unused.js',
                     'src/main.test.js', 'src/extra.cjs', 'src/node_modules/helper.js',
                     'outside.js']:
            _write(tmp_path, name, 'module.exports = 1;\n')
        _write(tmp_path, 'run.cjs', "\n".join(
            f"require('./{name}');" for name in [
                'src/main.js', 'src/nested/chosen.js', 'src/main.test.js',
                'src/extra.cjs', 'src/node_modules/helper.js', 'outside.js']))
        _capture(tmp_path, 'run.cjs')
        (tmp_path / 'v8/noise.json').write_text('partial JSON')
        result = _report(tmp_path)
        assert result.returncode == 0, result.stderr
        summary = _summary(tmp_path)
        assert set(summary) == {'total', str(tmp_path / 'src/main.js'),
                                str(tmp_path / 'src/nested/chosen.js')}
        assert summary['total']['lines'] == {
            'total': 2, 'covered': 2, 'skipped': 0, 'pct': 100}
        for file_summary in summary.values():
            assert {'lines', 'statements', 'functions', 'branches'} <= file_summary.keys()
            for metric in file_summary.values():
                assert set(metric) == {'total', 'covered', 'skipped', 'pct'}

    def test_merges_complementary_process_captures(self, tmp_path):
        _write(tmp_path, 'src/main.js', """if (process.argv[2] === 'left') {
  console.log('left');
} else {
  console.log('right');
}
""")
        _capture(tmp_path, 'src/main.js', 'left')
        assert _report(tmp_path).returncode == 0
        assert _summary(tmp_path)['total']['lines']['pct'] < 100
        _capture(tmp_path, 'src/main.js', 'right')
        result = _report(tmp_path)
        assert result.returncode == 0, result.stderr
        assert _summary(tmp_path)['total']['lines']['pct'] == 100

    def test_enforces_the_optional_line_floor_after_writing_the_report(self, tmp_path):
        _write(tmp_path, 'src/main.js', 'if (false) {\n  console.log(1);\n}\n')
        _capture(tmp_path, 'src/main.js')
        result = _report(tmp_path, '--lines', '0')
        assert result.returncode == 0, result.stderr
        before = _summary(tmp_path)
        assert before['total']['lines']['pct'] < 100
        result = _report(tmp_path, '--lines', '100')
        assert result.returncode == 1
        assert result.stderr
        assert _summary(tmp_path) == before

    def test_uses_the_capture_directory_environment(self, tmp_path):
        _write(tmp_path, 'src/main.js', 'const value = 1;\n')
        _capture(tmp_path, 'src/main.js')
        result = subprocess.run(
            ["node", str(CONVERTER)], cwd=tmp_path,
            env={**os.environ, 'NODE_V8_COVERAGE': str(tmp_path / 'v8')},
            check=False, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert _summary(tmp_path)['total']['lines']['pct'] == 100
