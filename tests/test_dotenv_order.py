from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


_IMPORT_APP = r"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import psycopg
import backend.db as db

dotenv_path = sys.argv[1]
expected_dotenv_path = Path.cwd() / ".env"
load_dotenv = db.load_dotenv

def load_test_dotenv(path=".env"):
    requested_path = Path(path)
    if (
        not requested_path.is_absolute()
        or requested_path.resolve() != expected_dotenv_path.resolve()
    ):
        raise AssertionError("backend.app did not request repository-root .env")
    load_dotenv(dotenv_path)

db.load_dotenv = load_test_dotenv

def reject_database_connection(*_args, **_kwargs):
    raise AssertionError("backend.app import attempted a database connection")

with (
    patch.object(db, "ConnectionPool", side_effect=reject_database_connection),
    patch.object(psycopg, "connect", side_effect=reject_database_connection),
    patch.object(
        psycopg.Connection, "connect", side_effect=reject_database_connection
    ),
):
    import backend.app

from backend import api_common, api_export

print(json.dumps({
    "export_python": api_export._EXPORT_PYTHON,
    "timing_on": api_common.TIMING_ON,
    "database_url_viz": os.environ["CLAUDIT_DATABASE_URL_VIZ"],
}))
"""

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _import_app_settings(
    tmp_path: Path, exported: dict[str, str] | None = None
) -> dict[str, object]:
    """Import the app with a temporary dotenv file and no DB access."""
    dotenv_path = tmp_path / "quickstart.env"
    dotenv_path.write_text(
        "CLAUDIT_DATABASE_URL_VIZ=postgresql:///dotenv-database\n"
        "CLAUDIT_EXPORT_PYTHON=dotenv-python\n"
        "CLAUDIT_TIMING=1\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    for name in (
        "CLAUDIT_DATABASE_URL_VIZ",
        "CLAUDIT_DATABASE_URL_AUTH",
        "CLAUDIT_EXPORT_PYTHON",
        "CLAUDIT_TIMING",
        "CLAUDIT_R2_ENDPOINT",
        "R2_ACCOUNT_ID",
        "CLAUDIT_R2_ACCESS_KEY_ID",
        "CLAUDIT_R2_SECRET_ACCESS_KEY",
        "CLAUDIT_R2_BUCKET",
        "CLAUDIT_INGEST_WORKERS",
        "CLAUDIT_WARM_CACHE",
        "CLAUDIT_ADMIN_TOKEN",
        "CLAUDIT_COOKIE_SECURE",
        "CLAUDIT_APP_NAME",
        "CLAUDIT_APP_TITLE",
        "CLAUDIT_APP_DESCRIPTION",
    ):
        env.pop(name, None)
    env["CLAUDIT_DATABASE_URL_VIZ"] = "postgresql:///test-default"
    env["PGHOST"] = "/nonexistent/claudit-issue267-sock"
    env.update(exported or {})

    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_APP, str(dotenv_path)],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_app_import_uses_settings_from_dotenv(tmp_path: Path) -> None:
    settings = _import_app_settings(tmp_path)

    assert settings == {
        "export_python": "dotenv-python",
        "timing_on": True,
        "database_url_viz": "postgresql:///test-default",
    }


def test_exported_setting_wins_and_other_dotenv_settings_load(
    tmp_path: Path,
) -> None:
    settings = _import_app_settings(
        tmp_path, exported={"CLAUDIT_EXPORT_PYTHON": "environment-python"}
    )

    assert settings == {
        "export_python": "environment-python",
        "timing_on": True,
        "database_url_viz": "postgresql:///test-default",
    }
