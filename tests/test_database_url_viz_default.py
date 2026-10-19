"""Importing the app must preserve the suite's non-production DSN.
A configured deployment dotenv must also differ from the test DSN.
"""
import importlib
import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The database backend/../.env names. Hardcoded so the check still means
# something in a checkout that has no .env at all (CI).
PRODUCTION_DB = "claudit"


def _dbname(dsn: str) -> str:
    """Database name out of a postgresql:// DSN, sans query string."""
    return dsn.rsplit("/", 1)[-1].split("?", 1)[0]


def _dotenv_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


def test_conftest_default_wins_the_race_against_production_dotenv():
    """Reproduce the exact trigger a future unguarded test would hit —
    importing backend.app — and confirm CLAUDIT_DATABASE_URL_VIZ still isn't
    production afterward.

    Checks the *current* env value rather than asserting anything about
    what set it, so this holds whether conftest's setdefault won the race
    (nothing else claims the var, e.g. running this suite locally) or the
    environment already had a non-production DSN before conftest ran (e.g.
    CI setting it externally) — either is a pass; only production is a
    failure.
    """
    importlib.import_module("backend.app")  # triggers db.load_dotenv(".env")

    dsn = os.environ["CLAUDIT_DATABASE_URL_VIZ"]
    assert _dbname(dsn) != PRODUCTION_DB, (
        f"the suite is pointed at the production database: {dsn}"
    )

    configured = _dotenv_value(_REPO_ROOT / ".env", "CLAUDIT_DATABASE_URL_VIZ")
    if configured is not None:
        assert dsn != configured, (
            f"the suite is pointed at the DSN .env deploys against: {dsn}"
        )
