"""The three load-bearing guards of the clean restamp (issue #391).

Each guard below can be deleted with the rest of the suite still green,
so each gets one test that fails under its deletion:

- the provider match in ``_SQL_CLEAN_RESTAMP``
  (``AND COALESCE(r.provider, '') = d.p``) — two rows of one model in
  DIFFERENT provider shapes whose stored fingerprint strings coincide
  must not cross-match each other's triples, or a row whose pair's
  rates moved is restamped at its old cost instead of repriced;

- the logic digest (``"logic": _logic()`` in
  ``rate_fingerprint._document``) — a change to the pricing modules'
  source must move every pair's fingerprint, or edited logic
  restamps clean instead of repricing;

- ``sorted(days)`` in ``rate_fingerprint._schedule`` — a schedule's
  weekday order must not reach the digest, or the same window spelled
  in two orders prices as two different schedules.

All rate data is synthetic (SV-TEST-DATA): the keys below match no
live row and no family pattern, the rates are unlike any real price,
and versions are derived from the committed constants at run time.
"""
from __future__ import annotations

from datetime import datetime, timezone

# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture,
)

from backend import constants, db, ingest_reprice, pricing, rate_fingerprint

UTC = timezone.utc

# Synthetic pair: no live model key folds onto 'zz-guard-model' (no
# MODEL_RATES key is its prefix, no tier-fallback pattern matches it),
# and no live provider row exists for the host.
_GUARD_MODEL = "zz-guard-model"
_GUARD_HOST = "xh"
_GUARD_PAIR = (_GUARD_MODEL, _GUARD_HOST)

# Rates deliberately unlike any real price, so an assertion against
# them can never be mistaken for a pricing fact (the conftest
# synthetic-rate fixtures' rule). The provider card is exactly 4x the
# model card, so the repriced cost is distinguishable from the seeded
# one by construction.
_MODEL_RATES = {"fresh": 1.5, "create_5m": 1.875, "create_1h": 3.0,
                "read": 0.15, "output": 7.5}
_PROVIDER_RATES = {"fresh": 6.0, "create_5m": 7.5, "create_1h": 12.0,
                   "read": 0.6, "output": 30.0}

# One seeded record's token tally: fresh, cache_creation, cache_read,
# output, eph5, eph1h — unsplit_create = 2000 - 250 - 500, so the
# pass's unsplit arithmetic is inside every hand cost below.
_SEED_TS = datetime(2026, 5, 7, 10, 0, tzinfo=UTC)
_TOKENS = (1_000, 2_000, 3_000, 100, 250, 500)
_FILE_KEY = "claude/reprice-guard-test/sess-g/sess-g.jsonl"
_SEED_VERSION = "0"


def _hand_cost(rates: dict) -> float:
    """The seeded tally's cost by transparent arithmetic over `rates` —
    the SV-COST-SPLIT shape (unsplit creation at the 1h rate), rounded
    into NUMERIC(12,6) exactly as the pass rounds its writes."""
    fresh, create, read, output, eph5, eph1h = _TOKENS
    unsplit = create - eph5 - eph1h
    input_cost = (
        fresh * rates["fresh"]
        + eph5 * rates["create_5m"]
        + (eph1h + unsplit) * rates["create_1h"]
        + read * rates["read"]
    ) / 1_000_000
    return round(input_cost + output * rates["output"] / 1_000_000, 6)


def _seed_guard_row(c, line_num: int, *, provider: str | None,
                    fingerprint: str, cost: float) -> None:
    """One stale record row of the guard pair, version '0', under the
    seeded project+file parents (the test_reprice.py seeder's shape)."""
    c.execute(
        "INSERT INTO projects (project_id, display_name, first_seen_at, "
        "last_seen_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (project_id) DO NOTHING",
        ("reprice-guard-test", "reprice-guard-test", _SEED_TS, _SEED_TS),
    )
    c.execute(
        "INSERT INTO files (file_key, project_id, session_id, is_main, "
        "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
        "parser_version) VALUES (%s, %s, %s, TRUE, %s, %s, %s, %s, %s) "
        "ON CONFLICT (file_key) DO NOTHING",
        (_FILE_KEY, "reprice-guard-test", "sess-guard", "seed-etag", 12,
         _SEED_TS, _SEED_TS, constants.PARSER_VERSION),
    )
    fresh, create, read, output, eph5, eph1h = _TOKENS
    c.execute(
        "INSERT INTO records (file_key, line_num, ts, model, fresh_tokens, "
        "cache_creation_tokens, cache_read_tokens, output_tokens, "
        "eph5_tokens, eph1h_tokens, cost_usd, provider, pricing_version, "
        "rate_fingerprint) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (_FILE_KEY, line_num, _SEED_TS, _GUARD_MODEL, fresh, create, read,
         output, eph5, eph1h, cost, provider, _SEED_VERSION, fingerprint),
    )


def _guard_rows(c) -> dict[int, tuple]:
    """The two seeded rows as {line_num: (provider, cost, version, fp)}."""
    rows = c.execute(
        "SELECT line_num, provider, cost_usd, pricing_version, "
        "rate_fingerprint FROM records WHERE file_key = %s "
        "ORDER BY line_num", (_FILE_KEY,)).fetchall()
    assert len(rows) == 2, "both seeded rows must survive the pass"
    return {row[0]: row[1:] for row in rows}


def test_clean_restamp_keeps_the_provider_shapes_apart(fresh_db, monkeypatch):
    """The provider match in _SQL_CLEAN_RESTAMP (issue #391, T1).

    Row X (provider 'xh') and row Y (provider NULL) of one model carry
    the SAME stored fingerprint string — the cross-shape collision the
    provider term of the join exists to disambiguate (free ids really
    produce it: their documents name no table data, so every provider
    shape of a free id fingerprints alike; for a non-free model the
    collision is constructed here because the test pins the predicate,
    not the accident). A provider row is then added for (M, 'xh') ONLY,
    moving THAT shape's current fingerprint: X is no longer provable
    clean and must be repriced at the provider card, while Y — whose
    pair's inputs the provider row cannot touch — restamps at its old
    cost. Delete the provider match and X cross-matches Y's triple on
    model+fingerprint, is restamped at the OLD cost, and never reaches
    the recompute path: the X cost assertion below fails.
    """
    monkeypatch.setattr(
        pricing, "MODEL_RATES",
        {**pricing.MODEL_RATES, _GUARD_MODEL: dict(_MODEL_RATES)})
    seed_fp = rate_fingerprint.pair_fingerprint(_GUARD_MODEL, None)
    seed_cost = _hand_cost(_MODEL_RATES)
    assert seed_cost != _hand_cost(_PROVIDER_RATES), (
        "the provider card must price the tally differently, or the "
        "repriced-vs-restamped distinction below proves nothing")
    with db.viz_conn() as c:
        _seed_guard_row(c, 1, provider=_GUARD_HOST, fingerprint=seed_fp,
                        cost=seed_cost)
        _seed_guard_row(c, 2, provider=None, fingerprint=seed_fp,
                        cost=seed_cost)
        c.commit()

    # The move: a provider row for the 'xh' shape ONLY, dict-additive
    # with whole-row replacement (no other key's content is touched, so
    # the perturbed-data leg's appended entries stay in place).
    monkeypatch.setattr(
        pricing, "PROVIDER_RATES",
        {**pricing.PROVIDER_RATES, _GUARD_PAIR: dict(_PROVIDER_RATES)})
    rate_fingerprint.clear_fingerprint_cache()
    monkeypatch.setattr(
        constants, "PRICING_VERSION",
        str(int(constants.PRICING_VERSION) + 1))

    changed = ingest_reprice.reprice_stale()

    with db.viz_conn() as c:
        rows = _guard_rows(c)
    x_provider, x_cost, x_version, x_fp = rows[1]
    y_provider, y_cost, y_version, y_fp = rows[2]
    assert x_provider == _GUARD_HOST, "row X is the provider-shape row"
    assert float(x_cost) == _hand_cost(_PROVIDER_RATES), (
        "row X must be REPRICED at the provider card, not restamped at "
        "its old cost")
    assert x_fp == rate_fingerprint.pair_fingerprint(
        _GUARD_MODEL, _GUARD_HOST), "row X carries its pair's current fp"
    assert y_provider is None
    assert float(y_cost) == seed_cost, (
        "row Y's pair's inputs did not move: restamped only, cost kept")
    assert y_fp == seed_fp, "row Y's fingerprint is unchanged"
    assert x_version == constants.PRICING_VERSION
    assert y_version == constants.PRICING_VERSION
    assert changed == 1, "only row X's rate-derived data changed"


def test_logic_digest_moves_the_fingerprint(monkeypatch):
    """The logic digest in rate_fingerprint._document (issue #391, T2).

    The fingerprint covers the pricing modules' AND the reprice pass's
    source, so a rule change shipped with a PRICING_VERSION bump moves
    every pair's fp and reprices instead of restamping clean. The
    digest is memoized in _LOGIC_CACHE; the test swaps the memo for a
    different digest string — the exact seam an edit flows through —
    and requires the pair's fingerprint to move with it. Delete the
    ``"logic"`` entry from _document and the two fingerprints are
    equal: the assertion below fails.
    """
    fp_before = rate_fingerprint.pair_fingerprint(_GUARD_MODEL, None)
    # pylint: disable-next=protected-access
    monkeypatch.setattr(rate_fingerprint, "_LOGIC_CACHE", ["changed-digest"])
    rate_fingerprint.clear_fingerprint_cache()
    fp_after = rate_fingerprint.pair_fingerprint(_GUARD_MODEL, None)
    assert fp_after != fp_before, (
        "a changed logic digest must move the pair's fingerprint")
    rate_fingerprint.clear_fingerprint_cache()


def test_schedule_weekday_order_is_normalised(monkeypatch):
    """sorted(days) in rate_fingerprint._schedule (issue #391, T3).

    A schedule window's days are a SET of weekday names; the digest
    must see them in one canonical order, so the same window spelled
    ['monday', 'tuesday'] and ['tuesday', 'monday'] fingerprints alike
    — that order-insensitivity is what sorted() buys — while a
    different day set must still fingerprint differently. The window
    rides the TAIL index (len(windows), here 0: the pair has no dated
    window), the entry resolve() consults once every dated window has
    ended (issue #377). Drop the sorted() and the two orders serialize
    differently: the equality assertion below fails.
    """
    monkeypatch.setattr(
        pricing, "PROVIDER_RATES",
        {**pricing.PROVIDER_RATES, _GUARD_PAIR: dict(_PROVIDER_RATES)})

    def set_schedule(days: list[str]) -> None:
        monkeypatch.setattr(
            pricing, "PROVIDER_SCHEDULES",
            {**pricing.PROVIDER_SCHEDULES,
             _GUARD_PAIR: {0: [(days, 1320, 120, dict(_PROVIDER_RATES))]}})
        rate_fingerprint.clear_fingerprint_cache()

    set_schedule(["monday", "tuesday"])
    fp_ordered = rate_fingerprint.pair_fingerprint(_GUARD_MODEL, _GUARD_HOST)
    set_schedule(["tuesday", "monday"])
    fp_swapped = rate_fingerprint.pair_fingerprint(_GUARD_MODEL, _GUARD_HOST)
    assert fp_swapped == fp_ordered, (
        "the same window's weekday order must not reach the digest")
    set_schedule(["monday"])
    fp_single = rate_fingerprint.pair_fingerprint(_GUARD_MODEL, _GUARD_HOST)
    assert fp_single != fp_ordered, (
        "a different day set must still move the fingerprint")
    rate_fingerprint.clear_fingerprint_cache()
