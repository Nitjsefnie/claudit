"""Bounded git responses for threshold-removal consumer tests."""
from __future__ import annotations

import json
import subprocess


def stub_history(monkeypatch, module, root, trailer, base):
    def read_range(argv, **kwargs):
        assert kwargs == {'capture_output': True, 'check': False, 'timeout': 10}
        if argv == ['git', '-C', str(root), 'log', '--format=%B', 'base..HEAD']:
            payload = trailer.encode()
        else:
            assert argv == ['git', '-C', str(root), 'show',
                            'base:.github/ci-thresholds.json']
            payload = json.dumps(base).encode()
        return subprocess.CompletedProcess(argv, 0, payload)
    monkeypatch.setattr(module.subprocess, 'run', read_range)
