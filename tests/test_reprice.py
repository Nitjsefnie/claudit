"""records.pricing_version — the stored half of the reprice switch.

Issue #193: a rate change must rePRICE stored records (recompute
cost_usd from the stored token columns) instead of re-parsing every R2
object. The reprice pass selects rows whose stored `pricing_version`
differs from constants.PRICING_VERSION, so persist-time stamping is the
groundwork: every INSERT names the version its row was priced under,
and a reparse naturally re-stamps it.

NULL keeps its meaning for rows written before this machinery: priced
by a build older than the reprice switch, therefore stale. The column
is additive and nullable (SV-SCHEMA-AUTOAPPLY), so the next startup's
apply_schema() adds it without disturbing anything.
"""
from __future__ import annotations

# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture,
    _mini_r2_env_fixture,
)

from backend import constants, db, ingest


def _pair(c, sql: str, params=None) -> tuple[int, int]:
    """The one row's two columns; the query must yield one (like
    test_ingest._scalar, for a two-column aggregate)."""
    row = (c.execute(sql, params) if params is not None
           else c.execute(sql)).fetchone()
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
