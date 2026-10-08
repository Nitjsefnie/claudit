"""The reprice differential (issue #351): the pre-#351 pass vs the
pair-qualified pass.

The pass frozen here as _legacy_reprice_stale is backend/ingest_reprice
at base 8d78ce5, kept verbatim so the pair-qualified pass must end
every seeded shape byte-identically on (cost_usd, long_context,
pricing_version). The frozen copy keeps importing _StaleRow,
_record_updates and _stored_pricing_version_is_newer (and
_row_is_unchanged, the fourth assembly helper) from
backend.ingest_reprice — they must not change behaviour — and keeps
its SQL constants local. Its one mechanical adaptation: the SELECT
names records.rate_fingerprint too, because the shared _StaleRow now
carries the field; the legacy writes stay exactly the pre-#351 shapes.

Split from test_reprice_parity.py, which sat one line under the
SV-CI-RATCHETS test ceiling once the differential landed.
"""
from __future__ import annotations

# duplicate-code at module scope: the frozen pre-#351 pass
# resembles the live one on purpose - the similarity IS the
# freeze the differential rests on (R0801 is only suppressible
# at module level).
# pylint: disable=duplicate-code

from typing import Any

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture,
)

from test_reprice import _FILE_KEY, _SEED_TS, _seed_parents

from backend import (
    constants,
    db,
    ingest,
    ingest_reprice,
    pricing,
    rate_fingerprint,
    timing,
)
from backend.ingest_reprice import (
    _record_updates,
    _row_is_unchanged,
    _StaleRow,
    _stored_pricing_version_is_newer,
)

UTC = timezone.utc

# Rates deliberately unlike any real price (SV-TEST-DATA): the boundary
# window below prices the seeded sonnet rows only through the two
# passes' shared resolution, never against a pinned rate.
_WINDOW_RATES = {"fresh": 1.0, "create_5m": 1.25, "create_1h": 2.0,
                 "read": 0.1, "output": 5.0}
_SONNET_END = datetime(2026, 3, 1, tzinfo=UTC)

# ---------------------------------------------------------------------------
# The differential (issue #351): the pre-#351 pass, frozen verbatim as
# _legacy_reprice_stale, and the pair-qualified pass must end every
# seeded shape at byte-identical (cost_usd, long_context, pricing_version).
# The frozen copy keeps importing _StaleRow, _record_updates and
# _stored_pricing_version_is_newer from backend.ingest_reprice — they are
# the assembly point and must not change behaviour — and keeps its SQL
# constants local. The one mechanical adaptation: its SELECT names the
# records.rate_fingerprint column too, because the shared _StaleRow now
# carries the field; the legacy writes stay exactly the pre-#351 shapes.
# (records.request_fee_usd joined the same SELECT when issue #469 added
# it to _StaleRow; the legacy writes stay the pre-#469 shapes.)
_LEGACY_SELECT_SQL = """
    SELECT file_key, line_num, model, fresh_tokens, cache_creation_tokens,
           cache_read_tokens, output_tokens, eph5_tokens, eph1h_tokens,
           ts, long_context, provider, cost_usd, pricing_version,
           rate_fingerprint, request_fee_usd
      FROM records
     WHERE (file_key, line_num) > (%s, %s)
       AND pricing_version IS DISTINCT FROM %s
     ORDER BY file_key, line_num
     LIMIT %s
"""

# A batch's unchanged rows: version only, no fingerprint — the
# pre-#351 write shape.
_LEGACY_SQL_RESTAMP = """
    UPDATE records r
       SET pricing_version = %s
      FROM unnest(%s::text[], %s::bigint[]) AS d(k, n)
     WHERE r.file_key = d.k
       AND r.line_num = d.n
"""

# A batch's moved rows: (cost, flag, version) — the pre-#351 shape.
_LEGACY_SQL_REPRICE = """
    UPDATE records r
       SET cost_usd = d.cost, long_context = d.flag, pricing_version = %s
      FROM unnest(%s::text[], %s::bigint[], %s::float8[], %s::boolean[])
           AS d(k, n, cost, flag)
     WHERE r.file_key = d.k
       AND r.line_num = d.n
"""

log = logging.getLogger("claudit.ingest")


def _legacy_reprice_stale(should_stop: Callable[[], bool | None] | None = None  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
                          ) -> int:
    """The pre-#351 pass, frozen at base 8d78ce5 for the differential
    below. Verbatim except: the SELECT names rate_fingerprint (the
    shared _StaleRow carries the field), and the module-level SQL
    constants, logger and REPRICE_BATCH/IngestAborted references
    resolve to the parity module's locals and backend.ingest_reprice's
    surviving names. duplicate-code is disabled because the frozen
    body resembling the live pass is the point: the similarity IS the
    freeze."""
    ph = timing.Phases("reprice", logger=log, account=True) \
        if timing.TIMING_ON else None
    marks: dict[str, float] = {}
    cpu0 = time.process_time()
    outcome = "failed"
    changed = 0
    restamped = 0
    skipped = 0
    batches = 0
    rows_seen = 0
    after_key: tuple[str, int] = ("", 0)
    try:
        while True:
            if should_stop is not None and should_stop():
                outcome = "aborted"
                raise ingest_reprice.IngestAborted("shutdown requested")
            with db.viz_conn() as c:
                t0 = time.perf_counter()
                cur = c.execute(
                    _LEGACY_SELECT_SQL,
                    (after_key[0], after_key[1], constants.PRICING_VERSION,
                     ingest_reprice.REPRICE_BATCH),
                )
                if ph is not None:
                    marks["select"] = (
                        marks.get("select", 0.0) + time.perf_counter() - t0)
                t0 = time.perf_counter()
                raw_rows = cur.fetchall()
                if ph is not None:
                    marks["fetch"] = (
                        marks.get("fetch", 0.0) + time.perf_counter() - t0)
                if not raw_rows:
                    break
                rows_seen += len(raw_rows)
                t0 = time.perf_counter()
                rows = [_StaleRow(*raw) for raw in raw_rows]
                restamp_keys: list[tuple[str, int]] = []
                moved: list[tuple[str, int, float, bool | None]] = []
                for row in rows:
                    if _stored_pricing_version_is_newer(
                            row.pricing_version, constants.PRICING_VERSION):
                        skipped += 1
                        continue
                    updates = _record_updates(row)
                    if _row_is_unchanged(row, updates):
                        restamp_keys.append((row.file_key, row.line_num))
                    else:
                        moved.append((row.file_key, row.line_num,
                                      updates["cost_usd"],
                                      updates["long_context"]))
                if ph is not None:
                    marks["recompute"] = (marks.get("recompute", 0.0)
                                          + time.perf_counter() - t0)
                t0 = time.perf_counter()
                if restamp_keys:
                    c.execute(_LEGACY_SQL_RESTAMP,
                              (constants.PRICING_VERSION,
                               [k for k, _ in restamp_keys],
                               [n for _, n in restamp_keys]))
                if ph is not None:
                    marks["restamp"] = (marks.get("restamp", 0.0)
                                        + time.perf_counter() - t0)
                t0 = time.perf_counter()
                if moved:
                    c.execute(_LEGACY_SQL_REPRICE,
                              (constants.PRICING_VERSION,
                               [r[0] for r in moved],
                               [r[1] for r in moved],
                               [r[2] for r in moved],
                               [r[3] for r in moved]))
                if ph is not None:
                    marks["moved"] = (marks.get("moved", 0.0)
                                      + time.perf_counter() - t0)
                t0 = time.perf_counter()
                c.commit()
                if ph is not None:
                    marks["commit"] = (marks.get("commit", 0.0)
                                       + time.perf_counter() - t0)
            changed += len(moved)
            restamped += len(restamp_keys)
            batches += 1
            last = rows[-1]
            after_key = (last.file_key, last.line_num)
        outcome = "complete"
    finally:
        if ph is not None:
            for label, seconds in marks.items():
                ph.mark(label, seconds)
            ph.done(batches=batches, rows=rows_seen, changed=changed,
                    outcome=outcome, cpu=f"{time.process_time() - cpu0:.1f}s")
    log.info(
        "reprice: %d record(s) repriced (rate-derived data changed), "
        "%d restamped only", changed, restamped)
    if skipped:
        log.info(
            "reprice: skipped %d record(s) priced by a NEWER "
            "PRICING_VERSION (rollback guard)", skipped)
    return changed


# Rates deliberately unlike any real price (SV-TEST-DATA): the moved
# window and the schedules price the seeded rows only through the two
# passes' shared resolution, never against a pinned rate.
_MOVED_WINDOW_RATES = {"fresh": 11.0, "create_5m": 13.75,
                       "create_1h": 22.0, "read": 1.1, "output": 55.0}
_SCHED_A_RATES = {"fresh": 0.75, "create_5m": 0.9375, "create_1h": 1.5,
                  "read": 0.075, "output": 3.75}
_SCHED_B_RATES = {"fresh": 0.25, "create_5m": 0.3125, "create_1h": 0.5,
                  "read": 0.025, "output": 1.25}

# The meter model, bound like _SEED_MODEL: a string constant naming a
# live rate row inside a pricing call is what the pinned-version guard
# flags; a module constant passed as a variable is the sanctioned shape
# (SV-TEST-DATA).
_METER_MODEL = "gpt-5.6-sol"

# One fixed tally for every seeded row: unsplit_create = 2000-250-500.
_TOKENS = (1000, 2000, 3000, 100, 250, 500)
_TALLY: dict[str, Any] = {"fresh": 1000, "output": 100,
                          "eph5": 250, "eph1h": 500,
                          "unsplit_create": 1250, "read": 3000}
_STALE = "0"


def _seed_shape_rows(c, rows: list[dict]) -> None:
    """One record per shape. Each row dict carries model, provider, ts,
    pricing_version, cost, rate_fingerprint and the stored flag; the
    tallies are the fixed ones above."""
    for line_num, row in enumerate(rows, start=1):
        fresh, create, read, output, eph5, eph1h = _TOKENS
        fresh = row.get("fresh", fresh)
        c.execute(
            "INSERT INTO records (file_key, line_num, ts, model, "
            "fresh_tokens, cache_creation_tokens, cache_read_tokens, "
            "output_tokens, eph5_tokens, eph1h_tokens, cost_usd, provider, "
            "long_context, pricing_version, rate_fingerprint) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s)",
            (_FILE_KEY, line_num, row.get("ts"), row["model"], fresh,
             create, read, output, eph5, eph1h, row["cost"],
             row.get("provider"), row.get("flag"), row["version"],
             row.get("fp")))


def _snapshot(c) -> tuple[list[str], list[tuple]]:
    """The whole records table as (column names, rows), ordered."""
    cur = c.execute("SELECT * FROM records ORDER BY file_key, line_num")
    names = [column.name for column in cur.description]
    return names, cur.fetchall()


def _restore(c, snapshot: tuple[list[str], list[tuple]]) -> None:
    """Put the exact snapshot back (DELETE + re-INSERT of every column),
    so the two passes run over byte-identical starting states."""
    names, rows = snapshot
    c.execute("DELETE FROM records")
    if rows:
        columns = ", ".join(names)
        marks = ", ".join(["%s"] * len(names))
        with c.cursor() as cur:
            cur.executemany(
                f"INSERT INTO records ({columns}) VALUES ({marks})", rows)
    c.commit()


def _versions(snapshot: tuple[list[str], list[tuple]]) -> list:
    """(file_key, line_num, cost_usd, long_context, pricing_version) of
    a snapshot — the columns the differential byte-compares."""
    names, rows = snapshot
    at = {name: index for index, name in enumerate(names)}
    return [(row[at["file_key"]], row[at["line_num"]], row[at["cost_usd"]],
             row[at["long_context"]], row[at["pricing_version"]])
            for row in rows]


def _fingerprint(snapshot: tuple[list[str], list[tuple]]) -> list:
    """(file_key, line_num, rate_fingerprint) of a snapshot."""
    names, rows = snapshot
    at = {name: index for index, name in enumerate(names)}
    return [(row[at["file_key"]], row[at["line_num"]],
             row[at["rate_fingerprint"]]) for row in rows]


def _install_shape_tables(monkeypatch, prov) -> None:
    """The synthetic shape tables, settled before any seeding: the
    provider row with its dated window and two-entry schedule, the
    permaslug row its dated ids fold to, and the sonnet boundary
    window."""
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {
        (prov.model, prov.host): prov.after,
        ("acme/acme-9-0731", prov.host): prov.after,
    })
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {
        (prov.model, prov.host): [(prov.cutover, prov.before)],
    })
    monkeypatch.setattr(pricing, "PROVIDER_STARTS", {
        (prov.model, prov.host): prov.start,
    })
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {
        (prov.model, prov.host): {
            0: [(None, 300, 600, _SCHED_A_RATES)],
            1: [(None, None, None, _SCHED_B_RATES)],
        },
    })
    monkeypatch.setattr(pricing, "DATED_RATES", {
        "claude-sonnet-4-5": [(_SONNET_END, _WINDOW_RATES)],
    })
    # The meter tables are the test's own (SV-TEST-DATA): exactly the
    # meter shape model is a member, so the sonnet boundary rows keep
    # their NULL flag under both passes whatever the live fold does.
    monkeypatch.setattr(pricing, "LONG_CONTEXT_MODELS",
                        frozenset({"gpt-5-6-sol"}))
    monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS", {})
    rate_fingerprint.clear_fingerprint_cache()


def _build_shape_rows(prov, inside_ts: datetime, outside_ts: datetime,
                      newer: str) -> list[dict]:
    """One persist-consistent row per shape: the (cost, flag,
    fingerprint) triple is exactly what persist (or the pass itself)
    stamps, so recomputation is the identity wherever the fingerprint
    still matches — the population the clean restamp claims."""
    def fp(model, provider=None):
        return rate_fingerprint.pair_fingerprint(model, provider)

    def cost(model, ts, provider=None, long_context=False):
        return round(pricing.compute_cost(
            model, **_TALLY, ts=ts, long_context=long_context,
            res=pricing.resolve(model, ts, provider)), 6)

    def row(model, ts, **kwargs):
        base = {"model": model, "ts": ts, "version": _STALE,
                "cost": cost(model, ts, kwargs.get("provider"),
                             bool(kwargs.get("flag"))),
                "fp": fp(model, kwargs.get("provider"))}
        base.update(kwargs)
        return base

    return [
        row("claude-opus-4-7", _SEED_TS),                       # exact key
        row("claude-sonnet-4-5",
            _SONNET_END - timedelta(seconds=1)),                 # boundary -
        row("claude-sonnet-4-5",
            _SONNET_END + timedelta(seconds=1)),                 # boundary +
        row(prov.model, inside_ts, provider=prov.host),         # sched in
        row(prov.model, outside_ts, provider=prov.host),        # sched out
        row("acme/acme-9-20260731", outside_ts,
            provider=prov.host),                                # permaslug
        row("acme/acme-9:nitro", inside_ts,
            provider=prov.host),                                # variant
        row("claude-opus-4-7", None),                           # NULL ts
        row("claude-opus-4-7",
            datetime(2026, 5, 7, 10)),                          # naive ts
        row("claude-opus-4-7", _SEED_TS,
            version=newer, cost=0.5),                           # guard
        dict(row("claude-opus-4-7", _SEED_TS), fp=None),        # NULL fp
        # A consistent TRUE flag needs a tally the derivation agrees is
        # above the threshold; FALSE and the NULL seed keep any tally
        # (the NULL seed's own derivation is FALSE — issue #765's
        # membership-keyed reprice).
        row(_METER_MODEL, _SEED_TS, fresh=300_000, flag=True,
            cost=round(pricing.compute_cost(
                _METER_MODEL, fresh=300_000, output=100, eph5=250,
                eph1h=500, unsplit_create=1250, read=3000, ts=_SEED_TS,
                long_context=True), 6),
            fp=fp(_METER_MODEL)),                               # meter TRUE
        row(_METER_MODEL, _SEED_TS, flag=False,
            cost=cost(_METER_MODEL, _SEED_TS),
            fp=fp(_METER_MODEL)),                               # meter FALSE
        # The NULL flag of a member row is a PRE-FOLD shape: under the
        # membership-keyed reprice (issue #765) a reparse stores the
        # decision, so a hand-consistent fingerprint can never pair with
        # it — persist stamps the derived flag beside the fingerprint.
        # Seeded with fp NULL (the conservative stale shape), so both
        # passes take the keyset path and derive FALSE.
        dict(row(_METER_MODEL, _SEED_TS, flag=None,
                 cost=cost(_METER_MODEL, _SEED_TS)), fp=None),   # #249 NULL
        row("gpt-9:free", _SEED_TS, cost=0.0,
            fp=fp("gpt-9:free")),                               # free
        row("claude-opus-99", _SEED_TS),                        # tier
        row("not-a-model-9", _SEED_TS),                         # default
    ]


def _move_opus_pair(monkeypatch) -> None:
    """Move pair opus AFTER seeding: its fingerprint and cost move,
    every other pair's stays (the sensitivity property the fingerprint
    tests pin)."""
    monkeypatch.setattr(pricing, "DATED_RATES", {
        **pricing.DATED_RATES,
        "claude-opus-4-7": [(datetime(2027, 6, 1, tzinfo=UTC),
                             _MOVED_WINDOW_RATES)]})
    rate_fingerprint.clear_fingerprint_cache()


def _assert_fingerprints_stamped(
        snapshot: tuple[list[str], list[tuple]], newer: str) -> None:
    """B and C must carry rate_fingerprint on every non-guard row."""
    names, rows = snapshot
    at = {name: index for index, name in enumerate(names)}
    version_at = at["pricing_version"]
    guards = {(row[0], row[1]) for row in rows
              if row[version_at] == newer}
    fingerprint_at = at["rate_fingerprint"]
    unstamped = [(row[0], row[1]) for row in rows
                 if (row[0], row[1]) not in guards
                 and row[fingerprint_at] is None]
    assert not unstamped, (
        f"every non-guard row must carry its fingerprint: {unstamped}")


def _assert_moved_pair_repriced(start, end) -> None:
    """The moved pair's rows must actually repriced, or the differential
    proves nothing."""
    def costs(snapshot):
        return {(row[0], row[1]): row[2] for row in _versions(snapshot)}

    before, after = costs(start), costs(end)
    moved = [key for key in after if before[key] != after[key]]
    assert moved, "the moved pair's rows must have repriced somewhere"


def test_legacy_and_pair_qualified_pass_agree(
        fresh_db, monkeypatch, synthetic_provider_dated_rate):
    """THE DIFFERENTIAL (issue #351): every seeded shape — scheduled
    provider pair (inside and outside the window), dated-window
    boundary (one second either side), NULL ts, naive ts, rollback
    guard, a changed pair and an unchanged pair, NULL-fp row,
    long_context NULL/FALSE/TRUE (the issue #249 Claude NULL shape
    included), free id, tier fallback, unmatchable default, permaslug
    and variant-suffix provider ids — ends at byte-identical
    (cost_usd, long_context, pricing_version) under the frozen
    pre-#351 pass and the pair-qualified pass, whether the new pass
    reaches a row through the SQL clean restamp or the keyset."""
    prov = synthetic_provider_dated_rate
    # hhmm 400: inside the schedule's 0300-0600 window AND the dated
    # window (start + 5 days sits before the cutover).
    inside_ts = prov.start.replace(hour=4) + timedelta(days=5)
    outside_ts = prov.cutover + timedelta(days=5)
    newer = str(int(constants.PRICING_VERSION) + 1)

    _install_shape_tables(monkeypatch, prov)
    rows = _build_shape_rows(prov, inside_ts, outside_ts, newer)
    with db.viz_conn() as c:
        _seed_parents(c, _FILE_KEY)
        _seed_shape_rows(c, rows)
        c.commit()

    _move_opus_pair(monkeypatch)

    with db.viz_conn() as c:
        start = _snapshot(c)
    _legacy_reprice_stale()
    with db.viz_conn() as c:
        legacy_snapshot = _snapshot(c)

    _restore(c, start)
    ingest.reprice_stale()
    with db.viz_conn() as c:
        new_snapshot = _snapshot(c)

    # Reset the staleness marker on exactly the rows the pass wrote, so
    # the third run is the pure clean-restamp path: every non-guard row
    # stale again, fingerprints and costs already current.
    with db.viz_conn() as c:
        c.execute(
            "UPDATE records SET pricing_version = %s "
            "WHERE pricing_version = %s",
            (_STALE, constants.PRICING_VERSION))
        c.commit()
    ingest.reprice_stale()
    with db.viz_conn() as c:
        clean_snapshot = _snapshot(c)

    assert len(_versions(legacy_snapshot)) == len(rows)
    assert _versions(legacy_snapshot) == _versions(new_snapshot), (
        "the pair-qualified pass must end every row exactly where the "
        "frozen pre-#351 pass does")
    assert _versions(new_snapshot) == _versions(clean_snapshot), (
        "the clean-restamp run must end where the keyset run did")
    _assert_fingerprints_stamped(new_snapshot, newer)
    _assert_fingerprints_stamped(clean_snapshot, newer)
    _assert_moved_pair_repriced(start, new_snapshot)
