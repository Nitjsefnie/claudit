"""Portable pytest cells use a socket directory for database housekeeping."""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
import yaml

from tests import scratch_db


def test_portable_pytest_uses_step_scoped_socket_directory():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/tests.yml"
    job = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)[
        "jobs"]["pytest-portable"]
    step, = (step for step in job["steps"] if step.get("name") == "Run tests")
    assert step.get("env") == {"PGHOST": "${{ runner.temp }}"}
    assert "PGHOST" not in (job.get("env") or {})


def test_empty_pghost_is_not_a_timeout(tmp_path_factory, monkeypatch):
    socket_dir = tmp_path_factory.mktemp("s")  # short: libpq caps socket paths
    monkeypatch.setenv("PGHOST", str(socket_dir))
    with pytest.raises(psycopg.OperationalError) as caught:
        scratch_db.admin_connection()
    assert not isinstance(caught.value, psycopg.errors.ConnectionTimeout)
    message = str(caught.value)
    assert f"{socket_dir}/.s.PGSQL." in message
    assert "too long" not in message
