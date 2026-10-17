"""Exercise the postinstall patcher against isolated package layouts."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ci/apply_converter_patch.py'


def _layout(root: Path, version: str = '9.3.0') -> tuple[Path, Path]:
    script = root / 'scripts/ci/apply_converter_patch.py'
    script.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT, script)
    target = root / 'node_modules/v8-to-istanbul'
    (target / 'lib').mkdir(parents=True)
    (target / 'package.json').write_text(json.dumps({'version': version}))
    patches = root / 'patches'
    patches.mkdir()
    patch = ''
    for name in ['probe.js', 'other.js']:
        (target / 'lib' / name).write_text('original\n')
        patch += (f'--- a/lib/{name}\n'
                  f'+++ b/lib/{name}\n'
                  '@@ -1 +1 @@\n-original\n+patched\n')
    (patches / 'v8-to-istanbul+9.3.0.patch').write_text(patch)
    return script, target


def _run(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(script)], check=False,
                          capture_output=True, text=True)


def test_applies_patch_to_its_own_root_from_another_cwd(tmp_path, monkeypatch):
    script, target = _layout(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert (target / 'lib/probe.js').read_text() == 'patched\n'
    assert (target / 'lib/other.js').read_text() == 'patched\n'


def test_second_apply_leaves_patched_files_untouched(tmp_path):
    script, target = _layout(tmp_path)
    assert _run(script).returncode == 0
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
              for p in (target / 'lib').iterdir()}
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert 'already patched' in result.stdout.lower()
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                      for p in (target / 'lib').iterdir()}


@pytest.mark.parametrize('patched', [False, True])
def test_rejects_version_drift_even_if_already_patched(tmp_path, patched):
    script, target = _layout(tmp_path, version='9.3.9')
    for path in (target / 'lib').iterdir():
        path.write_text('patched\n' if patched else 'original\n')
    before = {p.name: p.read_bytes() for p in (target / 'lib').iterdir()}
    result = _run(script)
    assert result.returncode != 0
    assert '9.3.9' in result.stderr and 'expected 9.3.0' in result.stderr
    assert before == {p.name: p.read_bytes() for p in (target / 'lib').iterdir()}


def test_failed_dry_run_does_not_apply_an_earlier_matching_hunk(tmp_path):
    script, target = _layout(tmp_path)
    (target / 'lib/other.js').write_text('incompatible\n')
    result = _run(script)
    assert result.returncode != 0
    assert 'expected 9.3.0' in result.stderr
    assert (target / 'lib/probe.js').read_text() == 'original\n'
    assert (target / 'lib/other.js').read_text() == 'incompatible\n'


def test_surfaces_patch_stderr_for_invalid_patch_input(tmp_path):
    script, target = _layout(tmp_path)
    (tmp_path / 'patches/v8-to-istanbul+9.3.0.patch').write_text('invalid\n')
    result = _run(script)
    assert result.returncode != 0
    # patch(1) uses different wording on GNU and BSD (macOS).
    assert result.stderr
    assert (target / 'lib/probe.js').read_text() == 'original\n'
