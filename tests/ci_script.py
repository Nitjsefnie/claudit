"""Load a scripts/ci module by name."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

CI = Path(__file__).resolve().parents[1] / "scripts" / "ci"


def load_ci_script(name: str):
    spec = importlib.util.spec_from_file_location(name, CI / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
