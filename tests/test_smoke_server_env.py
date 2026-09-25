"""The smoke server environment sets no PARSER_VERSION (issue #160).

scripts/ci/smoke.py passed "PARSER_VERSION": "smoke" to the server it
boots. Nothing reads it: every consumer reads the code constant
constants.PARSER_VERSION (SV-PARSER-VERSION — the env-var override was
removed, and tests/test_ingest.py pins that it stays removed). The entry
was an inert suggestion of a switch that no longer exists. This guard
fails if the entry comes back.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load():
    """Import scripts/ci/smoke.py by path.

    scripts/ci is not a package and deliberately has no __init__.py — it
    holds standalone CI entry points, not an importable library. The
    module imports cleanly: it carries a __main__ guard and only builds
    strings at module level. (Same loader as test_smoke_cleanup /
    test_smoke_client; kept local so this file stands alone.)
    """
    path = REPO_ROOT / "scripts" / "ci" / "smoke.py"
    spec = importlib.util.spec_from_file_location("ci_smoke_server_env", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_smoke_server_env"] = module
    spec.loader.exec_module(module)
    return module


ci_smoke = _load()


def test_server_env_sets_no_parser_version(monkeypatch):
    """server_env() adds no PARSER_VERSION to the server's environment.

    monkeypatch scrubs any inherited PARSER_VERSION first, so the
    assertion is about what smoke.py itself adds, not about whatever the
    calling shell happened to export.
    """
    monkeypatch.delenv("PARSER_VERSION", raising=False)
    env = ci_smoke.server_env(0)
    assert "PARSER_VERSION" not in env
