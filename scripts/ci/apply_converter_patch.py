#!/usr/bin/env python3
"""Apply the pinned coverage-converter patch without a package-manager helper."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


EXPECTED_VERSION = '9.3.0'


def _patch(target: Path, text: str, *, reverse: bool = False,
           dry_run: bool = True) -> subprocess.CompletedProcess[str]:
    command = ['patch', '--batch', '--forward', '-p1', '-d', str(target)]
    if reverse:
        command.append('--reverse')
    if dry_run:
        command.append('--dry-run')
    return subprocess.run(command, input=text, text=True, capture_output=True,
                          check=False)


def _error(target: Path, version: str, detail: str) -> int:
    print(f'Converter patch failed for {target} '
          f'(target version {version}, expected {EXPECTED_VERSION}):\n{detail}',
          file=sys.stderr)
    return 1


def main() -> int:
    root = Path(__file__).resolve().parent.parents[1]
    target = root / 'node_modules/v8-to-istanbul'
    patch_file = root / f'patches/v8-to-istanbul+{EXPECTED_VERSION}.patch'
    version = 'unknown'
    try:
        version = str(json.loads((target / 'package.json').read_text(
            encoding='utf-8')).get('version', 'unknown'))
        if version != EXPECTED_VERSION:
            return _error(target, version, 'Version drift; refusing to patch.')
        text = patch_file.read_text(encoding='utf-8')
        if _patch(target, text, reverse=True).returncode == 0:
            print(f'Converter already patched (v8-to-istanbul {version}).')
            return 0
        preview = _patch(target, text)
        if preview.returncode != 0:
            return _error(target, version,
                          'Forward dry run failed.\n' + preview.stdout + preview.stderr)
        applied = _patch(target, text, dry_run=False)
        if applied.returncode != 0:
            return _error(target, version,
                          'Apply failed.\n' + applied.stdout + applied.stderr)
    except (OSError, ValueError) as error:
        return _error(target, version, str(error))
    print(f'Applied converter patch (v8-to-istanbul {version}).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
