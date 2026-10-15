"""Obsolete oscillation writers are no longer importable."""
from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize("module", ["price_band", "collapse_oscillating_rates"])
def test_obsolete_band_writer_cannot_be_imported(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"scripts.ci.{module}")
