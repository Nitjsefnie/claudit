"""The response-cache invalidation / ingest_done broadcast gate of
run_ingest_locked: an ingest whose only data
change comes from the derived-state phases — the reprice pass after a
PRICING_VERSION bump, the suppression purge, or the project_aliases
fold — must gate exactly like a run that inserted, reparsed or deleted
a file.

Split from test_ingest.py's planned additions: the size gate holds
every tests/ module to its ceiling, so new tests live here and borrow
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
from tests import dims_seed

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
    cache.response_cache.put(key, {"v": "old"})
    broadcasts = []
    monkeypatch.setattr(ingest.events, "broadcast_threadsafe",
                        lambda *args, **kwargs: broadcasts.append(args))
    return broadcasts


def test_a_reprice_only_ingest_invalidates_and_broadcasts(
        fresh_db: str, mini_r2_env: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """an ingest whose only data change is the reprice pass
    must mark the response cache stale and broadcast ingest_done, exactly
    like a run that inserted, reparsed or deleted files — SV-ROLLUP: the
    dashboard must never serve the previous numbers after records
    changed."""
    assert ingest.run_ingest_locked("manual")["error"] is None
    # a restamp-only bump changes no data and must stay
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


@pytest.mark.parametrize("tool_only", [False, True],
                         ids=["records", "orphan-tool-use"])
def test_a_purge_only_ingest_invalidates_and_broadcasts(
        fresh_db: str, mini_r2_env: Path,
        monkeypatch: pytest.MonkeyPatch, tool_only: bool) -> None:
    """Suppression alone must invalidate and broadcast for either row family."""
    assert ingest.run_ingest_locked("manual")["error"] is None
    with db.viz_conn() as c:
        if tool_only:
            model = "tool-only-cache-gate-model"
            row = c.execute(
                "SELECT file_key FROM files ORDER BY file_key LIMIT 1").fetchone()
            assert row is not None, "initial ingest must create a files parent"
            line_num = 2_147_483_000
            assert _scalar(
                c, "SELECT COUNT(*) FROM records r "
                   "JOIN files f ON f.file_id = r.file_id "
                   "WHERE f.file_key = %s AND r.line_num = %s",
                (row[0], line_num)) == 0, "tool call must have no record"
            bash = dims_seed.dim_id(c, "dim_tool_name", "Bash")
            seed_model = dims_seed.dim_id(c, "dim_model", model)
            fid = dims_seed.file_id(c, row[0])
            c.execute(
                "INSERT INTO tool_uses "
                "(file_id, line_num, idx, tool_name_id, model_id) "
                "VALUES (%s, %s, 0, %s, %s)",
                (fid, line_num, bash, seed_model))
        else:
            model = "claude-sonnet%"
            assert _scalar(
                c, "SELECT COUNT(*) FROM records r "
                   "JOIN dim_model dm ON dm.id = r.model_id "
                   "WHERE dm.value ILIKE 'claude-sonnet%'") > 0, (
                "the mini mirror must carry records the pattern matches")
        c.execute(
            "INSERT INTO suppressed_models(pattern, note) "
            "VALUES (%s, 'suppression gate')", (model,))
        c.commit()
    broadcasts = _prime_and_spy("purge-only-key", monkeypatch)

    summary = ingest.run_ingest_locked("manual")

    assert summary["error"] is None
    assert (summary["inserted"], summary["reparsed"],
            summary["deleted"]) == (0, 0, 0)
    if tool_only:
        with db.viz_conn() as c:
            assert _scalar(
                c, "SELECT COUNT(*) FROM tool_uses t "
                   "JOIN files f ON f.file_id = t.file_id "
                   "WHERE f.file_key = %s AND t.line_num = %s",
                (row[0], line_num)) == 0, "matching tool_use must be purged"
    assert broadcasts, "a purge-only run must broadcast ingest_done"
    assert cache.response_cache.get_entry("purge-only-key") == (
        {"v": "old"}, True), "a purge-only run must mark responses stale"


@pytest.mark.parametrize("relabel_only", [False, True],
                         ids=["move-files", "reset-label"])
def test_an_alias_fold_only_ingest_invalidates_and_broadcasts(
        fresh_db: str, mini_r2_env: Path,
        monkeypatch: pytest.MonkeyPatch, relabel_only: bool) -> None:
    """Both alias changes must reach the cache and broadcast gate."""
    assert ingest.run_ingest_locked("manual")["error"] is None
    with db.viz_conn() as c:
        if relabel_only:
            c.execute("UPDATE projects SET display_name = 'Drifted label' "
                      "WHERE project_id = 'projA'")
            pattern, target = "relabel-370-no-match-%", "projA"
            assert _scalar(
                c, "SELECT COUNT(*) FROM projects WHERE project_id = 'projA' "
                   "AND display_name <> project_id") == 1, (
                "the seeded target must carry a drifted display_name")
        else:
            pattern, target = "projA%", "projB"
            assert _scalar(
                c, "SELECT COUNT(*) FROM files "
                   "WHERE project_id = 'projA'") > 0, (
                "the mini mirror must carry files the alias moves")
            assert _scalar(
                c, "SELECT COUNT(*) FROM files WHERE project_id = 'projB' "
                   "AND session_id = 'sess-A'") == 0, (
                "the mirror must not already carry sess-A under projB")
        c.execute(
            "INSERT INTO project_aliases(pattern, project_id, note) "
            "VALUES (%s, %s, 'alias gate')", (pattern, target))
        c.commit()
    broadcasts = _prime_and_spy("alias-fold-only-key", monkeypatch)

    summary = ingest.run_ingest_locked("manual")

    assert summary["error"] is None
    assert (summary["inserted"], summary["reparsed"],
            summary["deleted"]) == (0, 0, 0)
    assert broadcasts, "an alias-only run must broadcast ingest_done"
    assert cache.response_cache.get_entry("alias-fold-only-key") == (
        {"v": "old"}, True), "an alias-only run must mark responses stale"
    with db.viz_conn() as c:
        if relabel_only:
            assert _scalar(
                c, "SELECT display_name FROM projects "
                   "WHERE project_id = 'projA'") == "projA", (
                "the fold relabels its target with the target id")
        else:
            assert _scalar(
                c, "SELECT COUNT(*) FROM files WHERE project_id = 'projB' "
                   "AND session_id = 'sess-A'") > 0, (
                "the fold must move sess-A's files to projB")
            assert _scalar(
                c, "SELECT COUNT(*) FROM files "
                   "WHERE project_id = 'projA'") == 0, (
                "the fold must empty the source project projA")


def test_a_no_op_ingest_stays_quiet(
        fresh_db: str, mini_r2_env: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """ flip side: a run that changes nothing stays quiet.
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
# identity changes that bypass the walk-level counts must
# reach the same gate. A lane re-key moves files' project id without
# inserting, reparsing or deleting anything, so cached dashboards and the
# project list must be invalidated.

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
    monkeypatch.setenv("CLAUDIT_R2_ENDPOINT", f"file://{tmp_path}/r2/")

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
