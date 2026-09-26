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

import json
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


def _seed_parents(c, file_key: str) -> None:
    """The project+file rows every seeded record needs (idempotent)."""
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


def _seed(c, file_key: str, line_num: int, *, pricing_version=None,
          cost_usd: float = 0.5, model: str = _SEED_MODEL, ts=_SEED_TS,
          provider: str | None = None) -> None:
    """One record row under seeded project+file parents, with a fixed
    token tally (_SEED_TOKENS) a test prices through compute_cost.

    cost_usd is a sentinel far from any computed value, so "untouched"
    is observable.
    """
    _seed_parents(c, file_key)
    fresh, create, read, output, eph5, eph1h = _SEED_TOKENS
    c.execute(
        "INSERT INTO records (file_key, line_num, ts, model, fresh_tokens, "
        "cache_creation_tokens, cache_read_tokens, output_tokens, "
        "eph5_tokens, eph1h_tokens, cost_usd, provider, pricing_version) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (file_key, line_num, ts, model, fresh, create, read,
         output, eph5, eph1h, cost_usd, provider, pricing_version),
    )


def _seed_meter_row(c, line_num: int, *, model: str, fresh_tokens: int,
                    provider: str | None = None,
                    long_context: bool | None = None) -> None:
    """One stale record row whose tally the test chooses, inserted
    DIRECTLY: the fixed-tally _seed can carry no meter-sized tally and
    names no long_context. The INSERT names the long_context column, so
    a seeded flag is really in the row before the pass runs."""
    _seed_parents(c, _FILE_KEY)
    c.execute(
        "INSERT INTO records (file_key, line_num, ts, model, fresh_tokens, "
        "cache_creation_tokens, cache_read_tokens, output_tokens, "
        "eph5_tokens, eph1h_tokens, cost_usd, provider, long_context, "
        "pricing_version) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (_FILE_KEY, line_num, _SEED_TS, model, fresh_tokens,
         0, 0, 0, 0, 0, 0.5, provider, long_context, None),
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
    advance past a skip would spin forever. The skip sits at the batch
    boundary (line 4, the last row of the second batch of two) on
    purpose."""
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


def test_reprice_aborts_between_batches_when_shutdown_requested(
        fresh_db, monkeypatch):
    """A shutdown request between batches unwinds the pass via
    IngestAborted: every batch already committed persists, the batch the
    abort interrupts never opens, and the next run converges — the
    ingest stop contract (issue #103) at the reprice pass's own bounded
    steps. should_stop returning True (or raising) is the signal."""
    monkeypatch.setattr(ingest_reprice, "REPRICE_BATCH", 2)
    with db.viz_conn() as c:
        for line_num in range(1, 8):
            _seed(c, _FILE_KEY, line_num)
        c.commit()

    checks = iter([False, True])
    with pytest.raises(ingest.IngestAborted):
        ingest_reprice.reprice_stale(should_stop=lambda: next(checks))

    with db.viz_conn() as c:
        rows = _rows(c)
    versions = [row[2] for row in rows]
    assert versions[:2] == [constants.PRICING_VERSION] * 2, (
        "the first batch was committed before the abort, so it persists")
    assert versions[2:] == [None] * 5, (
        "rows the aborted pass never reached stay stale (NULL version)")


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


def test_reprice_rederives_long_context_for_the_meters_models(fresh_db):
    """Issue #194: for the meter's models the long-context flag is a pure
    function of stored columns — fresh + cache_creation + cache_read
    against the threshold — so the pass re-derives it beside the cost. A
    stale gpt-5.6-sol row whose stored tally (280k fresh) sits above the
    272k threshold flips to TRUE with its metered cost; a claude row
    seeded alongside — a model whose card carries no meter — keeps its
    stored NULL and prices flat above the same threshold."""
    metered = round(pricing.compute_cost(
        "gpt-5.6-sol", fresh=280_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=_SEED_TS, long_context=True), 6)
    flat = round(pricing.compute_cost(
        _SEED_MODEL, fresh=280_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=_SEED_TS, long_context=False), 6)
    with db.viz_conn() as c:
        _seed_meter_row(c, 1, model="gpt-5.6-sol", fresh_tokens=280_000)
        _seed_meter_row(c, 2, model=_SEED_MODEL, fresh_tokens=280_000)
        c.commit()

    assert ingest_reprice.reprice_stale() == 2

    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT line_num, long_context, cost_usd, pricing_version "
            "FROM records WHERE file_key = %s ORDER BY line_num",
            (_FILE_KEY,)).fetchall()
    codex, claude = rows
    assert codex[0] == 1 and claude[0] == 2
    assert codex[1] is True
    assert float(codex[2]) == metered
    assert codex[3] == constants.PRICING_VERSION
    assert claude[1] is None, "a no-meter model's stored flag is untouched"
    assert float(claude[2]) == flat


def test_reprice_keeps_a_provider_rows_stored_flag(fresh_db):
    """A row naming a provider host prices by that host's card, not the
    meter model's, so its stored long_context is left exactly as parse
    stored it: FALSE stays FALSE even though the model is one of the
    meter's and the tally sits above the threshold."""
    with db.viz_conn() as c:
        _seed_meter_row(c, 1, model="gpt-5.6-sol", fresh_tokens=280_000,
                        provider="OpenRouter", long_context=False)
        c.commit()

    assert ingest_reprice.reprice_stale() == 1

    with db.viz_conn() as c:
        row = c.execute(
            "SELECT long_context, cost_usd, pricing_version FROM records "
            "WHERE file_key = %s AND line_num = 1", (_FILE_KEY,)).fetchone()
    assert row is not None, "the seeded provider row must exist"
    flag, cost, version = row
    assert flag is False
    assert float(cost) == round(pricing.compute_cost(
        "gpt-5.6-sol", fresh=280_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=_SEED_TS, long_context=False,
        provider="OpenRouter"), 6)
    assert version == constants.PRICING_VERSION


def test_reprice_phase_runs_between_suppression_and_canonical(monkeypatch):
    """The phases tuple resolves its names through ingest's globals at
    call time, so stubbing every phase on `ingest` records the order the
    rebuild runs them in. Reprice is a records mutation, so it must land
    after suppression and before the canonical pass and every rollup."""
    order = []

    def stub(name):
        # The reprice phase is invoked through a partial that forwards
        # should_stop, so every stub accepts (and ignores) arguments.
        return lambda *args, **kwargs: order.append(name)

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


def _lane_blob(session_id: str, *, plan_type: str | None) -> bytes:
    """One codex rollout: a gpt-5.6-sol request at 300k fresh / 1k output,
    above the REAL 272k threshold, so the reprice's re-derivation must
    agree with the reparse in both plan shapes — under the test's
    threshold=1 patch and without it alike."""
    usage = {
        "input_tokens": 300_000, "cached_input_tokens": 0,
        "cache_write_input_tokens": 0, "output_tokens": 1_000,
        "reasoning_output_tokens": 0, "total_tokens": 301_000,
    }
    lines = [
        {"timestamp": "2026-09-01T12:00:00.000Z", "type": "session_meta",
         "payload": {"session_id": session_id}},
        {"timestamp": "2026-09-01T12:00:01.000Z", "type": "turn_context",
         "payload": {"model": "gpt-5.6-sol"}},
        {"timestamp": "2026-09-01T12:00:02.000Z", "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"total_token_usage": usage,
                              "last_token_usage": usage},
                     # plan_type rides rate_limits on every token_count of
                     # a subscription rollout; the API shape names none.
                     **({"rate_limits": {"plan_type": plan_type}}
                        if plan_type else {})}},
    ]
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


def _proof_mirror(tmp_path) -> tuple[Path, dict[str, bytes]]:
    """The mini mirror plus three codex lane files, copied into
    tmp_path; returns (bucket, {stored file_key: plain blob}) for the
    lane files (whose stored keys keep the .xz suffix the tree omits)."""
    bucket = tmp_path / "r2" / "claude"
    shutil.copytree(_REPO_ROOT / "fixtures/r2_mini/claude", bucket)
    codex_blob = (_FIX_ROOT / "parser" / "codex_min.jsonl").read_bytes()
    lane_blobs = {
        "sessions/8805b8ac99ad/01a0-uuid/wire.jsonl.xz": codex_blob,
        # The same tally twice, once per plan shape: "pro" on every
        # token_count (the subscription rollout) and no rate_limits at
        # all (the API shape).
        "sessions/8805b8ac99ad/01b1-sub/wire.jsonl.xz": _lane_blob(
            "00000000-0000-4000-8000-0000000000b1", plan_type="pro"),
        "sessions/8805b8ac99ad/01c2-api/wire.jsonl.xz": _lane_blob(
            "00000000-0000-4000-8000-0000000000c2", plan_type=None),
    }
    for rel, blob in lane_blobs.items():
        path = bucket / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(lzma.compress(blob))
    return bucket, {f"claude/{rel}": blob for rel, blob in lane_blobs.items()}


def _proof_blobs(bucket: Path, lane_blobs: dict[str, bytes]) -> dict:
    """{file_key: plain blob bytes} for every mirrored transcript."""
    blobs = {f"claude/{p.relative_to(bucket).as_posix()}": p.read_bytes()
             for p in sorted(bucket.rglob("*.jsonl"))}
    # The stored keys keep the .xz suffix; only the bytes r2 fetches
    # are decompressed.
    blobs.update(lane_blobs)
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
    mirror plus codex lane files, MUTATE the loaded rate tables, bump
    PRICING_VERSION, run the reprice — then every stored cost_usd must
    equal what parse_file yields for the same fixture bytes under the
    same mutated tables.

    Shapes exercised here: exact-key rows (claude-opus-4-7,
    gpt-6-astra), a dated window (claude-sonnet-4-5, end 2099 so it
    covers every fixture timestamp) and the long-context meter: three
    codex lane files — the small fixture plus two 300k-fresh rollouts,
    one per plan shape ("pro" on every token_count, and no rate_limits)
    — whose flag parity now includes the reprice's re-derivation (issue
    #194). The threshold patch to 1 stays only so the small fixture
    parses TRUE; the two meter-shaped files sit above the REAL 272k, so
    the reprice (re-derivation) and the reparse agree for both plan
    shapes. The provider-row and weekly-schedule shapes have no
    fixture-backed record; the seeded tests price them through the same
    compute_cost call the parse path makes — parity by construction,
    and still a check on the pass's column mapping.
    """
    bucket, lane_blobs = _proof_mirror(tmp_path)
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")
    # Threshold 1: the small codex fixture parses long_context=TRUE; the
    # two 300k rollouts clear the REAL threshold either way, and the
    # claude rows re-derive nothing (their models carry no meter).
    monkeypatch.setattr(pricing, "LONG_CONTEXT_THRESHOLD", 1)
    assert ingest.run_ingest(trigger="manual")["error"] is None

    with db.viz_conn() as c:
        total, long_marked = _pair(
            c, "SELECT COUNT(*), COUNT(*) FILTER (WHERE long_context) "
               "FROM records")
    assert total > 0
    assert 0 < long_marked < total, (
        "the codex records must store long_context and the claude ones "
        "must not, or the long-context shape is not exercised")

    blobs = _proof_blobs(bucket, lane_blobs)

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

    # One past the tree's own version, so the mirror ingest (which stamps
    # the tree's real PRICING_VERSION) leaves every row stale whatever
    # the tree carries.
    next_version = str(int(constants.PRICING_VERSION) + 1)
    monkeypatch.setattr(constants, "PRICING_VERSION", next_version)
    assert ingest.reprice_stale() == total

    expected = _reparse_costs(blobs)
    rows = _stored_rows()
    assert len(rows) == len(expected), (
        "every stored row must have a reparse counterpart")
    # (file_key, line_num, cost_usd, pricing_version), indexed: keeping
    # the loop's local count under pylint's gate for a test this size.
    moved = 0
    for row in rows:
        assert row[3] == next_version, "a repriced row carries the new version"
        assert (row[0], row[1]) in expected
        assert float(row[2]) == pytest.approx(
            expected[(row[0], row[1])], abs=1e-9)
        moved += float(row[2]) != before[(row[0], row[1])]
    assert moved > 0, (
        "the mutated rates must actually move costs, or the parity "
        "assertion proves nothing")

    with db.viz_conn() as c:
        assert c.execute(
            "SELECT long_context, COUNT(*) FROM records "
            "WHERE file_key LIKE 'claude/sessions/%' GROUP BY 1"
        ).fetchall() == [(True, 3)], (
            "the reprice re-derives the codex rows' flag in both plan "
            "shapes (issue #194): every codex record stores TRUE")
