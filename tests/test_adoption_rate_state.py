"""Model adoption moves price provenance atomically and respects rollbacks."""
from __future__ import annotations

import gc
import builtins
import os
import tracemalloc
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import psycopg
from psycopg_pool import ConnectionPool

from test_canonical_model_attribution import (  # pylint: disable=unused-import
    _fresh_db_fixture, _seed_two_identities,
)
from backend import (
    constants, db, dims, ingest, ingest_model_adoption, ingest_reprice,
    ingest_rollup_state, ingest_vacuum, pricing, rate_fingerprint,
)
from backend.ingest_abort import IngestAborted
from backend.ingest_scope import Scope
from tests import dims_seed


def _seed(pricing_version):
    _seed_two_identities()
    with db.viz_conn() as c:
        provider = dims_seed.dim_id(c, "dim_provider", "adoption-host")
        version = dims_seed.dim_id(c, "dim_pricing_version", pricing_version)
        c.execute(
            "UPDATE records SET fresh_tokens = 300000, output_tokens = 1000, "
            "web_search_requests = 2, provider_id = %s, "
            "ts = '2026-01-01T00:00Z' WHERE is_replay", (provider,))
        c.execute(
            "INSERT INTO records_price (file_id, line_num, cost_usd, "
            "long_context, long_context_input_mult, long_context_output_mult, "
            "pricing_version_id) SELECT file_id, line_num, 77, TRUE, 1, 1, %s "
            "FROM records", (version,))
        c.commit()


def _snapshot():
    with db.viz_conn() as c:
        return c.execute(
            "SELECT f.file_key, dm.value, r.provider_id, rp.* FROM records r "
            "JOIN files f USING (file_id) JOIN dim_model dm ON dm.id = r.model_id "
            "JOIN records_price rp USING (file_id, line_num) "
            "ORDER BY f.file_key, r.line_num").fetchall()


@pytest.mark.parametrize("spelling", ["plain", "signed-whitespace"])
def test_newer_price_defers_model_adoption(fresh_db, spelling):
    newer = str(int(constants.PRICING_VERSION) + 1)
    if spelling == "signed-whitespace":
        newer = f" +{newer} "
    _seed(newer)
    before = _snapshot()
    assert ingest_rollup_state.adopt_original_models() == 0
    assert _snapshot() == before, "keep the newer model and its entire price together"
    # The forward binary can finish the deferred adoption.
    with db.viz_conn() as c:
        current = dims.dim_id(c, "dim_pricing_version", constants.PRICING_VERSION)
        c.execute("UPDATE records_price SET pricing_version_id = %s", (current,))
        c.commit()
    assert ingest_rollup_state.adopt_original_models() == 2


@pytest.mark.parametrize("metered", [False, True])
def test_adoption_prices_own_provider_timestamp_tokens_and_meter(
        fresh_db, monkeypatch, metered):
    _seed(constants.PRICING_VERSION)
    winner = "gpt-5.6-sol"
    key = "gpt-5-6-sol"
    monkeypatch.setattr(pricing, "LONG_CONTEXT_MODELS", frozenset({key}))
    monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS", {
        key: {"threshold": 1 if metered else 400000,
              "input_mult": 3.0, "output_mult": 2.0}})
    monkeypatch.setattr(pricing, "VENDOR_BARE", {})
    pair = (key, "adoption-host")
    rates = {"fresh": 2.0, "output": 9.0, "read": 0.1,
             "create_5m": 3.0, "create_1h": 4.0, "web_search": 0.25}
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {pair: {**rates, "fresh": 99.0}})
    monkeypatch.setattr(pricing, "PROVIDER_STARTS", {})
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {})
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {
        pair: [(datetime(2027, 1, 1, tzinfo=timezone.utc), rates)]})
    ingest_vacuum.begin_run()
    scope = Scope(False, "test", 2000)
    scope.affected_uuids = {"u1"}
    before = _snapshot()
    assert ingest_rollup_state.adopt_original_models(scope) == 1
    after = _snapshot()
    assert after[1:] == before[1:], "only the scoped replay changes"
    fork = after[0]
    assert fork[0:3] == ("fork1", winner, before[0][2])
    # 300k fresh + 1k output at the dated provider price, plus two searches.
    assert fork[5:9] == (Decimal("2.318000" if metered else "1.109000"),
                         metered, 3.0 if metered else None,
                         2.0 if metered else None)
    assert ingest_vacuum.take_phase_rows() == {"reprice": 1}
    assert not scope.dirty_hours and not scope.full


def test_failed_adoption_pricing_rolls_back_model_and_file_metadata(fresh_db, monkeypatch):
    _seed(constants.PRICING_VERSION)
    before = _snapshot()
    with db.viz_conn() as c:
        files_before = c.execute("SELECT file_id, models FROM files ORDER BY file_id").fetchall()

    def fail_pricing(*args, **kwargs):
        raise RuntimeError("injected price failure")

    monkeypatch.setattr(pricing, "compute_cost", fail_pricing)
    ingest_vacuum.begin_run()
    with pytest.raises(RuntimeError, match="injected price failure"):
        ingest_rollup_state.adopt_original_models()
    assert _snapshot() == before
    with db.viz_conn() as c:
        assert c.execute("SELECT file_id, models FROM files ORDER BY file_id").fetchall() == files_before
    assert not ingest_vacuum.take_phase_rows()


def _seed_many(per_file):
    _seed(constants.PRICING_VERSION)
    with db.viz_conn() as c:
        c.execute(
            "INSERT INTO records (file_id, line_num, fallback, model_id, "
            "is_replay, is_canonical, fresh_tokens, output_tokens, "
            "web_search_requests, provider_id, ts) "
            "SELECT file_id, line_num + i * 3, fallback || '-' || i, model_id, "
            "is_replay, is_canonical, fresh_tokens, output_tokens, "
            "web_search_requests, provider_id, ts "
            "FROM records CROSS JOIN generate_series(1, %s) i", (per_file - 1,))
        c.execute(
            "INSERT INTO records_price (file_id, line_num, cost_usd, "
            "long_context, long_context_input_mult, long_context_output_mult, "
            "pricing_version_id) SELECT r.file_id, r.line_num, 77, TRUE, 1, 1, %s "
            "FROM records r LEFT JOIN records_price rp USING (file_id, line_num) "
            "WHERE rp.file_id IS NULL",
            (dims_seed.dim_id(c, "dim_pricing_version", constants.PRICING_VERSION),))
        c.commit()


@pytest.fixture(name="drop_failing_pool")
def _drop_failing_pool(fresh_db, monkeypatch):
    _seed_many(1)
    db.reset_viz_pool()
    with ConnectionPool(os.environ["CLAUDIT_DATABASE_URL_VIZ"], min_size=1, max_size=1,
                        kwargs={"autocommit": False}, check=ConnectionPool.check_connection,
                        open=True) as pool:
        monkeypatch.setattr(db, "_VIZ", pool)
        with db.viz_conn() as conn:
            conn.execute(
                "CREATE FUNCTION cancel_adoption_drop() RETURNS event_trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test DROP cancellation' "
                "USING ERRCODE='57014'; END $$")
            conn.execute(
                "CREATE EVENT TRIGGER cancel_adoption_drop ON ddl_command_start "
                "WHEN TAG IN ('DROP TABLE') EXECUTE FUNCTION cancel_adoption_drop()")
            pid = conn.info.backend_pid
            conn.commit()
        yield pid


@pytest.mark.parametrize("outcome", ["success", "abort", "pricing"])
def test_failed_staging_drop_does_not_poison_next_pool_checkout(
        drop_failing_pool, monkeypatch, outcome):
    monkeypatch.setattr(ingest_reprice, "REPRICE_BATCH", 1)
    original_error = IngestAborted("test shutdown") if outcome == "abort" else RuntimeError("test pricing")
    compute_cost = pricing.compute_cost

    def fail_pricing(*args, **kwargs):
        raise original_error

    if outcome == "pricing":
        monkeypatch.setattr(pricing, "compute_cost", fail_pricing)
    checks = []

    def stop():
        checks.append(1)
        if outcome == "abort" and len(checks) == 2:
            raise original_error
        return False

    with pytest.raises((psycopg.errors.QueryCanceled, IngestAborted, RuntimeError)) as error:
        ingest_rollup_state.adopt_original_models(should_stop=stop)
    with db.viz_conn() as conn:
        row = conn.execute("SELECT to_regclass('pg_temp.model_adoption_candidates')").fetchone()
        assert row is not None
        relation = row[0]
        print({"outcome": outcome, "pid": drop_failing_pool, "next_pid": conn.info.backend_pid,
               "relation": relation, "error": type(error.value).__name__})
        conn.execute("DROP EVENT TRIGGER cancel_adoption_drop")
        conn.commit()
    assert relation is None, "failed DROP left staging on the next physical pool checkout"
    if outcome == "success":
        assert isinstance(error.value, psycopg.errors.QueryCanceled)
        assert "test DROP cancellation" in str(error.value)
    else:
        assert error.value is original_error, "cleanup must preserve the original exception"
    adopted = sum(row[4] is not None for row in _replay_state())
    assert adopted == {"success": 2, "abort": 1, "pricing": 0}[outcome]
    monkeypatch.setattr(pricing, "compute_cost", compute_cost)
    assert ingest_rollup_state.adopt_original_models() == 2 - adopted
    assert ingest_rollup_state.adopt_original_models() == 0


def _replay_state():
    with db.viz_conn() as c:
        return c.execute(
            "SELECT r.file_id, r.line_num, dm.value, rp.cost_usd, "
            "rp.rate_fingerprint_id, f.models FROM records r "
            "JOIN records_price rp USING (file_id, line_num) "
            "JOIN files f USING (file_id) JOIN dim_model dm ON dm.id = r.model_id "
            "WHERE r.is_replay ORDER BY r.file_id, r.line_num").fetchall()


def test_adoption_batches_sidecars_and_counts_vacuum_rows(fresh_db, monkeypatch):
    _seed_many(4)
    monkeypatch.setattr(ingest_reprice, "REPRICE_BATCH", 3)
    batches = []
    reprice = ingest_model_adoption.reprice_adopted

    def observed(conn, rows):
        if rows:
            batches.append((sorted(row[:2] for row in rows), conn.execute(
                "SELECT txid_current(), current_setting('work_mem')").fetchone()))
        return reprice(conn, rows)

    monkeypatch.setattr(ingest_model_adoption, "reprice_adopted", observed)
    ingest_vacuum.begin_run()
    assert ingest_rollup_state.adopt_original_models() == 8
    print("adoption batch sizes:", [len(keys) for keys, _ in batches])
    assert [len(keys) for keys, _ in batches] == [3, 3, 2]
    assert len({state[0] for _, state in batches}) == 3
    assert all(state[1] == "64MB" for _, state in batches)
    keys = [key for batch, _ in batches for key in batch]
    assert len(keys) == len(set(keys)) == 8, "each replay is adopted exactly once"
    rows = _replay_state()
    assert len(rows) == 8
    assert all(row[2] == "gpt-5.6-sol" and row[4] is not None
               and "gpt-5.6-sol" in row[5] for row in rows)
    assert ingest_vacuum.take_phase_rows() == {"reprice": 8}
    assert ingest_rollup_state.adopt_original_models() == 0


def test_adoption_peak_allocation_is_bounded(fresh_db, monkeypatch):
    """Bound live Python batch payload with collection scheduling controlled."""
    batch_size = 32
    monkeypatch.setattr(ingest_reprice, "REPRICE_BATCH", batch_size)
    reprice = ingest_model_adoption.reprice_adopted

    def collect_and_reprice(conn, rows):
        # Collect driver cursor/adapter cycles between batches so GC scheduling
        # does not confound the peak of the live returned rows and write arrays.
        gc.collect()
        return reprice(conn, rows)

    monkeypatch.setattr(ingest_model_adoption, "reprice_adopted", collect_and_reprice)
    rate_fingerprint.pair_fingerprint("gpt-5.6-sol", "adoption-host")
    peaks = []
    for per_file in (batch_size, batch_size * 32):
        if peaks:
            with db.viz_conn() as c:
                c.execute("DELETE FROM projects")
                c.commit()
        _seed_many(per_file)
        gc.collect()
        tracemalloc.start()
        try:
            assert ingest_rollup_state.adopt_original_models() == 2 * per_file
            peaks.append(tracemalloc.get_traced_memory()[1])
        finally:
            tracemalloc.stop()
    print(f"adoption peaks B: rows=64 {peaks[0]}, rows=2048 {peaks[1]}, batch={batch_size}")
    # Two adjacent batches can coexist while the next fetch replaces the old
    # rows. Allow twice the small run's entire peak, independent of set size;
    # the 32x larger set defeats an unbounded fetch/sidecar/update array.
    assert peaks[1] <= 2 * peaks[0], peaks


def test_scoped_live_batch_payload_and_single_identity_materialization(fresh_db, monkeypatch):
    monkeypatch.setattr(ingest_reprice, "REPRICE_BATCH", 32)
    scope = Scope(False, "test", 2000)
    scope.affected_uuids = {f"u{file}" + (f"-{i}" if i else "")
                            for file in (1, 2) for i in range(4096)}
    real_sorted = builtins.sorted
    sorts = []

    def counted(values, *args, **kwargs):
        if values is scope.affected_uuids:
            sorts.append(1)
        return real_sorted(values, *args, **kwargs)

    monkeypatch.setattr(builtins, "sorted", counted)
    real_reprice = ingest_model_adoption.reprice_adopted

    def collected(conn, rows):
        gc.collect()
        return real_reprice(conn, rows)

    monkeypatch.setattr(ingest_model_adoption, "reprice_adopted", collected)
    rate_fingerprint.pair_fingerprint("gpt-5.6-sol", "adoption-host")
    peaks, materializations = [], []
    for per_file in (32, 1024):
        if peaks:
            with db.viz_conn() as conn:
                conn.execute("DELETE FROM projects")
                conn.commit()
        _seed_many(per_file)
        sorts.clear()
        gc.collect()
        tracemalloc.start()
        try:
            assert ingest_rollup_state.adopt_original_models(scope) == 2 * per_file
            peaks.append(tracemalloc.get_traced_memory()[1])
            materializations.append(len(sorts))
        finally:
            tracemalloc.stop()
    print("scoped live peaks B, fixed 8192 identities:", peaks,
          "identity materializations:", materializations)
    # Both runs hold the same identity filter; only batch count grows 32-fold.
    # Two adjacent batch payloads may overlap. Forced GC isolates live Python
    # payload, not process/database memory. One filter copy is allowed per call.
    assert peaks[1] <= 2 * peaks[0], peaks
    assert materializations == [1, 1], "reuse one sorted identity array across batches"


@pytest.mark.parametrize("batch_size", [1, 3])
def test_failure_after_second_sidecar_write_rolls_back_only_that_batch(
        fresh_db, monkeypatch, batch_size):
    _seed_many(4)
    monkeypatch.setattr(ingest_reprice, "REPRICE_BATCH", batch_size)
    before = _replay_state()
    real_reprice = ingest_model_adoption.reprice_adopted
    batches = []

    def fail_second(conn, rows):
        batches.append({row[:2] for row in rows})
        count = real_reprice(conn, rows)
        if len(batches) == 2:
            raise RuntimeError("injected second-batch sidecar failure")
        return count

    monkeypatch.setattr(ingest_model_adoption, "reprice_adopted", fail_second)
    ingest_vacuum.begin_run()
    with pytest.raises(RuntimeError, match="second-batch sidecar failure"):
        ingest_rollup_state.adopt_original_models()
    assert len(batches) == 2
    committed_files = {key[0] for key in batches[0]}
    if batch_size == 1:
        assert set(row[0] for row in before) - committed_files
    after = _replay_state()
    for old, row in zip(before, after):
        if row[:2] in batches[0]:
            assert row[2] == "gpt-5.6-sol" and row[4] is not None
        else:
            assert row[:5] == old[:5], "failing and later rows roll back model AND price"
        if row[0] not in committed_files:
            assert row[5] == old[5], "uncommitted file metadata rolls back too"
    assert ingest_vacuum.take_phase_rows() == {"reprice": batch_size}
    monkeypatch.setattr(ingest_model_adoption, "reprice_adopted", real_reprice)
    assert ingest_rollup_state.adopt_original_models() == 8 - batch_size
    assert ingest_rollup_state.adopt_original_models() == 0


@pytest.mark.parametrize("entry", ["direct", "canonical", "ingest"])
@pytest.mark.parametrize("signal", ["truthy", "raises"])
def test_adoption_abort_keeps_committed_batches_and_resumes(
        fresh_db, monkeypatch, entry, signal):
    _seed_many(4)
    monkeypatch.setattr(ingest_reprice, "REPRICE_BATCH", 3)
    before = _replay_state()
    committed_keys = set()
    checks = []

    def observed(conn, rows, real_reprice=ingest_model_adoption.reprice_adopted):
        committed_keys.update(row[:2] for row in rows)
        return real_reprice(conn, rows)

    monkeypatch.setattr(ingest_model_adoption, "reprice_adopted", observed)

    def stop():
        checks.append(1)
        rows = _replay_state()
        if any(row[4] is not None for row in rows):
            if signal == "raises" or entry == "ingest":
                raise IngestAborted("test shutdown")
            return True
        return False

    ingest_vacuum.begin_run()
    with pytest.raises(IngestAborted):
        if entry == "ingest":
            monkeypatch.setattr(ingest, "_check_shutdown", stop)
            for name in ("purge_suppressed", "reprice_stale", "rekey_folded_projects"):
                monkeypatch.setattr(ingest, name, lambda **kwargs: 0)
            ingest._rebuild_derived_state()  # pylint: disable=protected-access
        elif entry == "canonical":
            ingest_rollup_state.recompute_canonical(should_stop=stop)
        else:
            ingest_rollup_state.adopt_original_models(should_stop=stop)
    after = _replay_state()
    if entry != "ingest":
        assert len(checks) == 2, "one shutdown check at the top of each batch"
    assert len(committed_keys) == 3, "only the first batch opens"
    for old, row in zip(before, after):
        if row[:2] not in committed_keys:
            assert row[:5] == old[:5], "unselected model and sidecar stay untouched"
        if row[0] not in {key[0] for key in committed_keys}:
            assert row[5] == old[5], "unselected file's models stay untouched"
    winner = "gpt-5.6-sol"
    expected_cost = round(pricing.compute_cost(
        winner, fresh=300000, output=1000, eph5=0, eph1h=0, unsplit_create=0, read=0,
        adjustments=pricing.CostAdjustments(
            long_context=300000 > pricing.long_context_threshold(winner),
            web_search_requests=2),
        res=pricing.resolve(winner, datetime(2026, 1, 1, tzinfo=timezone.utc),
                            "adoption-host")), 6)
    for row in after:
        if row[:2] not in committed_keys:
            continue
        assert row[2] == "gpt-5.6-sol"
        assert float(row[3]) == expected_cost
        assert row[4] is not None and "gpt-5.6-sol" in row[5]
    assert ingest_vacuum.take_phase_rows().get("reprice") == 3
    assert ingest_rollup_state.adopt_original_models() == 5
    assert all(row[2] == "gpt-5.6-sol" and float(row[3]) == expected_cost
               and row[4] is not None for row in _replay_state())
    assert ingest_rollup_state.adopt_original_models() == 0
