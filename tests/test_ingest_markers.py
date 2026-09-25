"""Lane project markers are fetched only when new or changed.

Every run used to GET every sessions/<project>/project.json marker: the
resolved paths lived only in memory, so the next run had nothing to
compare a listed etag against. Each marker's key, etag and resolved path
is now stored, and a run fetches a marker only when its listing shows a
key with no row or an etag that differs from the row's.
"""
import json
import lzma
import os
from collections import Counter

import pytest
# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _FIX_ROOT, _fresh_db_fixture, _scalar,
)

from backend import db, ingest, r2

_MARKER = "project.json"


def _lane_project(bucket, proj, marker=None, sess="s-1"):
    """A lane project with one main wire; `marker` is the raw marker body
    (None plants no marker at all)."""
    lane = bucket / "sessions" / proj / sess
    lane.mkdir(parents=True)
    (lane / "wire.jsonl.xz").write_bytes(
        lzma.compress((_FIX_ROOT / "parser" / "codex_min.jsonl").read_bytes()))
    if marker is not None:
        (bucket / "sessions" / proj / _MARKER).write_text(marker)


def _path_marker(path):
    return json.dumps({"path": path})


@pytest.fixture(name="lane_tree")
def _lane_tree_fixture(tmp_path, monkeypatch):
    bucket = tmp_path / "r2" / "claude"
    _lane_project(bucket, "aaaa1111", _path_marker("/home/me/alpha"))
    _lane_project(bucket, "bbbb2222", _path_marker("/home/me/beta"))
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")
    return bucket


@pytest.fixture(name="marker_gets")
def _marker_gets_fixture(monkeypatch):
    """Counts every r2.get_object of a marker key, by key."""
    counts: Counter = Counter()
    real_get = r2.get_object

    def counting_get(key):
        if key.endswith("/" + _MARKER):
            counts[key] += 1
        return real_get(key)

    monkeypatch.setattr(r2, "get_object", counting_get)
    return counts


def _marker_rows():
    with db.viz_conn() as c:
        return dict(c.execute(
            "SELECT marker_key, path FROM lane_markers").fetchall())


def _projects():
    with db.viz_conn() as c:
        return sorted(c.execute(
            "SELECT project_id, display_name FROM projects").fetchall())


def _file_projects():
    with db.viz_conn() as c:
        return sorted(c.execute(
            "SELECT file_key, project_id FROM files").fetchall())


def _rewrite_marker(bucket, proj, body):
    """Replace a marker's body and move its mtime, so the mirror's etag
    (mtime and size) is certain to change."""
    marker = bucket / "sessions" / proj / _MARKER
    before = marker.stat().st_mtime_ns
    marker.write_text(body)
    os.utime(marker, ns=(before + 10**9, before + 10**9))


def test_second_ingest_fetches_no_unchanged_marker(
        fresh_db, lane_tree, marker_gets):
    """The #80 reproduction: the first run fetches each marker once, and a
    second run over the same listing fetches none of them."""
    first = ingest.run_ingest(trigger="manual")
    assert first["error"] is None
    assert sum(marker_gets.values()) == 2, "the first run reads each marker"

    marker_gets.clear()
    second = ingest.run_ingest(trigger="manual")
    assert second["error"] is None
    assert sum(marker_gets.values()) == 0, (
        f"unchanged markers were fetched again: {dict(marker_gets)}")


def test_cached_paths_resolve_projects_exactly_as_fetched_ones(
        fresh_db, lane_tree, marker_gets):
    """A run that reads every path from stored rows must resolve the same
    project ids and display names as the run that fetched them, and must
    hand the walk the same project_paths."""
    _lane_project(lane_tree, "cccc3333", "{not json")
    _lane_project(lane_tree, "dddd4444", json.dumps(["no", "object"]))
    _lane_project(lane_tree, "eeee5555", json.dumps({"path": ""}))
    _lane_project(lane_tree, "ffff6666")
    (lane_tree / "sessions" / "0000wire" / _MARKER).parent.mkdir()
    (lane_tree / "sessions" / "0000wire" / _MARKER).write_text(
        _path_marker("/home/me/wireless"))
    expected_paths = {
        "aaaa1111": "/home/me/alpha", "bbbb2222": "/home/me/beta",
        "0000wire": "/home/me/wireless",
    }

    _, markers = ingest._scan_objects()  # pylint: disable=protected-access
    fetched = ingest._resolve_project_paths(  # pylint: disable=protected-access
        markers, 1, [])
    assert fetched == expected_paths
    assert sum(marker_gets.values()) == 6

    marker_gets.clear()
    cached = ingest._resolve_project_paths(  # pylint: disable=protected-access
        markers, 1, [])
    assert cached == expected_paths
    assert sum(marker_gets.values()) == 0, (
        "a malformed marker is a successful read and is not re-fetched")

    ingest.run_ingest(trigger="manual")
    projects, files = _projects(), _file_projects()
    ingest.run_ingest(trigger="manual")
    assert _projects() == projects
    assert _file_projects() == files
    assert ("-home-me-alpha", "/home/me/alpha") in projects
    assert ("cccc3333", "cccc3333") in projects, (
        "a malformed marker still displays the project id")


def test_an_etag_change_refetches_that_marker_once(
        fresh_db, lane_tree, marker_gets):
    """A rewritten marker is fetched exactly once, its new path replaces
    the stored one, and the project follows it."""
    ingest.run_ingest(trigger="manual")
    _rewrite_marker(lane_tree, "aaaa1111", _path_marker("/home/me/gamma"))

    marker_gets.clear()
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert marker_gets == Counter(
        {f"claude/sessions/aaaa1111/{_MARKER}": 1})
    assert _marker_rows()[f"claude/sessions/aaaa1111/{_MARKER}"] == (
        "/home/me/gamma")
    with db.viz_conn() as c:
        pid = _scalar(c, "SELECT DISTINCT project_id FROM files "
                         "WHERE file_key LIKE '%%/aaaa1111/%%'")
        display = _scalar(c, "SELECT display_name FROM projects "
                             "WHERE project_id = %s", (pid,))
    assert (pid, display) == ("-home-me-gamma", "/home/me/gamma")

    marker_gets.clear()
    ingest.run_ingest(trigger="manual")
    assert sum(marker_gets.values()) == 0


def test_a_marker_that_left_the_listing_drops_its_row(
        fresh_db, lane_tree):
    ingest.run_ingest(trigger="manual")
    assert len(_marker_rows()) == 2

    (lane_tree / "sessions" / "bbbb2222" / _MARKER).unlink()
    ingest.run_ingest(trigger="manual")

    assert list(_marker_rows()) == [f"claude/sessions/aaaa1111/{_MARKER}"]


@pytest.mark.parametrize("exc", [
    RuntimeError("marker GET dropped"),
    ingest.VanishedObject("gone"),
], ids=["failed", "vanished"])
def test_a_failed_or_vanished_marker_is_not_cached(
        fresh_db, lane_tree, marker_gets, monkeypatch, exc):
    """A marker whose GET failed or found nothing leaves no row, supplies
    no path to that run, and is fetched again on the next one."""
    failing = f"claude/sessions/bbbb2222/{_MARKER}"
    real_fetch = ingest._fetch_with_retry  # pylint: disable=protected-access

    def fetch(key):
        if key == failing:
            raise exc
        return real_fetch(key)

    monkeypatch.setattr(ingest, "_fetch_with_retry", fetch)
    first = ingest.run_ingest(trigger="manual")
    monkeypatch.setattr(ingest, "_fetch_with_retry", real_fetch)
    assert first["failed"] == (1 if isinstance(exc, RuntimeError) else 0)
    assert failing not in _marker_rows()
    with db.viz_conn() as c:
        assert _scalar(c, "SELECT COUNT(*) FROM files "
                          "WHERE project_id = 'bbbb2222'") == 1

    marker_gets.clear()
    second = ingest.run_ingest(trigger="manual")
    assert second["error"] is None
    assert marker_gets == Counter({failing: 1})
    assert _marker_rows()[failing] == "/home/me/beta"


def test_a_failed_refetch_keeps_no_stale_path_for_that_run(
        fresh_db, lane_tree, monkeypatch):
    """A marker whose etag changed and whose re-fetch then fails supplies
    no path to that run, as before the cache, and is retried next run."""
    ingest.run_ingest(trigger="manual")
    _rewrite_marker(lane_tree, "aaaa1111", _path_marker("/home/me/gamma"))
    failing = f"claude/sessions/aaaa1111/{_MARKER}"
    real_fetch = ingest._fetch_with_retry  # pylint: disable=protected-access

    def fetch(key):
        if key == failing:
            raise RuntimeError("marker GET dropped")
        return real_fetch(key)

    monkeypatch.setattr(ingest, "_fetch_with_retry", fetch)
    _, markers = ingest._scan_objects()  # pylint: disable=protected-access
    failed: list = []
    paths = ingest._resolve_project_paths(  # pylint: disable=protected-access
        markers, 1, failed)
    assert "aaaa1111" not in paths
    assert [key for key, _ in failed] == [failing]

    monkeypatch.setattr(ingest, "_fetch_with_retry", real_fetch)
    paths = ingest._resolve_project_paths(  # pylint: disable=protected-access
        markers, 1, [])
    assert paths["aaaa1111"] == "/home/me/gamma"


def test_startup_schema_creates_the_marker_table(fresh_db):
    """A database from before the marker table gains it at startup."""
    with db.viz_conn() as c:
        c.execute("DROP TABLE lane_markers")
        c.commit()

    db.apply_schema()

    with db.viz_conn() as c:
        assert _scalar(c, "SELECT to_regclass('public.lane_markers')")
