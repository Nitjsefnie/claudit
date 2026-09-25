"""records.pricing_version and the reprice pass (issue #193).

A rate change must rePRICE stored records — recompute cost_usd from the
stored token columns — instead of re-parsing every R2 object. Two halves
are tested here:

- persist-time stamping (backend/ingest_persist.py): every INSERT names
  the PRICING_VERSION its row was priced under, and a reparse naturally
  re-stamps it. NULL counts as stale — priced by an older build. The
  column is additive and nullable (SV-SCHEMA-AUTOAPPLY), so the next
  startup's apply_schema() adds it without disturbing anything.

- the reprice pass (backend/ingest_reprice.py, wired as a derived-state
  phase between suppression and the canonical pass): selects rows whose
  stored pricing_version differs from constants.PRICING_VERSION (NULL
  counts as stale) and recomputes their cost from STORED COLUMNS ONLY —
  no R2 access, no reparse. Its PROOF is parity: repriced rows must
  equal what a full reparse of the same bytes under the same rate
  tables yields.
"""
from __future__ import annotations

import logging
import lzma
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture,
    _mini_r2_env_fixture,
)

from backend import constants, db, ingest, ingest_reprice, parse, pricing

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIX_ROOT = _REPO_ROOT / "fixtures"

UTC = timezone.utc

# Rates deliberately unlike any real price, so an assertion against them
# can never be mistaken for a pricing fact (the same rule the conftest
# synthetic-rate fixtures follow).
_WINDOW_RATES = {"fresh": 1.0, "create_5m": 1.25, "create_1h": 2.0,
                 "read": 0.1, "output": 5.0}
_OPUS_RATES = {"fresh": 2.0, "create_5m": 2.5, "create_1h": 4.0,
               "read": 0.2, "output": 10.0}
_SCHED_RATES = {"fresh": 0.5, "create_5m": 0.625, "create_1h": 1.0,
                "read": 0.05, "output": 2.5}
_ASTRA_RATES = {"fresh": 3.0, "create_5m": 3.75, "create_1h": 6.0,
                "read": 0.3, "output": 15.0}

# One seeded record's token tally: unsplit_create = 2000 - 250 - 500,
# so the pass's unsplit arithmetic is exercised by every seeded row.
_SEED_MODEL = "claude-opus-4-7"
_SEED_TS = datetime(2026, 5, 7, 10, 0, tzinfo=UTC)
_SEED_TOKENS = (1_000, 2_000, 3_000, 100, 250, 500)
_SEED_INPUTS = {"fresh": 1_000, "output": 100, "eph5": 250, "eph1h": 500,
                "unsplit_create": 1_250, "read": 3_000}

# The file key every unit test seeds its rows under (fresh_db is
# per-test, so the name never collides across tests).
_FILE_KEY = "claude/reprice-test/sess-a/sess-a.jsonl"


def _pair(c, sql: str, params=None) -> tuple[int, int]:
    """The one row's two columns; the query must yield one (like
    test_ingest._scalar, for a two-column aggregate)."""
    row = c.execute(sql, params).fetchone()
    assert row is not None, f"expected a row: {sql[:80]}"
    return row[0], row[1]


def test_ingest_stamps_pricing_version(fresh_db, mini_r2_env):
    """run_ingest over the mini mirror stamps every record with the
    current PRICING_VERSION — the value the reprice pass compares
    against."""
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None

    with db.viz_conn() as c:
        total, stamped = _pair(
            c,
            "SELECT COUNT(*), COUNT(*) FILTER ("
            "WHERE pricing_version = %s) FROM records",
            (constants.PRICING_VERSION,),
        )
    assert total > 0, "the mirror must produce records, or this proves nothing"
    assert stamped == total, (
        f"{total - stamped} of {total} records were not stamped "
        f"with pricing_version {constants.PRICING_VERSION!r}")


def test_pricing_version_column_is_nullable_migration(fresh_db, mini_r2_env):
    """A DB already holding pre-migration-shaped records rows gains the
    column at the next schema pass: additive, NULL-allowing, and the
    existing rows keep their place — reading NULL, the shape the reprice
    pass treats as stale."""
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None

    # Put the DB into the pre-migration shape: the column the new schema
    # would create is absent while data is already in `records`.
    with db.viz_conn() as c:
        c.execute(
            "ALTER TABLE records DROP COLUMN IF EXISTS pricing_version")
        c.commit()

    with db.viz_conn() as c:
        before = c.execute("SELECT COUNT(*) FROM records").fetchone()
    assert before is not None and before[0] > 0, (
        "the mirror must produce records, or this proves nothing")

    db.apply_schema()

    with db.viz_conn() as c:
        row = c.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'records' "
            "AND column_name = 'pricing_version'").fetchone()
        assert row is not None and row[0] == "YES", (
            "schema.sql must add pricing_version and admit NULL")
        after, nulls = _pair(
            c,
            "SELECT COUNT(*), COUNT(*) FILTER ("
            "WHERE pricing_version IS NULL) FROM records")
    assert after == before[0], "the migration must not disturb existing rows"
    assert nulls == after, (
        "pre-migration rows must read NULL, not be backfilled")


def _seed(c, file_key: str, line_num: int, *, pricing_version=None,
          cost_usd: float = 0.5, model: str = _SEED_MODEL, ts=_SEED_TS,
          provider: str | None = None) -> None:
    """One record row under seeded project+file parents, with a fixed
    token tally (_SEED_TOKENS) a test prices through compute_cost.

    cost_usd is a sentinel far from any computed value, so "untouched"
    is observable.
    """
    c.execute(
        "INSERT INTO projects (project_id, display_name, first_seen_at, "
        "last_seen_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (project_id) DO NOTHING",
        ("reprice-test", "reprice-test", _SEED_TS, _SEED_TS),
    )
    c.execute(
        "INSERT INTO files (file_key, project_id, session_id, is_main, "
        "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
        "parser_version) VALUES (%s, %s, %s, TRUE, %s, %s, %s, %s, %s) "
        "ON CONFLICT (file_key) DO NOTHING",
        (file_key, "reprice-test", "sess-seed", "seed-etag", 12,
         _SEED_TS, _SEED_TS, constants.PARSER_VERSION),
    )
    fresh, create, read, output, eph5, eph1h = _SEED_TOKENS
    c.execute(
        "INSERT INTO records (file_key, line_num, ts, model, fresh_tokens, "
        "cache_creation_tokens, cache_read_tokens, output_tokens, "
        "eph5_tokens, eph1h_tokens, cost_usd, provider, pricing_version) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (file_key, line_num, ts, model, fresh, create, read,
         output, eph5, eph1h, cost_usd, provider, pricing_version),
    )


def _seeded_cost() -> float:
    """What compute_cost prices the seeded tally at, under the tables
    as they stand right now."""
    return round(pricing.compute_cost(
        _SEED_MODEL, **_SEED_INPUTS, ts=_SEED_TS,
        long_context=False, provider=None), 6)


def _rows(c) -> list:
    """The seeded file's record rows as (line_num, cost_usd,
    pricing_version), ordered by line_num."""
    return c.execute(
        "SELECT line_num, cost_usd, pricing_version FROM records "
        "WHERE file_key = %s ORDER BY line_num",
        (_FILE_KEY,),
    ).fetchall()


def test_reprice_selects_null_version(fresh_db):
    """NULL pricing_version is stale: the row is repriced from its own
    stored columns to exactly what compute_cost prices the tally at,
    and re-stamped with the current version."""
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1)
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        cost, version = _pair(
            c, "SELECT cost_usd, pricing_version FROM records "
               "WHERE file_key = %s AND line_num = 1", (_FILE_KEY,))
    assert float(cost) == _seeded_cost()
    assert version == constants.PRICING_VERSION


def test_reprice_leaves_the_current_version_alone(fresh_db):
    """A row already stamped with the current PRICING_VERSION is not
    stale: neither repriced nor re-stamped, its stored cost sentinel
    intact."""
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1)
        _seed(c, _FILE_KEY, 2, pricing_version=constants.PRICING_VERSION)
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        rows = _rows(c)
    assert float(rows[0][1]) == _seeded_cost()
    assert rows[0][2] == constants.PRICING_VERSION
    assert float(rows[1][1]) == 0.5, "the current row keeps its sentinel"
    assert rows[1][2] == constants.PRICING_VERSION


def test_reprice_skips_rows_priced_by_a_newer_version(fresh_db, caplog):
    """The rollback guard mirrors _stored_version_is_newer (issue #118):
    a stored version that parses as an int and is GREATER than the
    current one was priced by a newer build, and an older binary must
    not clobber it. The row is skipped, the keyset advances past it,
    and the skip is logged."""
    with db.viz_conn() as c:
        for line_num, version in ((1, None), (2, "0"), (3, "999")):
            _seed(c, _FILE_KEY, line_num, pricing_version=version)
        c.commit()

    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        assert ingest_reprice.reprice_stale() == 2

    with db.viz_conn() as c:
        rows = _rows(c)
    assert [row[2] for row in rows] == [
        constants.PRICING_VERSION, constants.PRICING_VERSION, "999"]
    assert float(rows[2][1]) == 0.5, "the newer row keeps its sentinel"
    assert "skipped 1 record" in caplog.text


def test_reprice_reprices_a_non_integer_version(fresh_db):
    """A stored pricing_version that does not parse as an int cannot be
    shown NEWER, so the guard's ordinary-staleness branch applies: the
    row is repriced from its stored columns and stamped with the current
    version, exactly like a NULL."""
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1, pricing_version="abc")
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        cost, version = _pair(
            c, "SELECT cost_usd, pricing_version FROM records "
               "WHERE file_key = %s AND line_num = 1", (_FILE_KEY,))
    assert float(cost) == _seeded_cost()
    assert version == constants.PRICING_VERSION


def test_reprice_is_idempotent(fresh_db):
    """Rows the first run stamped no longer differ from the current
    version, so the second run selects nothing."""
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1)
        _seed(c, _FILE_KEY, 2)
        c.commit()

    assert ingest_reprice.reprice_stale() == 2
    assert ingest_reprice.reprice_stale() == 0


def test_reprice_batches_across_the_keyset(fresh_db, monkeypatch):
    """Seven rows against a REPRICE_BATCH of 2: the keyset cursor must
    carry past updated AND guard-skipped rows — a batch that could not
    advance past a skip would spin forever. The skip sits mid-batch
    (line 4, inside the second batch of two) on purpose."""
    monkeypatch.setattr(ingest_reprice, "REPRICE_BATCH", 2)
    with db.viz_conn() as c:
        for line_num in range(1, 8):
            _seed(c, _FILE_KEY, line_num,
                  pricing_version="999" if line_num == 4 else None)
        c.commit()

    assert ingest_reprice.reprice_stale() == 6

    with db.viz_conn() as c:
        rows = _rows(c)
    assert [row[2] for row in rows] == [
        constants.PRICING_VERSION, constants.PRICING_VERSION,
        constants.PRICING_VERSION, "999", constants.PRICING_VERSION,
        constants.PRICING_VERSION, constants.PRICING_VERSION]
    assert float(rows[3][1]) == 0.5, "the skipped row keeps its sentinel"


def test_reprice_prices_a_provider_row(fresh_db,
                                       synthetic_provider_dated_rate):
    """A record naming its serving host prices from PROVIDER_RATES keyed
    (model, provider), the row's dated windows included: seeded rows on
    both sides of the synthetic row's cutover must reprice to exactly
    what compute_cost — the parse path's own pricing code — yields for
    the same stored columns. No fixture-backed record names a provider,
    so this shape is seeded and compared by construction (SV-RATE-DATA)."""
    prov = synthetic_provider_dated_rate
    before_ts = prov.start + timedelta(days=5)   # inside the dated window
    after_ts = prov.cutover + timedelta(days=5)  # at the row's list price
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1, model=prov.model, ts=before_ts,
              provider=prov.host)
        _seed(c, _FILE_KEY, 2, model=prov.model, ts=after_ts,
              provider=prov.host)
        c.commit()

    assert ingest_reprice.reprice_stale() == 2

    with db.viz_conn() as c:
        rows = _rows(c)
    assert [row[0] for row in rows] == [1, 2]
    for line_num, cost, _version in rows:
        ts = before_ts if line_num == 1 else after_ts
        assert float(cost) == round(pricing.compute_cost(
            prov.model, **_SEED_INPUTS, ts=ts, long_context=False,
            provider=prov.host), 6), f"line {line_num} repriced by host"


def test_reprice_prices_a_weekly_schedule(fresh_db,
                                          synthetic_provider_dated_rate,
                                          monkeypatch):
    """A schedule on a provider row REPLACES the row's rates inside its
    windows. The synthetic row carries one dated window, so a record
    after the cutover reads schedule entry 1; seeded with an
    always-applies window, the repriced row must cost exactly what
    compute_cost prices through the same schedule."""
    prov = synthetic_provider_dated_rate
    ts = prov.cutover + timedelta(days=5)
    # Entry index 1: exactly one dated window (the cutover) ends at or
    # before ts. (None, None, None, rates) = every day, the whole day.
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {
        (prov.model, prov.host): {1: [(None, None, None, _SCHED_RATES)]}})
    assert pricing.rate_for(prov.model, ts, prov.host) is _SCHED_RATES, (
        "the schedule must be the price in force at ts, or this test "
        "proves nothing")
    with db.viz_conn() as c:
        _seed(c, _FILE_KEY, 1, model=prov.model, ts=ts, provider=prov.host)
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        cost, _version = _pair(
            c, "SELECT cost_usd, pricing_version FROM records "
               "WHERE file_key = %s AND line_num = 1", (_FILE_KEY,))
    assert float(cost) == round(pricing.compute_cost(
        prov.model, **_SEED_INPUTS, ts=ts, long_context=False,
        provider=prov.host), 6)


def test_reprice_phase_runs_between_suppression_and_canonical(monkeypatch):
    """The phases tuple resolves its names through ingest's globals at
    call time, so stubbing every phase on `ingest` records the order the
    rebuild runs them in. Reprice is a records mutation, so it must land
    after suppression and before the canonical pass and every rollup."""
    order = []

    def stub(name):
        return lambda: order.append(name)

    phases = ("purge_suppressed", "reprice_stale", "recompute_canonical",
              "resolve_teammate_agent_types", "rebuild_rollup",
              "rebuild_tool_rollup", "rebuild_tool_error_rollup",
              "rebuild_dispatch_rollup", "rebuild_dispatch_brief_rollup",
              "rebuild_latency_rollup", "rebuild_ctx_cost_rollup",
              "rebuild_agent_rollup")
    for name in phases:
        monkeypatch.setattr(ingest, name, stub(name))
    ingest._rebuild_derived_state()  # pylint: disable=protected-access

    assert set(order) == set(phases), "every phase ran exactly once"
    assert order[:3] == [
        "purge_suppressed", "reprice_stale", "recompute_canonical"]


def _proof_mirror(tmp_path) -> tuple[Path, bytes]:
    """The mini mirror plus one codex lane file, copied into tmp_path;
    returns (bucket, codex_blob)."""
    bucket = tmp_path / "r2" / "claude"
    shutil.copytree(_REPO_ROOT / "fixtures/r2_mini/claude", bucket)
    codex_blob = (_FIX_ROOT / "parser" / "codex_min.jsonl").read_bytes()
    lane = bucket / "sessions" / "8805b8ac99ad" / "01a0-uuid"
    lane.mkdir(parents=True)
    (lane / "wire.jsonl.xz").write_bytes(lzma.compress(codex_blob))
    return bucket, codex_blob


def _proof_blobs(bucket: Path, codex_blob: bytes) -> dict:
    """{file_key: plain blob bytes} for every mirrored transcript."""
    blobs = {f"claude/{p.relative_to(bucket).as_posix()}": p.read_bytes()
             for p in sorted(bucket.rglob("*.jsonl"))}
    # The stored key keeps the .xz suffix; only the bytes r2 fetches
    # are decompressed.
    blobs["claude/sessions/8805b8ac99ad/01a0-uuid/wire.jsonl.xz"] = codex_blob
    return blobs


def _reparse_costs(blobs: dict) -> dict:
    """{(file_key, line_num): cost_usd} that a full reparse of the same
    bytes yields, under whatever the rate tables are right now."""
    expected = {}
    for file_key, blob in blobs.items():
        for rec in parse.parse_file(file_key, blob)["records"]:
            expected[(file_key, rec["line_num"])] = rec["cost_usd"]
    return expected


def _stored_costs() -> dict:
    """{(file_key, line_num): cost_usd} of every stored record."""
    with db.viz_conn() as c:
        return {(fk, ln): float(cost) for fk, ln, cost in c.execute(
            "SELECT file_key, line_num, cost_usd FROM records").fetchall()}


def _stored_rows() -> list:
    """(file_key, line_num, cost_usd, pricing_version) of every record."""
    with db.viz_conn() as c:
        return c.execute(
            "SELECT file_key, line_num, cost_usd, pricing_version "
            "FROM records").fetchall()


def test_reprice_matches_full_reparse(fresh_db, tmp_path, monkeypatch):
    """THE PROOF (issue #193): reprice == full reparse. Ingest the mini
    mirror plus a codex lane file, MUTATE the loaded rate tables, bump
    PRICING_VERSION, run the reprice — then every stored cost_usd must
    equal what parse_file yields for the same fixture bytes under the
    same mutated tables.

    Shapes exercised here: exact-key rows (claude-opus-4-7,
    gpt-6-astra), a dated window (claude-sonnet-4-5, end 2099 so it
    covers every fixture timestamp) and the long-context meter (the
    codex record, threshold patched to 1 so it parses
    long_context=TRUE — reprice reads the STORED flag, so parity holds
    through the column, not a re-derivation). The provider-row and
    weekly-schedule shapes have no fixture-backed record; the seeded
    tests price them through the same compute_cost call the parse path
    makes — parity by construction, and still a check on the pass's
    column mapping.
    """
    bucket, codex_blob = _proof_mirror(tmp_path)
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")
    # Threshold 1: the codex fixture parses long_context=TRUE, the flag
    # is stored, and the reprice reads the same stored column.
    monkeypatch.setattr(pricing, "LONG_CONTEXT_THRESHOLD", 1)

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None

    with db.viz_conn() as c:
        total, long_marked = _pair(
            c, "SELECT COUNT(*), COUNT(*) FILTER (WHERE long_context) "
               "FROM records")
    assert total > 0
    assert 0 < long_marked < total, (
        "the codex record must store long_context and the claude ones "
        "must not, or the long-context shape is not exercised")

    blobs = _proof_blobs(bucket, codex_blob)

    # Mutate the loaded tables the fixtures price under: two exact keys
    # and a dated window covering every fixture timestamp (end 2099).
    monkeypatch.setattr(pricing, "MODEL_RATES", {
        **pricing.MODEL_RATES,
        "claude-opus-4-7": _OPUS_RATES,
        "gpt-6-astra": _ASTRA_RATES,
    })
    monkeypatch.setattr(pricing, "DATED_RATES", {
        **pricing.DATED_RATES,
        "claude-sonnet-4-5": [(datetime(2099, 1, 1, tzinfo=UTC),
                               _WINDOW_RATES)],
    })
    before = _stored_costs()

    monkeypatch.setattr(constants, "PRICING_VERSION", "2")
    assert ingest.reprice_stale() == total

    expected = _reparse_costs(blobs)
    rows = _stored_rows()
    assert len(rows) == len(expected), (
        "every stored row must have a reparse counterpart")
    # (file_key, line_num, cost_usd, pricing_version), indexed: keeping
    # the loop's local count under pylint's gate for a test this size.
    moved = 0
    for row in rows:
        assert row[3] == "2", "a repriced row carries the new version"
        assert (row[0], row[1]) in expected
        assert float(row[2]) == pytest.approx(
            expected[(row[0], row[1])], abs=1e-9)
        moved += float(row[2]) != before[(row[0], row[1])]
    assert moved > 0, (
        "the mutated rates must actually move costs, or the parity "
        "assertion proves nothing")
