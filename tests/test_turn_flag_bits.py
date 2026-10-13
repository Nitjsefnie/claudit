"""Flag name <-> INT4 bit for records.turn_flags (issue #959, ruling R5).

The vocabulary stays ``backend/turn_flags.py`` (the FLAG_* constants);
``backend/turn_flag_bits.py`` owns the bit ASSIGNMENT and is the only
place a flag name becomes a bit. These tests pin the lockstep between
the two, the round trip in both directions over EVERY flag, and the
persist path that stores the mask instead of a TEXT[] (the parse side
keeps emitting the sorted name list — fixtures pin that shape).
"""
from __future__ import annotations

import psycopg
import pytest

from backend import ingest_persist, turn_flag_bits, turn_flags
from tests import scratch_db


def _vocabulary() -> set[str]:
    """Every FLAG_* constant turn_flags.py declares (the source of truth)."""
    return {value for name, value in vars(turn_flags).items()
            if name.startswith("FLAG_") and isinstance(value, str)}


def test_the_bit_table_covers_the_vocabulary_exactly():
    assert set(turn_flag_bits.FLAG_BITS) == _vocabulary()


def test_bits_are_distinct_powers_of_two_inside_a_signed_int4():
    bits = list(turn_flag_bits.FLAG_BITS.values())
    assert len(set(bits)) == len(bits)
    assert all(bit > 0 and bit & (bit - 1) == 0 for bit in bits)
    assert max(bits) < (1 << 31)
    assert len(bits) <= turn_flag_bits.MAX_FLAGS


def test_round_trip_every_flag_on_its_own():
    for flag in turn_flag_bits.FLAG_BITS:
        mask = turn_flag_bits.flags_to_mask([flag])
        assert turn_flag_bits.mask_to_flags(mask) == [flag]


def test_round_trip_the_whole_vocabulary_and_the_empty_set():
    flags = list(turn_flag_bits.FLAG_BITS)
    assert turn_flag_bits.mask_to_flags(
        turn_flag_bits.flags_to_mask(flags)) == flags
    assert turn_flag_bits.flags_to_mask([]) == 0
    assert turn_flag_bits.flags_to_mask(None) == 0
    assert turn_flag_bits.mask_to_flags(0) == []


def test_a_repeated_name_folds_into_one_bit():
    assert turn_flag_bits.mask_to_flags(
        turn_flag_bits.flags_to_mask(["compact", "compact"])) == ["compact"]


def test_an_unknown_flag_name_is_refused_loudly():
    """A vocabulary typo must not silently drop out of the mask."""
    with pytest.raises(ValueError, match="not_a_real_flag"):
        turn_flag_bits.flags_to_mask(["not_a_real_flag"])


def test_a_bit_with_no_flag_is_refused_loudly():
    """A mask from a newer build (or corruption) must not decode quietly."""
    full = turn_flag_bits.flags_to_mask(list(turn_flag_bits.FLAG_BITS))
    with pytest.raises(ValueError):
        turn_flag_bits.mask_to_flags(full + 1)


def _record_with_flags(flags: list[str]) -> dict:
    """One records row's parse shape, as ingest_persist._write_facts sees it."""
    return {
        "line_num": 1,
        "uuid": "u-1",
        "request_id": "",
        "ts": None,
        "model": "claude-sonnet-5",
        "stop_reason": None,
        "effort": None,
        "provider": None,
        "fresh_tokens": 1,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 1,
        "eph5_tokens": 0,
        "eph1h_tokens": 0,
        "cost_usd": 0,
        "text_chars": 0,
        "reply_latency_s": None,
        "thinking_tokens": 0,
        "cli_version": None,
        "turn_flags": flags,
        "turn_tool_results": 0,
        "web_search_requests": None,
    }


def _seed_file(conn) -> int:
    """projects + files row; returns the file_id the facts FK needs."""
    conn.execute(
        "INSERT INTO projects (project_id, display_name, first_seen_at, "
        "last_seen_at) VALUES ('projT', 'projT', now(), now())")
    row = conn.execute(
        "INSERT INTO files (file_key, project_id, session_id, is_main, "
        "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
        "parser_version) VALUES ('bucket/projT/sessT/main.jsonl', 'projT', "
        "'sessT', TRUE, 'etag', 1, now(), now(), '0') "
        "RETURNING file_id").fetchone()
    assert row is not None
    return row[0]


@pytest.mark.db
def test_persist_stores_the_mask_and_the_column_is_int4():
    """Round trip: parser's name list -> INT4 mask -> every flag back."""
    name = scratch_db.create_database("turn_flag_bits")
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            dtype = conn.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'records' "
                "AND column_name = 'turn_flags'").fetchone()
            assert dtype is not None and dtype[0] == "integer", dtype

            file_id = _seed_file(conn)
            flags = list(turn_flag_bits.FLAG_BITS)
            parsed = {"records": [_record_with_flags(flags)], "tool_uses": []}
            with conn.cursor() as cur:
                ingest_persist._resolve_dims(cur, parsed)  # pylint: disable=protected-access
                ingest_persist._write_facts(cur, parsed, file_id)  # pylint: disable=protected-access
            conn.commit()

            stored = conn.execute(
                "SELECT turn_flags FROM records").fetchone()
            assert stored is not None
            assert isinstance(stored[0], int), type(stored[0])
            assert turn_flag_bits.mask_to_flags(stored[0]) == flags
    finally:
        scratch_db.drop_database(name)


@pytest.mark.db
def test_persist_refuses_a_measure_that_cannot_fit_int4():
    """The overflow guard fails loudly BEFORE the INSERT (no silent wrap)."""
    name = scratch_db.create_database("tf_overflow")
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            file_id = _seed_file(conn)
            too_big = _record_with_flags([])
            too_big["fresh_tokens"] = 1 << 31
            parsed = {"records": [too_big], "tool_uses": []}
            with conn.cursor() as cur:
                with pytest.raises(ValueError, match="fresh_tokens"):
                    ingest_persist._write_facts(  # pylint: disable=protected-access
                        cur, parsed, file_id)
            conn.rollback()
    finally:
        scratch_db.drop_database(name)


@pytest.mark.db
def test_persist_refuses_tool_churn_that_cannot_fit_int4():
    """Same guard on tool_uses' per-call churn columns."""
    name = scratch_db.create_database("tf_overflow_tool")
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            file_id = _seed_file(conn)
            tool = {
                "line_num": 1,
                "idx": 0,
                "ts": None,
                "tool_name": "Edit",
                "tool_use_id": "t1",
                "model": None,
                "is_error": None,
                "error_kind": None,
                "error_text": None,
                "lines_added": 1 << 31,
                "lines_deleted": 0,
                "agent_type": None,
                "agent_model": None,
                "dispatch_prompt_chars": None,
                "dispatch_brief_ref": None,
                "dispatch_name": None,
                "result_chars": 0,
                "read_kind": None,
                "read_targets": None,
                "write_targets": None,
                "is_reread": None,
            }
            parsed = {"records": [], "tool_uses": [tool]}
            with conn.cursor() as cur:
                with pytest.raises(ValueError, match="lines_added"):
                    ingest_persist._write_facts(  # pylint: disable=protected-access
                        cur, parsed, file_id)
            conn.rollback()
    finally:
        scratch_db.drop_database(name)
