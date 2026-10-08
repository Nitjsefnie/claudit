"""The response-cache invalidation / ingest_done broadcast gate of
run_ingest_locked (issues #256 and #341): an ingest whose only data
change comes from the derived-state phases — the reprice pass after a
PRICING_VERSION bump, the suppression purge, or the project_aliases
fold — must gate exactly like a run that inserted, reparsed or deleted
a file.

Split from test_ingest.py's planned additions: that module sits at its
committed size-baseline entry (957), so new tests live here and borrow
its fixtures, the way test_ingest_fetch.py already does.
"""
from __future__ import annotations

import json
import lzma

from datetime import datetime, timezone

from pathlib import Path

import pytest

# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _FIX_ROOT, _fresh_db_fixture, _mini_r2_env_fixture, _scalar,
)

from backend import (cache, constants, db, ingest, pricing,
                     rate_fingerprint)

# Synthetic rates, deliberately unlike any real price (SV-TEST-DATA):
# the simulated pricing bump the reprice-gate tests apply before their
# second ingest. Mutating BOTH tables replaces the whole row whatever
# the tree's data, so the move survives the perturbed-data CI leg.
_BUMP_RATES = {"fresh": 9.0, "create_5m": 9.5, "create_1h": 9.75,
               "read": 0.9, "output": 19.0}
UTC = timezone.utc


def _prime_and_spy(
        key: str, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[object, ...]]:
    """Prime one response-cache entry and record every ingest_done
    broadcast; return the recorded list."""
    cache.response_cache.put(key, {"v": "old"})
    broadcasts = []
    monkeypatch.setattr(ingest.events, "broadcast_threadsafe",
                        lambda *args, **kwargs: broadcasts.append(args))
    return broadcasts


def test_a_reprice_only_ingest_invalidates_and_broadcasts(
        fresh_db: str, mini_r2_env: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #256: an ingest whose only data change is the reprice pass
    must mark the response cache stale and broadcast ingest_done, exactly
    like a run that inserted, reparsed or deleted files — SV-ROLLUP: the
    dashboard must never serve the previous numbers after records
    changed."""
    assert ingest.run_ingest_locked("manual")["error"] is None
    # Issue #339: a restamp-only bump changes no data and must stay
    # quiet; the simulated bump moves real rates so the reprice pass has
    # something to reprice.
    # claude-opus-4-7 is a tracked vendor key since the migration: the
    # pair its bare path reads is (key, vendor_host), and replacing the
    # WHOLE row in the provider tables is what makes the move
    # perturbation-proof (the perturbed tree appends dated entries to
    # every row).
    opus_row = ("claude-opus-4-7", pricing.VENDOR_HOSTS["claude-opus-4-7"])
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {
        **pricing.PROVIDER_RATES,
        opus_row: _BUMP_RATES,
    })
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {
        **pricing.PROVIDER_DATED_RATES,
        opus_row: [(datetime(2099, 1, 1, tzinfo=UTC), _BUMP_RATES)],
    })
    # The fingerprints the first ingest stamped came from the
    # pre-mutation tables (rate_fingerprint memoizes per pair, and the
    # production tables are immutable per process — the established
    # in-test pattern is an explicit clear at the mutation site, as in
    # the parity test).
    rate_fingerprint.clear_fingerprint_cache()
    monkeypatch.setattr(constants, "PRICING_VERSION",
                        str(int(constants.PRICING_VERSION) + 1))
    broadcasts = _prime_and_spy("reprice-only-key", monkeypatch)

    summary = ingest.run_ingest_locked("manual")

    assert summary["error"] is None
    assert (summary["inserted"], summary["reparsed"],
            summary["deleted"]) == (0, 0, 0), (
        "the run must have changed records ONLY through the reprice pass")
    assert broadcasts, "a reprice-only run must broadcast ingest_done"
    assert cache.response_cache.get_entry("reprice-only-key") == (
        {"v": "old"}, True), "a reprice-only run must mark responses stale"


def test_a_purge_only_ingest_invalidates_and_broadcasts(
        fresh_db: str, mini_r2_env: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #256: an ingest whose only data change is the suppression
    purge must mark the response cache stale and broadcast ingest_done —
    the purge deletes records without inserting, reparsing or deleting a
    file, so the walk-level counts all stay zero."""
    assert ingest.run_ingest_locked("manual")["error"] is None
    with db.viz_conn() as c:
        c.execute(
            "INSERT INTO suppressed_models(pattern, note) "
            "VALUES ('claude-sonnet%', 'issue 256')")
        c.commit()
        assert _scalar(
            c, "SELECT COUNT(*) FROM records "
               "WHERE model ILIKE 'claude-sonnet%'") > 0, (
            "the mini mirror must carry records the pattern matches")
    broadcasts = _prime_and_spy("purge-only-key", monkeypatch)

    summary = ingest.run_ingest_locked("manual")

    assert summary["error"] is None
    assert (summary["inserted"], summary["reparsed"],
            summary["deleted"]) == (0, 0, 0), (
        "orphan deletion is separate from the purge's own deletion — the "
        "purge removing records is the whole point")
    assert broadcasts, "a purge-only run must broadcast ingest_done"
    assert cache.response_cache.get_entry("purge-only-key") == (
        {"v": "old"}, True), "a purge-only run must mark responses stale"


def test_a_tool_use_only_purge_invalidates_and_broadcasts(
        fresh_db: str, mini_r2_env: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A suppressed tool call without a matching record still changes
    stored data and must invalidate cached responses and broadcast."""
    assert ingest.run_ingest_locked("manual")["error"] is None
    model = "tool-only-cache-gate-model"
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT file_key FROM files ORDER BY file_key LIMIT 1").fetchone()
        assert row is not None, "the initial ingest must create a files parent"
        line_num = 2_147_483_000
        assert _scalar(
            c, "SELECT COUNT(*) FROM records "
               "WHERE file_key = %s AND line_num = %s", (row[0], line_num)
        ) == 0, "the seeded tool call must have no record on its line"
        c.execute(
            "INSERT INTO tool_uses "
            "(file_key, line_num, idx, tool_name, model) "
            "VALUES (%s, %s, 0, 'Bash', %s)", (row[0], line_num, model))
        c.execute(
            "INSERT INTO suppressed_models(pattern, note) "
            "VALUES (%s, 'issue 256 tool-use-only')", (model,))
        c.commit()
    broadcasts = _prime_and_spy("tool-use-purge-only-key", monkeypatch)

    summary = ingest.run_ingest_locked("manual")

    assert summary["error"] is None
    assert (summary["inserted"], summary["reparsed"],
            summary["deleted"]) == (0, 0, 0)
    with db.viz_conn() as c:
        assert _scalar(
            c, "SELECT COUNT(*) FROM tool_uses "
               "WHERE file_key = %s AND line_num = %s", (row[0], line_num)
        ) == 0, "the matching tool_use row must be purged"
    cache_entry = cache.response_cache.get_entry("tool-use-purge-only-key")
    assert broadcasts and cache_entry == ({"v": "old"}, True), (
        "a tool_use-only purge must broadcast and mark responses stale; "
        f"got broadcasts={broadcasts!r}, cache_entry={cache_entry!r}")


def test_an_alias_fold_only_ingest_invalidates_and_broadcasts(
        fresh_db: str, mini_r2_env: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #341: an ingest whose only data change is the project_aliases
    fold must mark the response cache stale and broadcast ingest_done —
    the fold re-keys stored identity without inserting, reparsing or
    deleting a file, so the walk-level counts all stay zero."""
    assert ingest.run_ingest_locked("manual")["error"] is None
    with db.viz_conn() as c:
        c.execute(
            "INSERT INTO project_aliases(pattern, project_id, note) "
            "VALUES ('projA%', 'projB', 'issue 341')")
        c.commit()
        assert _scalar(
            c, "SELECT COUNT(*) FROM files WHERE project_id = 'projA'") > 0, (
            "the mini mirror must carry files the alias moves")
        assert _scalar(
            c, "SELECT COUNT(*) FROM files WHERE project_id = 'projB' "
               "AND session_id = 'sess-A'") == 0, (
            "the mirror must not already carry sess-A under projB — "
            "the fold is what puts it there")
    broadcasts = _prime_and_spy("alias-fold-only-key", monkeypatch)

    summary = ingest.run_ingest_locked("manual")

    assert summary["error"] is None
    assert (summary["inserted"], summary["reparsed"],
            summary["deleted"]) == (0, 0, 0), (
        "the run must have changed data ONLY through the alias fold")
    assert broadcasts, "an alias-fold-only run must broadcast ingest_done"
    assert cache.response_cache.get_entry("alias-fold-only-key") == (
        {"v": "old"}, True), "an alias-fold-only run must mark responses stale"
    with db.viz_conn() as c:
        assert _scalar(
            c, "SELECT COUNT(*) FROM files WHERE project_id = 'projB' "
               "AND session_id = 'sess-A'") > 0, (
            "the fold must move sess-A's files TO the alias target projB")
        assert _scalar(
            c, "SELECT COUNT(*) FROM files WHERE project_id = 'projA'") == 0, (
            "the fold must empty the source project projA")


def test_a_no_op_ingest_stays_quiet(
        fresh_db: str, mini_r2_env: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #256's flip side: a run that changes nothing stays quiet.
    The rollup rebuilds rewrite their whole derived table every run, so
    their row counts must NOT gate the invalidation — only the five
    record-mutating phases do (suppression, reprice, the alias fold,
    canonical flags, teammate resolution)."""
    assert ingest.run_ingest_locked("manual")["error"] is None
    broadcasts = _prime_and_spy("no-op-key", monkeypatch)

    summary = ingest.run_ingest_locked("manual")

    assert summary["error"] is None
    assert (summary["inserted"], summary["reparsed"],
            summary["deleted"]) == (0, 0, 0)
    assert not broadcasts, "a no-op run must not broadcast ingest_done"
    assert cache.response_cache.get_entry("no-op-key") == (
        {"v": "old"}, False), "a no-op run must leave responses fresh"


# --------------------------------------------------------------------------
# Issue #370: identity changes that bypass the walk-level counts must
# reach the same gate. A lane re-key moves files' project id without
# inserting, reparsing or deleting anything; the fold pass's label reset
# changes what /api/projects displays. Either alone must invalidate and
# broadcast, or the cached project list and dashboards keep the old
# project for up to an hour.

def test_a_lane_marker_rekey_only_ingest_invalidates_and_broadcasts(
        fresh_db, tmp_path, monkeypatch) -> None:
    """A lane project's marker landing for a previously hash-keyed
    project re-keys stored rows onto the slug with every walk-level
    count at zero — yet identity changed, so the gate must fire."""
    proj = "8805b8ac99ad"
    slug = "-home-me-lanework"
    bucket = tmp_path / "r2" / "claude"
    lane = bucket / "sessions" / proj / "01a0-uuid"
    lane.mkdir(parents=True)
    (lane / "wire.jsonl.xz").write_bytes(
        lzma.compress((_FIX_ROOT / "parser" / "codex_min.jsonl").read_bytes()))
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")

    # Run 1: no marker — the lane project keys by its bare hash.
    assert ingest.run_ingest_locked("manual")["error"] is None
    with db.viz_conn() as c:
        assert _scalar(
            c, "SELECT COUNT(*) FROM files WHERE project_id = %s",
            (proj,)) > 0
    broadcasts = _prime_and_spy("lane-rekey-key", monkeypatch)

    # The marker lands; NO etag changes, so nothing reparses.
    (bucket / "sessions" / proj / "project.json").write_text(
        json.dumps({"path": "/home/me/lanework"}))
    summary = ingest.run_ingest_locked("manual")

    assert summary["error"] is None
    assert (summary["inserted"], summary["reparsed"],
            summary["deleted"]) == (0, 0, 0), (
        "the run must have changed identity ONLY through the lane re-key")
    assert broadcasts, "a lane-re-key-only run must broadcast ingest_done"
    assert cache.response_cache.get_entry("lane-rekey-key") == (
        {"v": "old"}, True), "a lane-re-key-only run must mark responses stale"
    with db.viz_conn() as c:
        assert _scalar(
            c, "SELECT COUNT(*) FROM files WHERE project_id = %s",
            (slug,)) > 0, "the marker slug now holds the files"
        assert _scalar(
            c, "SELECT COUNT(*) FROM files WHERE project_id = %s",
            (proj,)) == 0, "the hash holds nothing after the re-key"


def test_a_relabel_only_alias_pass_invalidates_and_broadcasts(
        fresh_db: str, mini_r2_env: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The fold pass relabels every alias target with its own id on every
    run; a pass whose ONLY change is that relabel changes what
    /api/projects displays and must gate exactly like a fold that moves
    files."""
    assert ingest.run_ingest_locked("manual")["error"] is None
    with db.viz_conn() as c:
        c.execute("UPDATE projects SET display_name = 'Drifted label' "
                  "WHERE project_id = 'projA'")
        c.execute(
            "INSERT INTO project_aliases(pattern, project_id, note) "
            "VALUES ('relabel-370-no-match-%', 'projA', 'issue 370')")
        c.commit()
        assert _scalar(
            c, "SELECT COUNT(*) FROM projects WHERE project_id = 'projA' "
               "AND display_name <> project_id") == 1, (
            "the seeded target must carry a drifted display_name")
    broadcasts = _prime_and_spy("relabel-only-key", monkeypatch)

    summary = ingest.run_ingest_locked("manual")

    assert summary["error"] is None
    assert (summary["inserted"], summary["reparsed"],
            summary["deleted"]) == (0, 0, 0), (
        "the run must have changed data ONLY through the label reset")
    assert broadcasts, "a relabel-only alias pass must broadcast ingest_done"
    assert cache.response_cache.get_entry("relabel-only-key") == (
        {"v": "old"}, True), "a relabel-only pass must mark responses stale"
    with db.viz_conn() as c:
        assert _scalar(
            c, "SELECT display_name FROM projects "
               "WHERE project_id = 'projA'") == "projA", (
            "the fold relabels its target with the target id")
