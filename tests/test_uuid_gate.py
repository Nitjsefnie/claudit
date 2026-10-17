"""The records.uuid type gate (ruling R6 → task 8's split).

``gate()`` is the decision rule: convert the column to the ``uuid`` type
ONLY when every stored value casts; one refusal keeps the values out of
a bare uuid column and is reported verbatim — a refused value is NEVER
coerced to NULL (NULL means always-canonical in the winner rule, a
silent dedup break). The corpus scan (scripts/uuid_gate.py main) runs
the same gate over the full-corpus mirror with PostgreSQL's own cast as
the predicate; these tests pin both branches with an injected predicate.

The failing values the scan found are exactly what task 8's SPLIT
stores instead of refusing: the native uuid part in ``uuid_col``, the
``<uuid>:<n>`` counter in ``seq``, everything else verbatim in
``fallback`` (backend/uuid_split.py).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import psycopg
import pytest

from backend import ingest_persist
from tests import scratch_db

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_gate():
    """scripts/ is not a package; import the gate module by path."""
    path = REPO_ROOT / "scripts" / "uuid_gate.py"
    spec = importlib.util.spec_from_file_location("uuid_gate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


uuid_gate = _load_gate()


def test_every_value_casts_so_the_column_converts():
    result = uuid_gate.gate(
        ["018f0000-0000-7000-8000-000000000001", "u-2"],
        cast=lambda _value: True,
    )
    assert result.convert is True
    assert result.failures == ()
    assert result.total == 2


def test_one_refusing_value_blocks_the_conversion_and_is_reported_verbatim():
    refused = "sessA/wire.jsonl:42"
    result = uuid_gate.gate(
        ["018f0000-0000-7000-8000-000000000001", refused, "u-3"],
        cast=lambda value: value != refused,
    )
    assert result.convert is False
    assert result.failures == (refused,)
    # The refusal carries the value as stored — never None, never dropped.
    assert all(value is not None for value in result.failures)


def test_failures_are_distinct_but_multiplicity_is_counted():
    refused = "file.jsonl:7"
    result = uuid_gate.gate(
        [refused, refused, "ok-1"], cast=lambda value: value != refused)
    assert result.convert is False
    assert result.failures == (refused,)
    assert result.total == 3


def test_an_empty_scan_converts_nothing():
    """No evidence is not evidence: an empty corpus authorises no retyping."""
    result = uuid_gate.gate([], cast=lambda _value: True)
    assert result.convert is False
    assert result.total == 0


def test_merge_splits_ok_and_failed_file_counts():
    """An errored file is NOT an ok file: `_merge` books them apart, so
    the summary line can never fold a parse failure into files_ok."""
    ok = uuid_gate._Part("ok", ("u-1",), (), (), (), 1)  # pylint: disable=protected-access
    bad = uuid_gate._Part("err", (), (), (), ("f: boom",), 1)  # pylint: disable=protected-access
    merged = uuid_gate._merge([ok, bad, ok])  # pylint: disable=protected-access
    uuids, _non_str, _bounds, errors, files_ok, files_failed = merged
    assert (files_ok, files_failed) == (2, 1)
    assert uuids == ["u-1", "u-1"]  # merge concatenates; gate() dedups
    assert errors == ["f: boom"]


def test_the_summary_line_labels_the_two_file_counts_honestly():
    """`files_ok` counts only successful parses; failures show as their
    own field beside parse_errors — the printed report cannot lie."""
    line = uuid_gate._summary_line(  # pylint: disable=protected-access
        files_ok=2, files_failed=1, errors=["f: boom"],
        uuids=["a", "b", "b"], non_str=set())
    assert line == ("files_ok=2 files_failed=1 parse_errors=1 "
                    "uuid_values=3 distinct=2 non_str=0")


def test_the_measure_column_lists_match_ingest_persist():
    """uuid_gate's INT4 measure columns (R7 evidence) and ingest_persist's
    range-check lists are one list held in two places: an edit to either
    alone must fail here."""
    assert uuid_gate.RECORD_MEASURES == ingest_persist._RECORD_INT4  # pylint: disable=protected-access
    assert uuid_gate.TOOL_MEASURES == ingest_persist._TOOL_INT4  # pylint: disable=protected-access


@pytest.mark.db
def test_the_chosen_branch_is_the_split_column_set():
    """The gate's verdict, asserted against the applied schema.

    The corpus + fixture scan found non-casting values (lane uuids of
    the shape ``<session-or-key>:<counter>``), so R6's one-column uuid
    conversion does not fire — task 8 stores the split instead:
    ``uuid_col`` (uuid), ``seq`` (integer) and ``fallback`` (text),
    with no bare ``uuid`` column left behind.
    """
    name = scratch_db.create_database("uuid_gate_verdict")
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            rows = conn.execute(
                "SELECT column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'records' "
                "AND column_name IN ('uuid', 'uuid_col', 'seq', 'fallback')"
                "ORDER BY column_name").fetchall()
            conn.commit()
        assert rows == [("fallback", "text"), ("seq", "integer"),
                        ("uuid_col", "uuid")], rows
    finally:
        scratch_db.drop_database(name)


def test_split_counts_classifies_each_slot_and_finds_coexistence():
    """The corpus report's classification: one count per storage slot,
    and the heads stored BOTH pure and split — the coexistence evidence
    for the partition scheme (a uuid_col-only partition would merge
    them)."""
    head = "018f0000-0000-7000-8000-000000000001"
    counts = uuid_gate.split_counts([
        head,                  # uuid slot
        f"{head}:5",           # seq slot, SAME head
        f"{head}:5",           # duplicate value: totals count it twice
        "sess/wire.jsonl:42",  # fallback slot
        f"{head}:9",           # another seq on the same head
    ])
    assert counts.distinct == {"uuid": 1, "seq": 2, "fallback": 1}
    assert counts.totals == {"uuid": 1, "seq": 3, "fallback": 1}
    assert counts.coexistence == frozenset({head})


def test_split_counts_reports_no_coexistence_for_disjoint_heads():
    head = "018f0000-0000-7000-8000-000000000001"
    other = "018f0000-0000-7000-8000-000000000002"
    counts = uuid_gate.split_counts([head, f"{other}:5"])
    assert counts.coexistence == frozenset()
