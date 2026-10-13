"""The records.uuid split columns (issue #959 task 8).

`records.uuid` stores one identity in exactly one of three slots —
``uuid_col`` (native uuid), ``seq`` (the INT4 counter beside it) or
``fallback`` (everything else) — and every read rebuilds the original
text with ``uuid_split.uuid_text_sql``, so canonical grouping, winner
parity and API output stay byte-identical to the old TEXT column.

The classification is total and deterministic: one input text maps to
one slot, and ``reconstruct(split_uuid(v)) == v`` for every shape the
corpus carries. The DB-side CHECKs enforce the shape of a stored row
(store coherence) and the disjointness the partition-by-parts scheme
relies on (a fallback never holds a form another slot owns).

The coexistence test is the pin for the partition scheme: a pure ``U``
row and a ``U:5`` split row share ``uuid_col`` but are DIFFERENT
identities — partitioning by ``uuid_col`` alone would merge them and
flip a winner.
"""
from __future__ import annotations

import functools
import uuid as uuid_mod
from datetime import datetime, timezone

import psycopg
import pytest

from backend import db, ingest_rollup_state, parser_version, uuid_split
from tests import dims_seed, scratch_db

# The canonical lowercase 8-4-4-4-12 form: exactly what PostgreSQL
# renders for uuid::text, so it round-trips byte-identically.
CANON = "018f0000-0000-7000-8000-000000000001"
# Verbatim corpus samples from task 2's full-corpus gate report.
CORPUS_SPLIT = (
    "01a0e87e-008f-7281-89ed-41cf8c56f117:32380",
    "01a0e87e-008f-7281-89ed-41cf8c56f117:1018385",
    "019fe6e8-705e-7841-8aa0-68428e291164:16668",
    "019fe6e8-705e-7841-8aa0-68428e291164:262973",
    "01a1226a-b198-78b1-80cc-dfd7e32a3673:24477",
    "01a1226a-b198-78b1-80cc-dfd7e32a3673:1054260",
)

# Every shape, with the slot each must land in.
PURE = (CANON, "uuid")
NONCANONICAL_CASE = (CANON.upper(), "fallback")
NO_HYPHENS = (CANON.replace("-", ""), "fallback")
BRACED = ("{" + CANON + "}", "fallback")
SPLIT = (f"{CANON}:32380", "split")
ZERO_COUNTER = (f"{CANON}:0", "split")
INT4_MAX = (f"{CANON}:2147483647", "split")
LEADING_ZERO = (f"{CANON}:007", "fallback")
INT4_OVERFLOW = (f"{CANON}:2147483648", "fallback")
KIMI_PATH_LINE = ("project/sess/kimi-0001.jsonl:17", "fallback")
LEGACY_MESSAGE_ID = ("legacy-msg-42", "fallback")
UPPERCASE_PREFIX_SPLIT = (f"{CANON.upper()}:5", "fallback")
# Python's `$` matches BEFORE a trailing newline; `\Z` does not. Both
# trailing-newline forms must be fallback: `U:5\n` would otherwise
# reconstruct without its newline (byte identity broken, and it merges
# with a genuine `U:5` in the winner rule), and `U\n` would reach
# ingest_persist as a "uuid" whose uuid.UUID() parse then crashes the
# file's persist — the old TEXT column stored both fine.
TRAILING_NEWLINE_SPLIT = (f"{CANON}:5\n", "fallback")
TRAILING_NEWLINE_PURE = (f"{CANON}\n", "fallback")
EMPTY = ("", "fallback")

ALL_SHAPES = (
    PURE, NONCANONICAL_CASE, NO_HYPHENS, BRACED, SPLIT, ZERO_COUNTER,
    INT4_MAX, LEADING_ZERO, INT4_OVERFLOW, KIMI_PATH_LINE,
    LEGACY_MESSAGE_ID, UPPERCASE_PREFIX_SPLIT, TRAILING_NEWLINE_SPLIT,
    TRAILING_NEWLINE_PURE, EMPTY,
) + tuple((value, "split") for value in CORPUS_SPLIT)


def _slot(parts: uuid_split.UuidParts) -> str:
    if parts.fallback is not None:
        return "fallback"
    if parts.uuid_col is None:
        return "null"
    return "split" if parts.seq is not None else "uuid"


@pytest.mark.parametrize("value,slot", ALL_SHAPES,
                         ids=[value or "<empty>"
                              for value, _slot_ in ALL_SHAPES])
def test_every_shape_lands_in_exactly_one_slot(value, slot):
    """Classification: one text, one slot, verbatim where it falls back."""
    parts = uuid_split.split_uuid(value)
    assert _slot(parts) == slot
    # One slot means the other two are NULL — never two stores at once.
    if slot == "uuid":
        assert parts == uuid_split.UuidParts(value, None, None)
    elif slot == "split":
        head, _, counter = value.rpartition(":")
        assert parts.uuid_col == head
        assert parts.seq == int(counter)
        assert parts.fallback is None
    elif slot == "fallback":
        assert parts == uuid_split.UuidParts(None, None, value)


def test_a_null_identity_stores_nothing_at_all():
    """NULL uuid (legacy/absent) → all three slots NULL, never ''."""
    assert uuid_split.split_uuid(None) == (None, None, None)


@pytest.mark.parametrize("value,_slot", ALL_SHAPES,
                         ids=[value or "<empty>"
                              for value, _slot_ in ALL_SHAPES])
def test_every_shape_reconstructs_byte_identically(value, _slot):
    assert uuid_split.reconstruct(uuid_split.split_uuid(value)) == value


def test_null_reconstructs_to_null():
    assert uuid_split.reconstruct(uuid_split.split_uuid(None)) is None


def test_the_split_rule_is_one_deterministic_function():
    """The same input always maps to the same slot: the disjointness the
    partition-by-parts scheme rests on starts with determinism."""
    first = uuid_split.split_uuid(CORPUS_SPLIT[0])
    second = uuid_split.split_uuid(CORPUS_SPLIT[0])
    assert first == second


# --------------------------------------------------------------------------
# The applied schema: one slot per row, byte-identical rebuild.
# --------------------------------------------------------------------------

def _seed(conn):
    """A project, two files and a model, so records rows can exist."""
    conn.execute(
        "INSERT INTO projects (project_id, display_name, first_seen_at, "
        "last_seen_at) VALUES ('p', 'p', now(), now())")
    now = datetime.now(timezone.utc)
    for key in ("bucket/p/a.jsonl", "bucket/p/b.jsonl"):
        conn.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, "
            "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
            "parser_version) VALUES (%s, 'p', 's', TRUE, 'e', 1, %s, %s, "
            "'0')", (key, now, now))
    files = dict(conn.execute(
        "SELECT file_key, file_id FROM files").fetchall())
    model = dims_seed.dim_id(conn, "dim_model", "model-x")
    return files, model, now


def _insert(conn, file_id, line_num, parts, model, now):
    conn.execute(
        "INSERT INTO records (file_id, line_num, uuid_col, seq, fallback, "
        "model_id, ts) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (file_id, line_num,
         uuid_mod.UUID(parts.uuid_col) if parts.uuid_col else None,
         parts.seq, parts.fallback, model, now))


def _assert_row(row, value: str, slot: str) -> None:
    """One stored row: the SQL rebuild matches the input byte-identically,
    the Python mirror agrees with the SQL (no drift), and only the
    classified slot is populated."""
    line, uuid_col, seq, fallback, text = row
    assert text == value, (line, slot)
    assert text == uuid_split.reconstruct(
        uuid_split.UuidParts(str(uuid_col) if uuid_col else None,
                             seq, fallback))
    if slot == "uuid":
        assert uuid_col is not None and seq is None and fallback is None
    elif slot == "split":
        assert uuid_col is not None and seq is not None
        assert fallback is None
    else:
        assert uuid_col is None and seq is None
        assert fallback == value


@pytest.mark.db
def test_every_shape_stores_in_one_slot_and_reads_back_byte_identical():
    """The SQL rebuild the readers use returns the original text, and only
    the classified slot is populated — asserted against the live column."""
    name = scratch_db.create_database("uksplit_rt")
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            files, model, now = _seed(conn)
            for line, (value, _slot) in enumerate(ALL_SHAPES, start=1):
                _insert(conn, files["bucket/p/a.jsonl"], line,
                        uuid_split.split_uuid(value), model, now)
            rows = conn.execute(
                f"SELECT line_num, uuid_col, seq, fallback, "
                f"{uuid_split.uuid_text_sql('r')} AS text "
                f"FROM records r ORDER BY line_num").fetchall()
            conn.commit()
        assert len(rows) == len(ALL_SHAPES)
        for (value, slot), row in zip(ALL_SHAPES, rows):
            _assert_row(row, value, slot)
    finally:
        scratch_db.drop_database(name)


@pytest.mark.db
def test_a_null_identity_stays_null_through_the_rebuild():
    name = scratch_db.create_database("uksplit_null")
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            files, model, now = _seed(conn)
            _insert(conn, files["bucket/p/a.jsonl"], 1,
                    uuid_split.split_uuid(None), model, now)
            row = conn.execute(
                f"SELECT {uuid_split.uuid_text_sql('r')}, "
                f"{uuid_split.uuid_present_sql('r')} "
                f"FROM records r").fetchone()
            conn.commit()
        # NULL uuid still means always-canonical in the winner rule: the
        # rebuild is NULL and the row does NOT read as uuid-bearing.
        assert row == (None, False)
    finally:
        scratch_db.drop_database(name)


def _refused(conn, insert) -> None:
    """Run an INSERT the schema must reject, without poisoning the
    transaction: a CHECK violation aborts the whole transaction
    otherwise, so each probe gets its own savepoint."""
    conn.execute("SAVEPOINT slot_probe")
    with pytest.raises(psycopg.errors.CheckViolation):
        insert()
    conn.execute("ROLLBACK TO SAVEPOINT slot_probe")


@pytest.mark.db
def test_the_stores_are_mutually_exclusive():
    """CHECK: fallback is exclusive, and seq needs its uuid_col."""
    name = scratch_db.create_database("uksplit_chk")
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            files, model, now = _seed(conn)
            file_id = files["bucket/p/a.jsonl"]
            # uuid_col + fallback together: refused.
            _refused(conn, functools.partial(
                _insert, conn, file_id, 1,
                uuid_split.UuidParts(CANON, None, "some-fallback"),
                model, now))
            # seq without uuid_col: refused.
            _refused(conn, functools.partial(
                conn.execute,
                "INSERT INTO records (file_id, line_num, seq, model_id, "
                "ts) VALUES (%s, %s, %s, %s, %s)",
                (file_id, 2, 5, model, now)))
            conn.rollback()
    finally:
        scratch_db.drop_database(name)


@pytest.mark.db
def test_the_fallback_never_holds_a_form_another_slot_owns():
    """CHECK: the three stores' TEXT sets are pairwise disjoint — a
    fallback cannot be a pure uuid or a valid uuid:seq, so partitioning
    by the parts cannot merge two different identity texts."""
    name = scratch_db.create_database("uksplit_dis")
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            files, model, now = _seed(conn)
            file_id = files["bucket/p/a.jsonl"]
            # A canonical uuid belongs to uuid_col.
            _refused(conn, functools.partial(
                _insert, conn, file_id, 1,
                uuid_split.UuidParts(None, None, CANON), model, now))
            # A valid uuid:seq belongs to uuid_col + seq.
            _refused(conn, functools.partial(
                _insert, conn, file_id, 2,
                uuid_split.UuidParts(None, None, f"{CANON}:7"),
                model, now))
            # But the forms that CLASSIFY as fallback must still be
            # storable there: leading zeros, an INT4-overflow counter and
            # the trailing-newline shapes (the SQL mirror of the pattern
            # lives in this CHECK — PG's `$` does not match before a
            # trailing newline, so `"U:5\n"` must NOT hit the numeric
            # cast branch; if it ever did, the cast would raise and this
            # insert would fail rather than store).
            for line, value in ((1, f"{CANON}:007"),
                                (2, f"{CANON}:2147483648"),
                                (3, f"{CANON.upper()}:5"),
                                (4, f"{CANON}\n"),
                                (5, f"{CANON}:5\n")):
                _insert(conn, file_id, line,
                        uuid_split.split_uuid(value), model, now)
            texts = [row[0] for row in conn.execute(
                f"SELECT {uuid_split.uuid_text_sql('r')} FROM records r "
                "ORDER BY line_num").fetchall()]
            conn.commit()
        assert texts == [f"{CANON}:007", f"{CANON}:2147483648",
                         f"{CANON.upper()}:5", f"{CANON}\n",
                         f"{CANON}:5\n"]
    finally:
        scratch_db.drop_database(name)


@pytest.mark.db
def test_pure_and_split_rows_of_one_uuid_keep_separate_winners(monkeypatch):
    """Coexistence pin: ``U`` and ``U:5`` share uuid_col but are DIFFERENT
    identities — the old TEXT rule gave each its own winner, and so must
    the split. A partition keyed on uuid_col alone would merge them and
    flip one row's flag; this test fails on that scheme."""
    name = scratch_db.create_database("uksplit_coex")
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
    db.reset_viz_pool()
    try:
        with db.viz_conn() as conn:
            files, model, now = _seed(conn)
            # Two identities in one file plus a second file's copy of the
            # split identity, so a merged partition would have to pick one
            # winner across all three.
            _insert(conn, files["bucket/p/a.jsonl"], 1,
                    uuid_split.split_uuid(CANON), model, now)
            _insert(conn, files["bucket/p/a.jsonl"], 2,
                    uuid_split.split_uuid(f"{CANON}:5"), model, now)
            _insert(conn, files["bucket/p/b.jsonl"], 1,
                    uuid_split.split_uuid(f"{CANON}:5"), model, now)
            conn.commit()

        ingest_rollup_state.recompute_canonical()

        with db.viz_conn() as conn:
            rows = conn.execute(
                f"SELECT line_num, {uuid_split.uuid_text_sql('r')} AS text, "
                "is_canonical FROM records r ORDER BY file_id, line_num"
            ).fetchall()
        winners = {text for _line, text, canon in rows if canon}
        # The pure identity has exactly its one row as winner...
        assert winners == {CANON, f"{CANON}:5"}, rows
        # ...and the split identity still picks ONE of its two copies.
        split_rows = [row for row in rows if row[1] == f"{CANON}:5"]
        assert sum(1 for row in split_rows if row[2]) == 1, rows
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)


@pytest.mark.db
def test_the_uuid_index_is_gone_with_its_readers_named():
    """records_uuid_idx is dropped (#958-class: measured 140 MB on the
    full corpus). Its only readers were the two hourly equality probes in
    ingest_rollup_state's incremental branches (recompute_canonical's
    ``uuid = ANY`` winners scan and adopt_original_models' join) — 4 ms
    CPU with the index vs ~86 ms without, while adopt's winners CTE
    already seq-scans the whole table every incremental run anyway, and a
    window PARTITION BY never consults a b-tree index at all."""
    name = scratch_db.create_database("uksplit_idx")
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            names = {row[0] for row in conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'records'").fetchall()}
            conn.commit()
        assert "records_uuid_idx" not in names, names
    finally:
        scratch_db.drop_database(name)


@pytest.mark.db
def test_a_text_uuid_database_drops_to_the_split_and_forces_a_refill(
        monkeypatch):
    """Migration: old TEXT-uuid rows converge to the split columns.

    The shape guard in schema_widths.sql keys on the live column
    signature; a `uuid:text` records matches neither canonical split
    signature, so it is dropped (facts are derived — R1) and rebuilt by
    schema_dims.sql. Dropping alone cannot refill it:
    files.parser_version would still match ingest's stored version, so
    task 8 carries PARSER_VERSION 110 — the seeded '109' below is the
    branch's pre-split stamp, and the assertion is that it no longer
    matches: the bump is what forces the reparse that restores the
    dropped facts.
    """
    name = scratch_db.create_database("uksplit_mig",
                                      schema=scratch_db.OLD_SCHEMA)
    now = datetime.now(timezone.utc)
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            before = conn.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'records' "
                "AND column_name = 'uuid'").fetchone()
            conn.execute(
                "INSERT INTO projects (project_id, display_name, "
                "first_seen_at, last_seen_at) VALUES ('p', 'p', %s, %s)",
                (now, now))
            conn.execute(
                "INSERT INTO files (file_key, project_id, session_id, "
                "is_main, r2_etag, r2_size_bytes, r2_last_modified, "
                "parsed_at, parser_version) VALUES ('k/p/s/m.jsonl', 'p', "
                "'s', TRUE, 'e', 1, %s, %s, '109')", (now, now))
            conn.execute(
                "INSERT INTO records (file_key, line_num, uuid, ts, model) "
                "VALUES ('k/p/s/m.jsonl', 1, %s, %s, 'm')", (CANON, now))
            conn.commit()
        assert before is not None and before[0] == "text", before

        monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
        db.reset_viz_pool()
        db.apply_schema()

        with psycopg.connect(f"postgresql:///{name}") as conn:
            columns = dict(conn.execute(
                "SELECT column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'records'"
            ).fetchall())
            rows = conn.execute("SELECT count(*) FROM records").fetchone()
            stamped = conn.execute(
                "SELECT parser_version FROM files "
                "WHERE file_key = 'k/p/s/m.jsonl'").fetchone()
            conn.commit()
        assert columns.get("uuid") is None, columns
        assert columns.get("uuid_col") == "uuid", columns
        assert columns.get("seq") == "integer", columns
        assert columns.get("fallback") == "text", columns
        # The TEXT-uuid facts are gone with the dropped table — nothing
        # refills them until ingest reparses, which the version
        # mismatch below is what triggers.
        assert rows is not None and rows[0] == 0, rows
        assert stamped is not None, stamped
        assert stamped[0] != parser_version.PARSER_VERSION, (
            stamped[0], parser_version.PARSER_VERSION)
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)
