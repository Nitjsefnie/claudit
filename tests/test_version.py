"""The root VERSION file and its exposure through /health.

VERSION is the single source of truth for the release machinery:
`.github/workflows/release.yml` tags when it changes, and `speed.yml`
benchmarks HEAD against the release it names. Both read the file with
`cat`, so its shape matters as much as its content — a stray second line
or a `v` prefix would produce a malformed tag.
"""
from __future__ import annotations

import importlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from backend import constants


REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_PATH = REPO_ROOT / "VERSION"

# Semver with an optional prerelease suffix, no leading `v`: release.yml
# prefixes the tag itself, so a `v` here would produce `vv0.1.0`. Between
# releases the tree carries the `-dev` form (e.g. `0.4.0-dev`); a release
# is the suffix being dropped (issue #131).
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")


def test_version_file_exists():
    assert VERSION_PATH.is_file(), "VERSION is the release machinery's input"


def test_version_file_is_one_semver_line():
    raw = VERSION_PATH.read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert len(lines) == 1, f"VERSION must hold exactly one line, got {lines!r}"
    assert SEMVER.match(lines[0]), (
        f"VERSION must be semver X.Y.Z with an optional -suffix and no "
        f"leading 'v', got {lines[0]!r}"
    )


def test_version_file_ends_with_a_newline():
    # `cat VERSION` in a workflow feeds a shell var; a missing trailing
    # newline is harmless to $(cat) but makes the file annoying to edit and
    # shows up as "\ No newline at end of file" in every release diff.
    assert VERSION_PATH.read_bytes().endswith(b"\n")


def test_constants_version_matches_the_file():
    assert constants.VERSION == VERSION_PATH.read_text(encoding="utf-8").strip()


def test_version_is_not_tracked_as_ignored():
    # The deny-by-default .gitignore names files back one at a time. A
    # VERSION that git cannot see would let release.yml tag a version the
    # repo never records.
    proc = subprocess.run(
        ["git", "check-ignore", "-q", "VERSION"],
        cwd=REPO_ROOT, check=False,
    )
    assert proc.returncode != 0, ".gitignore hides VERSION from git"


def test_missing_version_file_degrades_to_unknown(monkeypatch, tmp_path):
    """A deploy without the file reports "unknown" rather than crashing.

    /health answering with an unknown version beats /health not answering.
    """
    missing = tmp_path / "backend" / "constants.py"
    missing.parent.mkdir(parents=True)
    missing.write_text("", encoding="utf-8")
    monkeypatch.setattr(constants, "__file__", str(missing))

    assert constants._read_version() == "unknown"  # pylint: disable=protected-access


def test_blank_version_file_degrades_to_unknown(monkeypatch, tmp_path):
    (tmp_path / "VERSION").write_text("   \n", encoding="utf-8")
    fake = tmp_path / "backend" / "constants.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(constants, "__file__", str(fake))

    assert constants._read_version() == "unknown"  # pylint: disable=protected-access


class _FakeCursor:
    """Enough of a psycopg cursor for /health's single ingest_runs query."""

    @staticmethod
    def fetchone():
        return None


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    @staticmethod
    def execute(*_args, **_kwargs):
        return _FakeCursor()


def test_health_error_branch_reports_version(monkeypatch):
    """`version` must survive the DB-error branch.

    "Which build is broken?" is precisely the question asked while /health
    is failing, so that is the worst possible moment for the field to be
    the one that goes missing.
    """
    app_mod = importlib.import_module("backend.app")

    def _boom():
        raise RuntimeError("no database")

    monkeypatch.setattr(app_mod.db, "viz_conn", _boom)

    response = app_mod.health()
    payload = json.loads(response.body)

    assert payload["ok"] is False
    assert payload["version"] == constants.VERSION
    assert "parser_version" in payload


def test_health_error_branch_answers_503(monkeypatch):
    """The DB-error branch must fail at the status line, not only in the
    body.

    A status-code monitor (curl -fsS, an LB probe) never parses the body:
    /health answering 200 with ok:false reports the outage as healthy to
    it. The branch answers 503 carrying the same JSON fields (issue
    #104).
    """
    app_mod = importlib.import_module("backend.app")

    def _boom():
        raise RuntimeError("no database")

    monkeypatch.setattr(app_mod.db, "viz_conn", _boom)

    response = app_mod.health()

    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["ok"] is False
    assert payload["db"] is False
    assert payload["error"] == "database unavailable"
    assert payload["version"] == constants.VERSION
    assert "parser_version" in payload
    assert "now" in payload


def test_health_ok_branch_reports_version(monkeypatch):
    app_mod = importlib.import_module("backend.app")

    monkeypatch.setattr(app_mod.db, "viz_conn", _FakeConn)
    monkeypatch.setattr(
        app_mod.ingest, "progress_snapshot", lambda: {"phase": "idle"}
    )

    response = app_mod.health()
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["version"] == constants.VERSION


class _RowCursor:
    """A cursor answering /health's ingest_runs SELECT with one row."""

    @staticmethod
    def fetchone():
        started = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        finished = datetime(2026, 9, 25, 12, 30, tzinfo=timezone.utc)
        # Column order follows /health's SELECT: id, started_at,
        # finished_at, trigger, r2_listed, reparsed, newer, error.
        return (7, started, finished, "manual", 42, 3, 1, None)


class _RowConn:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    @staticmethod
    def execute(*_args, **_kwargs):
        return _RowCursor()


def test_health_last_ingest_carries_newer(monkeypatch):
    """`last_ingest` carries the newer-version skip count beside
    `reparsed` (issue #161).

    Files skipped because a NEWER binary stored them are the visible
    signature of a rollback in progress; /health is the unauthenticated
    ops surface, and the run row is its only source.
    """
    app_mod = importlib.import_module("backend.app")

    monkeypatch.setattr(app_mod.db, "viz_conn", _RowConn)
    monkeypatch.setattr(
        app_mod.ingest, "progress_snapshot", lambda: {"phase": "idle"}
    )

    response = app_mod.health()
    payload = json.loads(response.body)

    assert response.status_code == 200
    last = payload["last_ingest"]
    assert last["id"] == 7
    assert last["reparsed"] == 3
    assert last["newer"] == 1
    assert last["error"] is None
    assert last["finished_at"] == "2026-09-25T12:30:00+00:00"
