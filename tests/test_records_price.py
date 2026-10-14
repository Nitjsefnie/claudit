"""The records_price sidecar (issue #959, ruling R4).

The reprice pass rewrites its six columns on every PRICING_VERSION bump.
Tuple versions are whole-row, so an in-row UPDATE rewrites the whole
`records` tuple — which is why #350 had to store `records` at
fillfactor 50 (measured 57.6s / 2.66 GB WAL per full-table restamp at
100, 12.2s / 499 MB at 50, stable across consecutive bump cycles).
Ruling R4 splits the reprice-written columns onto a narrow
(file_id, line_num) sidecar at fillfactor 50; `records` returns to 100
and stops reserving half of every page.

These tests pin the split: the sidecar's applied shape and BOTH
fillfactors, the pair invariant's cascade, the guarded carry-over that
migrates an already-stamped in-row database without losing a value,
the real-ingest round trip, the guarantee that the reprice pass never
issues an UPDATE against `records`, and byte-identical cost-reading
endpoint responses over a fixed seed.
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from itertools import takewhile
from pathlib import Path
from typing import cast

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture,
    _mini_r2_env_fixture,
)

from backend import (
    api,
    constants,
    db,
    ingest,
    ingest_reprice,
    pricing,
    rate_fingerprint,
    schema_files,
)
from backend.cache import response_cache
from tests import dims_seed, scratch_db
from tests.reprice_fixture_builders import (
    SEED_INPUTS,
    SEED_MODEL,
    SEED_TS,
    SeedRecord,
    seed_record,
)

UTC = timezone.utc
_FILE_KEY = "claude/reprice-test/sess-a/sess-a.jsonl"

# The moved columns, verbatim: R4's list plus request_fee_usd, which
# _SQL_REPRICE writes (SET request_fee_usd = NULL — the retired legacy
# fee column). web_search_requests and text_chars are NOT here: no
# reprice UPDATE writes them (they are parse-time inputs/outputs).
_MOVED = (
    "cost_usd",
    "long_context",
    "long_context_input_mult",
    "long_context_output_mult",
    "pricing_version_id",
    "rate_fingerprint_id",
    "request_fee_usd",
)


def _reloptions(conn, table: str) -> list[str]:
    row = conn.execute(
        "SELECT COALESCE(reloptions, '{}') FROM pg_class WHERE relname = %s",
        (table,)).fetchone()
    assert row is not None, f"no pg_class row for {table}"
    return list(row[0])


def _pk_def(conn, table: str) -> str:
    row = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = %s::regclass AND contype = 'p'",
        (table,)).fetchone()
    assert row is not None, f"no primary key on {table}"
    return row[0]


def _columns(conn, table: str) -> set[str]:
    return scratch_db.table_columns(conn, table)


def test_the_sidecar_shape_and_both_fillfactors_are_applied(fresh_db):
    """The split as the database actually stores it: records_price keyed
    (file_id, line_num) with the cascade back to records, sidecar at
    fillfactor 50 (#350's HOT pages) and records at 100 — the reloption
    every startup stamps (SV-SCHEMA-AUTOAPPLY), not just a source line."""
    with db.viz_conn() as conn:
        assert _pk_def(conn, "records_price") == "PRIMARY KEY (file_id, line_num)"
        cascade = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'records_price'::regclass AND confrelid = "
            "'records'::regclass").fetchone()
        assert cascade is not None, (
            "records_price must reference records so its rows cascade away "
            "with them")
        assert "ON DELETE CASCADE" in cascade[0]
        assert _reloptions(conn, "records_price") == ["fillfactor=50"]
        assert _reloptions(conn, "records") == ["fillfactor=100"]
        price_cols = _columns(conn, "records_price")
        rec_cols = _columns(conn, "records")
        for column in _MOVED:
            assert column in price_cols, column
            assert column not in rec_cols, column


def test_no_schema_line_pins_records_at_fillfactor_50(fresh_db):
    """Issue #350's storage fix moves with the columns it was protecting:
    after the split, every fillfactor statement the schema chain applies
    to `records` says 100, and 50 is stated for records_price alone.
    PostgreSQL applies the LAST assignment, so a surviving 50 anywhere
    in the chain would win silently over the pinned 100."""
    chain = "\n".join(path.read_text(encoding="utf-8")
                      for path in schema_files.SCHEMA_PATHS)
    assert re.findall(r"ALTER TABLE records SET \(fillfactor = (\d+)\);",
                      chain) == ["100"]
    assert not re.search(r"ALTER TABLE records SET \(fillfactor = 50\)", chain)
    assert re.findall(r"ALTER TABLE records_price SET \(fillfactor = (\d+)\);",
                      chain) == ["50"]
    with db.viz_conn() as conn:
        assert _reloptions(conn, "records") == ["fillfactor=100"]


def test_price_rows_round_trip_and_cascade_with_their_records(fresh_db):
    """The pair invariant: one sidecar row per records row, keyed by the
    records PK, and every way a records row dies — the per-file wipe, a
    suppressed-model purge, the files cascade — takes its sidecar row
    with it (an orphan would shadow nothing today and conflict on the
    next reparse's insert)."""
    with db.viz_conn() as conn:
        seed_record(conn, _FILE_KEY, 1)
        seed_record(conn, _FILE_KEY, 2, SeedRecord(cost_usd=0.25))
        conn.commit()
        assert (conn.execute(
            "SELECT COUNT(*) FROM records_price").fetchone() or (0,))[0] == 2
        conn.execute("DELETE FROM records WHERE line_num = 1")
        assert (conn.execute(
            "SELECT COUNT(*) FROM records_price WHERE line_num = 1"
        ).fetchone() or (0,))[0] == 0, (
            "a deleted records row must take its price row")
        conn.execute("DELETE FROM files")
        assert (conn.execute(
            "SELECT COUNT(*) FROM records").fetchone() or (0,))[0] == 0
        assert (conn.execute(
            "SELECT COUNT(*) FROM records_price").fetchone() or (0,))[0] == 0


def test_ingest_stores_a_price_row_per_record(fresh_db, mini_r2_env):
    """The real persist path keeps the invariant: after a full ingest of
    the mini mirror, every stored record has exactly one sidecar row
    carrying its stored cost, and no sidecar row is left behind."""
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    with db.viz_conn() as conn:
        orphans = (conn.execute(
            "SELECT COUNT(*) FROM records r "
            "LEFT JOIN records_price rp "
            "  ON rp.file_id = r.file_id AND rp.line_num = r.line_num "
            "WHERE rp.file_id IS NULL").fetchone() or (0,))[0]
        assert orphans == 0, "persist must write a price row per record"
        extras = (conn.execute(
            "SELECT COUNT(*) FROM records_price rp "
            "LEFT JOIN records r "
            "  ON r.file_id = rp.file_id AND r.line_num = rp.line_num "
            "WHERE r.file_id IS NULL").fetchone() or (0,))[0]
        assert extras == 0, "no sidecar row may outlive its record"
        priced = (conn.execute(
            "SELECT COUNT(*) FROM records_price WHERE cost_usd IS NOT NULL"
        ).fetchone() or (0,))[0]
        total = (conn.execute(
            "SELECT COUNT(*) FROM records_price").fetchone() or (0,))[0]
        assert priced == total, "persist stamps every sidecar row's cost"


@pytest.mark.db
def test_the_split_carries_in_row_values_into_the_sidecar(monkeypatch):
    """A dev database already stamped at PARSER_VERSION 108 (the in-row
    shape schema_dims.sql of tasks 1-2 creates) converges with its values
    intact: the guarded block sees records.cost_usd, copies every moved
    column into the sidecar, then drops the in-row columns — no value
    vanishes, nothing is zeroed — and a second apply is a no-op."""
    boundary = "schema_records_price.sql"
    assert sum(path.name == boundary for path in scratch_db.SCHEMA) == 1, (
        f"scratch schema must contain {boundary} exactly once")
    name = scratch_db.create_database(
        "price_migrate", schema=tuple(takewhile(
            lambda path: path.name != boundary,
            scratch_db.SCHEMA)))
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
    db.reset_viz_pool()
    try:
        now = datetime.now(timezone.utc)
        with psycopg.connect(f"postgresql:///{name}") as conn:
            conn.execute(
                "INSERT INTO projects (project_id, display_name, "
                "first_seen_at, last_seen_at) VALUES ('p-m', 'p-m', %s, %s)",
                (now, now))
            conn.execute(
                "INSERT INTO files (file_key, project_id, session_id, "
                "is_main, r2_etag, r2_size_bytes, r2_last_modified, "
                "parsed_at, parser_version) VALUES ('k/p/m.jsonl', 'p-m', "
                "'s-m', TRUE, 'e', 1, %s, %s, %s)", (now, now, "108"))
            conn.execute(
                "INSERT INTO dim_model (value) VALUES ('m') "
                "ON CONFLICT (value) DO UPDATE SET value = EXCLUDED.value")
            conn.execute(
                "INSERT INTO dim_pricing_version (value) VALUES ('77') "
                "ON CONFLICT (value) DO UPDATE SET value = EXCLUDED.value")
            conn.execute(
                "INSERT INTO dim_rate_fingerprint (value) VALUES ('fp-x') "
                "ON CONFLICT (value) DO UPDATE SET value = EXCLUDED.value")
            conn.execute(
                "INSERT INTO records (file_id, line_num, model_id, ts, "
                "cost_usd, long_context, long_context_input_mult, "
                "long_context_output_mult, request_fee_usd, "
                "pricing_version_id, rate_fingerprint_id) "
                "SELECT file_id, 1, id, %s, 0.5, TRUE, 1.5, 2.0, 0.25, "
                "  (SELECT id FROM dim_pricing_version WHERE value='77'), "
                "  (SELECT id FROM dim_rate_fingerprint WHERE value='fp-x') "
                "FROM files, dim_model "
                "WHERE files.file_key = 'k/p/m.jsonl' AND dim_model.value='m'",
                (now,))
            conn.execute("DELETE FROM schema_stamp")
            conn.commit()

        db.apply_schema()
        db.apply_schema()  # a second stamp run must be a no-op

        with psycopg.connect(f"postgresql:///{name}") as conn:
            row = conn.execute(
                "SELECT rp.cost_usd, rp.long_context, "
                "       rp.long_context_input_mult, "
                "       rp.long_context_output_mult, rp.request_fee_usd, "
                "       pv.value, df.value "
                "  FROM records_price rp "
                "  JOIN files f ON f.file_id = rp.file_id "
                "  LEFT JOIN dim_pricing_version pv "
                "    ON pv.id = rp.pricing_version_id "
                "  LEFT JOIN dim_rate_fingerprint df "
                "    ON df.id = rp.rate_fingerprint_id "
                " WHERE f.file_key = 'k/p/m.jsonl'").fetchone()
            assert row is not None, "the carried row must reach the sidecar"
            assert row == (0.5, True, 1.5, 2.0, 0.25, "77", "fp-x"), (
                "every in-row value must be carried over verbatim")
            cols = _columns(conn, "records")
            for column in _MOVED:
                assert column not in cols, column
            assert _reloptions(conn, "records") == ["fillfactor=100"]
            assert (conn.execute(
                "SELECT COUNT(*) FROM records_price"
            ).fetchone() or (0,))[0] == 1
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)


def test_reprice_writes_only_the_sidecar(fresh_db):
    """All three of the pass's UPDATE statements — Phase A's clean
    restamp, the keyset restamp and the keyset reprice — must land on
    records_price and never issue an UPDATE against `records`: a
    BEFORE UPDATE trigger on records turns any such write into the
    test's failure, and each path is exercised (clean-restamp row,
    keyset-restamp row, moved-cost row)."""
    correct = round(pricing.compute_cost(
        SEED_MODEL, **SEED_INPUTS, ts=SEED_TS,
        adjustments=pricing.CostAdjustments(long_context=False)), 6)
    current_fp = rate_fingerprint.pair_fingerprint(SEED_MODEL, None)
    with db.viz_conn() as conn:
        # A: current fingerprint, correct cost -> Phase A clean restamp.
        seed_record(conn, _FILE_KEY, 1, SeedRecord(
            pricing_version="0", cost_usd=correct,
            rate_fingerprint=current_fp))
        # B: stale fingerprint, correct cost -> keyset restamp.
        seed_record(conn, _FILE_KEY, 2, SeedRecord(
            pricing_version="0", cost_usd=correct,
            rate_fingerprint="stale-string"))
        # C: stale fingerprint, wrong cost -> keyset reprice.
        seed_record(conn, _FILE_KEY, 3, SeedRecord(
            pricing_version="0", cost_usd=0.1,
            rate_fingerprint="stale-string"))
        conn.execute(
            "CREATE FUNCTION _block_records_update() RETURNS trigger AS "
            "$fn$ BEGIN RAISE EXCEPTION 'reprice updated records'; END; "
            "$fn$ LANGUAGE plpgsql")
        conn.execute(
            "CREATE TRIGGER _no_records_update BEFORE UPDATE ON records "
            "FOR EACH ROW EXECUTE FUNCTION _block_records_update()")
        conn.commit()

    assert ingest_reprice.reprice_stale() == 1, (
        "only the moved row reports changed; the two restamps are silent")

    with db.viz_conn() as conn:
        rows = conn.execute(
            "SELECT rp.line_num, rp.cost_usd, pv.value "
            "  FROM records_price rp JOIN files f ON f.file_id = rp.file_id "
            "  LEFT JOIN dim_pricing_version pv "
            "    ON pv.id = rp.pricing_version_id "
            " WHERE f.file_key = %s ORDER BY rp.line_num",
            (_FILE_KEY,)).fetchall()
    assert [float(r[1]) for r in rows] == [correct, correct, correct]
    assert [r[2] for r in rows] == [constants.PRICING_VERSION] * 3


def test_no_reprice_statement_targets_records_in_source():
    """The source-level companion to the trigger test: every UPDATE
    statement the pass owns names records_price, so a future statement
    that drifts back onto `records` fails here even before it runs.
    Only the SQL constants are scanned — prose may say "UPDATE" in any
    shape, the statements cannot."""
    src = Path(ingest_reprice.__file__).read_text(encoding="utf-8")
    sql_consts = re.findall(r'_SQL_[A-Z_]+ = """(.*?)"""', src, re.S)
    updates = [stmt for stmt in sql_consts if "UPDATE" in stmt]
    assert len(updates) == 3, sql_consts
    for stmt in updates:
        assert re.search(r"UPDATE\s+records_price\b", stmt), stmt
        assert not re.search(r"UPDATE\s+records\b", stmt), stmt


# ---------------------------------------------------------------------------
# Cost-reading endpoint equality over a fixed seed: byte-identical
# responses for the small cost-by panels, exact cost fields for the
# heavier readers. The dataset uses three
# rows, costs derived from dated rates, one long-context meter
# row, one web-search row.
# ---------------------------------------------------------------------------

_EP_TS = datetime.now(UTC).replace(hour=1, minute=0, second=0,
                                   microsecond=0)
_EP_FILE = "capture/sess/main.jsonl"
_EP_SESSION = "sess-capture"
_EP_PROJECT = "capture-project"
_EP_TTLS = {1: (100, 200), 2: (0, 0), 3: (0, 0)}


def _endpoint_buckets(tokens: tuple[int, ...],
                      factors: tuple[float, float] = (1.0, 1.0),
                      searches: int = 0) -> dict[str, float]:
    """Independent TTL arithmetic over the seed's dated pricing rates."""
    rates = pricing.resolve(SEED_MODEL, _EP_TS).rates
    fresh, create, read, output, eph5, eph1h = tokens
    in_mult, out_mult = factors
    return {
        "fresh": fresh * rates["fresh"] * in_mult / 1_000_000,
        "create_5m": eph5 * rates["create_5m"] * in_mult / 1_000_000,
        "create_1h": (eph1h + max(0, create - eph5 - eph1h))
        * rates["create_1h"] * in_mult / 1_000_000,
        "read": read * rates["read"] * in_mult / 1_000_000,
        "output": output * rates["output"] * out_mult / 1_000_000,
        "web_search": searches * rates.get("web_search", 0.0),
    }


# (line, fresh, create, read, output, cost, long_context, mults, ws).
# NUMERIC(12,6) stores each independently priced seed cost at six places.
# The search row stores half the token cost plus the full search fee.
_EP_ROWS = [
    (line, fresh, create, read, output,
     round(sum(value * (0.5 if lc and key != "web_search" else 1.0)
               for key, value in _endpoint_buckets(
         (fresh, create, read, output, *_EP_TTLS[line]),
         cast(tuple[float, float], mults) if lc else (1.0, 1.0),
         ws or 0).items()), 6), lc, mults, ws)
    for line, fresh, create, read, output, lc, mults, ws in (
        (1, 1000, 500, 2000, 100, False, (None, None), None),
        (2, 300000, 0, 0, 50, True, (1.5, 2.0), 3),
        (3, 42, 7, 13, 9, False, (None, None), None),
    )
]
_EP_SMALL_COST = round(_EP_ROWS[0][5] + _EP_ROWS[2][5], 6)
_EP_TOTAL_COST = round(sum(row[5] for row in _EP_ROWS), 6)


def _seed_endpoint_rows(conn) -> None:
    conn.execute(
        "INSERT INTO projects (project_id, display_name, first_seen_at, "
        "last_seen_at) VALUES (%s, %s, %s, %s)",
        (_EP_PROJECT, _EP_PROJECT, _EP_TS, _EP_TS))
    conn.execute(
        "INSERT INTO files (file_key, project_id, session_id, is_main, "
        "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
        "parser_version, agent_type) VALUES (%s, %s, %s, TRUE, 'e', 1, "
        "%s, %s, %s, 'general-purpose')",
        (_EP_FILE, _EP_PROJECT, _EP_SESSION, _EP_TS, _EP_TS,
         constants.PARSER_VERSION))
    model_id = dims_seed.dim_id(conn, "dim_model", SEED_MODEL)
    fid = dims_seed.file_id(conn, _EP_FILE)
    for (line, fresh, create, read, output, cost, lc, (im, om),
         ws) in _EP_ROWS:
        conn.execute(
            "INSERT INTO records (file_id, line_num, fallback, request_id, ts, "
            "model_id, fresh_tokens, cache_creation_tokens, "
            "cache_read_tokens, output_tokens, eph5_tokens, eph1h_tokens, "
            "text_chars, web_search_requests, thinking_tokens) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "10)",
            (fid, line, f"uuid-{line}", f"req-{line}", _EP_TS, model_id,
             fresh, create, read, output, *_EP_TTLS[line],
             (fresh + create + read) * 4, ws))
        conn.execute(
            "INSERT INTO records_price (file_id, line_num, cost_usd, "
            "long_context, long_context_input_mult, "
            "long_context_output_mult) VALUES (%s, %s, %s, %s, %s, %s)",
            (fid, line, cost, lc, im, om))
    conn.commit()


@pytest.fixture(name="cost_client")
def _cost_client_fixture(fresh_db, monkeypatch):
    """TestClient over the seed, with every DB session pinned to UTC.

    The endpoints render timestamptz through psycopg's session `TimeZone`
    offset (`_iso`'s bare isoformat, and the JSON encoder for raw rows),
    so without the pin the byte assertions depend on the runner: CI's
    postgres service defaults to UTC, a dev cluster to its system zone.
    With the pin, actual and `_bytes(expected)` are UTC everywhere.
    """
    response_cache.clear()
    original = db.viz_conn

    @contextmanager
    def utc_conn():
        with original() as conn:
            conn.execute("SET TimeZone = 'UTC'")
            yield conn

    monkeypatch.setattr(db, "viz_conn", utc_conn)
    with db.viz_conn() as conn:
        _seed_endpoint_rows(conn)
    application = FastAPI()
    application.include_router(api.router)
    with TestClient(application) as client:
        yield client
    response_cache.clear()


def _bytes(payload) -> bytes:
    """What FastAPI's JSONResponse emits for `payload`."""
    return json.dumps(payload, ensure_ascii=False, allow_nan=False,
                      indent=None, separators=(",", ":")).encode("utf-8")


def test_cost_by_context_response_is_byte_identical(cost_client):
    """The live cost-by-context path (range=1d takes the records pass)
    must return exactly the seeded truth, including cumulative shares."""
    total = _EP_SMALL_COST + _EP_ROWS[1][5]
    expected = {
        "range": "1d", "project": None, "model": None,
        "bucket_width": 50000, "bucket_max": 1000000,
        "total_cost_usd": total,
        "total_tokens": 303721, "buckets": [
            {"ctx_bucket": 0, "requests": 2, "total_tokens": 3671,
             "cost_usd": _EP_SMALL_COST, "cum_share": _EP_SMALL_COST / total,
             "cum_token_share": 3671 / 303721},
            {"ctx_bucket": 300000, "requests": 1, "total_tokens": 300050,
             "cost_usd": _EP_ROWS[1][5], "cum_share": 1.0,
             "cum_token_share": 1.0},
        ],
    }
    resp = cost_client.get("/api/cost-by-context?range=1d")
    assert resp.status_code == 200
    assert resp.content == _bytes(expected)


def test_cost_by_agent_response_is_byte_identical(cost_client):
    """Same for the live cost-by-agent path."""
    expected = {
        "range": "1d", "project": None, "model": None,
        "total_cost_usd": _EP_TOTAL_COST, "total_tokens": 303721,
        "agents": [{"agent_type": "general-purpose", "requests": 3,
                    "output_tokens": 159, "total_tokens": 303721,
                    "cost_usd": _EP_TOTAL_COST, "share": 1.0}],
    }
    resp = cost_client.get("/api/cost-by-agent?range=1d")
    assert resp.status_code == 200
    assert resp.content == _bytes(expected)


def test_sessions_list_response_is_byte_identical(cost_client):
    """The sessions list sums cost through the sidecar join; its full
    bytes over the fixed seed must not move."""
    stamp = _EP_TS.isoformat()  # UTC: cost_client pins the session TimeZone
    expected = {
        "items": [{
            "session_id": _EP_SESSION, "project_id": _EP_PROJECT,
            "first_event_at": stamp, "last_event_at": stamp,
            "duration_s": 0, "request_count": 3,
            "input_tokens": 301042, "output_tokens": 159,
            "cache_create_5m_tokens": 100, "cache_create_1h_tokens": 200,
            "cache_read_tokens": 2013, "cost_usd": _EP_TOTAL_COST,
            "models": {SEED_MODEL: 3}, "limit_hits": 0,
        }],
        "next_cursor": None,
    }
    resp = cost_client.get("/api/sessions?limit=50")
    assert resp.status_code == 200
    assert resp.content == _bytes(expected)


# ---------------------------------------------------------------------------
# Full-response BYTE assertions for the three heavier readers that used to
# assert JSON field equality only: /api/cache, /api/dashboard and the
# session detail. Every leaf — not just the cost ones — must match the
# seeded truth, through the sidecar join, exactly as the starlette
# JSONResponse serialises it (_bytes above).
# ---------------------------------------------------------------------------


def test_cache_response_is_byte_identical(cost_client):
    """The /api/cache fold's full bytes: model rows, the TTL-split
    buckets, and all three top-N lists in their measure order."""
    stamp = _EP_TS.isoformat()  # UTC: cost_client pins the session TimeZone
    # SQL groups rows 1+3 together, separately from the metered search row.
    plain = _endpoint_buckets((1042, 507, 2013, 109, 100, 200))
    metered = _endpoint_buckets((300000, 0, 0, 50, 0, 0), (1.5, 2.0), 3)
    search = metered.pop("web_search")
    # The search group's token decomposition reconciles to its stored total.
    scale = (_EP_ROWS[1][5] - search) / sum(metered.values())
    buckets = {key: round(value * scale + plain[key], 4)
               for key, value in metered.items()}
    buckets["web_search"] = round(search + plain["web_search"], 4)
    total = round(_EP_ROWS[1][5] + _EP_SMALL_COST, 4)
    expected = {
        "range": "all", "project": None,
        "per_model": [{
            "model": SEED_MODEL, "turns": 3,
            "fresh": 301042, "cache_create": 507, "cache_read": 2013,
            "output": 159, "eph5": 100, "eph1h": 200,
            "cost_total": total, "estimated_rate": False,
            "banded_rate": False, "hit_rate_pct": 0.7,
        }],
        "per_model_provider": [{
            "model": SEED_MODEL, "turns": 3,
            "fresh": 301042, "cache_create": 507, "cache_read": 2013,
            "output": 159, "eph5": 100, "eph1h": 200,
            "cost_total": total, "estimated_rate": False,
            "banded_rate": False, "provider": None, "hit_rate_pct": 0.7,
        }],
        "session_total": {
            "turns": 3, "fresh": 301042, "cache_create": 507,
            "cache_read": 2013, "output": 159, "eph5": 100, "eph1h": 200,
            "cost_total": total,
            "cost_buckets": buckets,
            "estimated_rate": False, "banded_rate": False,
            "hit_rate_pct": 0.7,
        },
        "top_output": [
            {"ts": stamp, "line": line, "request_id": f"req-{line}",
             "model": SEED_MODEL, "output": out, "c_read": read,
             "c_create_1h": _EP_TTLS[line][1], "c_create_5m": _EP_TTLS[line][0], "fresh": fresh,
             "cost": float(cost), "file_key": _EP_FILE}
            # ORDER BY output_tokens DESC over the seed's three rows
            # (r[4] is output_tokens).
            for (line, fresh, _create, read, out, cost, _lc, _mult,
                 _ws) in sorted(_EP_ROWS, key=lambda r: -r[4])
        ],
        "top_cache_create": [
            {"ts": stamp, "line": line, "request_id": f"req-{line}",
             "model": SEED_MODEL, "c_create": create, "c_create_1h": _EP_TTLS[line][1],
             "c_create_5m": _EP_TTLS[line][0], "c_read": read, "output": out,
             "fresh": fresh, "cost": float(cost), "file_key": _EP_FILE}
            # WHERE cache_creation_tokens > 0, ORDER BY it DESC: lines 1, 3.
            for (line, fresh, create, read, out, cost, _lc, _mult, _ws)
            in (_EP_ROWS[0], _EP_ROWS[2])
        ],
        "top_cache_read": [
            {"ts": stamp, "line": line, "request_id": f"req-{line}",
             "model": SEED_MODEL, "c_read": read, "c_create_1h": _EP_TTLS[line][1],
             "c_create_5m": _EP_TTLS[line][0], "output": out, "fresh": fresh,
             "cost": float(cost), "file_key": _EP_FILE}
            # WHERE cache_read_tokens > 0, ORDER BY it DESC: lines 1, 3.
            for (line, fresh, _create, read, out, cost, _lc, _mult,
                 _ws) in (_EP_ROWS[0], _EP_ROWS[2])
        ],
    }
    resp = cost_client.get("/api/cache?range=all")
    assert resp.status_code == 200
    assert resp.content == _bytes(expected)


def test_dashboard_response_is_byte_identical(cost_client):
    """The dashboard's full bytes over the seed — the live (<3600s)
    1800 s bucket path, the folds, and the file-count summary."""
    bucket = (_EP_TS + timedelta(minutes=15)).isoformat()  # UTC, pinned
    epoch = float(_EP_TS.timestamp())
    # Model totals add the two hourly floats, rather than the SQL total.
    hourly_cost = _EP_SMALL_COST + _EP_ROWS[1][5]
    expected = {
        "range": "4d", "project": None, "bucket_s": 1800,
        "hourly": [
            # One entry per (hour, model, long_context): rows 1+3 and
            # row 2 (the long-context meter row), bucket = epoch-aligned
            # 1800 s bucket midpoint.
            {"hour": bucket, "model": SEED_MODEL, "long_context": False,
             "provider": None, "input_tokens": 1042, "output_tokens": 109,
             "cache_5m_tokens": 100, "cache_1h_tokens": 200,
             "cache_read_tokens": 2013, "thinking_tokens": 20,
             "cost_usd": _EP_SMALL_COST, "requests": 2, "session_count": 1,
             "lines_added": 0, "lines_deleted": 0},
            {"hour": bucket, "model": SEED_MODEL, "long_context": True,
             "provider": None, "input_tokens": 300000, "output_tokens": 50,
             "cache_5m_tokens": 0, "cache_1h_tokens": 0,
             "cache_read_tokens": 0, "thinking_tokens": 10,
             "cost_usd": _EP_ROWS[1][5], "requests": 1, "session_count": 0,
             "lines_added": 0, "lines_deleted": 0},
        ],
        "token_types": ["input_tokens", "output_tokens",
                        "thinking_tokens", "cache_5m_tokens", "cache_1h_tokens", "cache_read_tokens"],
        "cost_by_model": [{"model": SEED_MODEL, "cost_usd": hourly_cost}],
        "cost_by_model_provider": [{"model": SEED_MODEL, "provider": None,
                                    "cost_usd": hourly_cost,
                                    "total_tokens": 303514}],
        "cost_by_project": [{"project": _EP_PROJECT, "cost_usd": _EP_TOTAL_COST}],
        "tokens_by_project": [{"project": _EP_PROJECT,
                               "total_tokens": 303721}],
        "rate_limit_hits": [],
        "hide_rate_limits": False,
        "sessions": [{
            "session_id": _EP_SESSION, "start_ts": epoch, "end_ts": epoch,
            "requests": 3, "input_tokens": 301042, "output_tokens": 159,
            "cache_create_tokens": 507, "cache_read_tokens": 2013,
            "cost_usd": _EP_TOTAL_COST, "model": SEED_MODEL,
            "models_used": [SEED_MODEL], "ctx_at_end": None,
        }],
        "total_sessions": 1, "main_w_usage": 1, "main_empty": 0,
        "subagent_files": 0, "subagent_only_sessions": 0,
        "total_prompts": 0, "total_turns": 0,
        # p50/p90 over the rows' text_chars ((fresh+create+read)*4 =
        # 14000, 1200000, 248): median 14000, linear p90 962800.
        "response_sizes": [{"ts": bucket, "model": SEED_MODEL, "n": 3,
                            "p50": 14000.0, "p90": 962800.0}],
    }
    resp = cost_client.get("/api/dashboard?range=4d")
    assert resp.status_code == 200
    assert resp.content == _bytes(expected)


def test_session_detail_response_is_byte_identical(cost_client):
    """The session detail's full bytes: the aggregates read through the
    sidecar join, the public r2_key, and the burn row."""
    stamp = _EP_TS.isoformat()  # UTC: cost_client pins the session TimeZone
    expected = {
        "session_id": _EP_SESSION, "project_id": _EP_PROJECT,
        "first_event_at": stamp, "last_event_at": stamp,
        "duration_s": 0, "request_count": 3,
        "input_tokens": 301042, "output_tokens": 159,
        "cache_create_5m_tokens": 100, "cache_create_1h_tokens": 200,
        "cache_read_tokens": 2013, "cost_usd": _EP_TOTAL_COST,
        "models": {SEED_MODEL: 3}, "limit_hits": 0,
        "r2_key": _EP_FILE, "ctx_trace": [],
        # tps = write tokens / max(duration, 1).
        "burn": {"tps": 301342.0, "model": SEED_MODEL,
                 "hit_5h_limit": False},
    }
    resp = cost_client.get(f"/api/sessions/{_EP_SESSION}")
    assert resp.status_code == 200
    assert resp.content == _bytes(expected)
