from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from backend import api_export


def test_missing_export_python_answers_handled_503(tmp_path, monkeypatch):
    """A nonexistent CLAUDIT_EXPORT_PYTHON must yield a handled 503 naming the knob.
    Classify spawn OSError at launch: spawning precedes _render_export's guarded
    rendering region, so an unhandled FileNotFoundError would escape as a 500."""
    missing = str(tmp_path / "no-such-interpreter")
    monkeypatch.setattr(api_export, "_EXPORT_PYTHON", missing)

    async def run() -> None:
        with pytest.raises(HTTPException) as excinfo:
            await api_export.export_png(rng="7d", project=None)
        detail = str(excinfo.value.detail)
        assert excinfo.value.status_code == 503
        assert "CLAUDIT_EXPORT_PYTHON" in detail
        assert "point CLAUDIT_EXPORT_PYTHON at an interpreter" in detail
        assert missing in detail

    asyncio.run(run())
