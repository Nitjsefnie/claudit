"""EXPORT_PYTHON launch-failure handling for the export endpoint (issue #949).

Its own module rather than a addition to tests/test_export_cancel.py: that
file sits at the size ratchet's test ceiling.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from backend import api_export


def test_missing_export_python_answers_handled_503(tmp_path, monkeypatch):
    """A nonexistent EXPORT_PYTHON is a HANDLED 503 naming the knob, not an
    unhandled FileNotFoundError escaping as a 500 (issue #949): the spawn
    sits outside _render_export's guarded region, so nothing classified its
    OSError before the fix."""
    missing = str(tmp_path / "no-such-interpreter")
    monkeypatch.setattr(api_export, "_EXPORT_PYTHON", missing)

    async def run() -> None:
        with pytest.raises(HTTPException) as excinfo:
            await api_export.export_png(rng="7d", project=None)
        detail = str(excinfo.value.detail)
        assert excinfo.value.status_code == 503
        assert "EXPORT_PYTHON" in detail
        assert missing in detail

    asyncio.run(run())
