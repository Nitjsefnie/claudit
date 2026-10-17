#!/usr/bin/env python3
"""The records.uuid type gate and its corpus scan.

``records`` stores the identity in a three-slot split
(``uuid_col UUID`` / ``seq INT4`` / ``fallback TEXT``, classified once
at persist by ``backend/uuid_split.py``), so a value that refuses a
bare uuid cast is never coerced and never lost — it lands in ``seq`` or
``fallback`` and rebuilds byte-identically on read. This tool remains
the CLASSIFIER's evidence pass: ``gate`` still reports which values
refuse the cast (the cast evidence behind the split), ``split_counts``
reports every distinct value's slot plus the pure/split coexistence
that decides how the winner rule may group, and
``pg_roundtrip_disagreements`` proves PostgreSQL re-renders every
``uuid_col`` head byte-identically — the native uuid column stores
PG's OWN rendering, so one disagreement would be a stored-text change.

:func:`gate` is the decision rule (pure; both branches unit-tested in
``tests/test_uuid_gate.py``). :func:`main` runs it with POSTGRESQL'S OWN
cast as the predicate over the full-corpus mirror and the fixtures —
one parse pass, because only the parser's output is what would be
stored. The same pass reports each per-row measure column's observed
bounds to verify its INT4 storage range.

SV-FULL-CORPUS: run against a mirror OUTSIDE the tree pointed at with
R2_ENDPOINT=file://..., never the live database or the session tree.

    set -a; . <deploy env-file>; set +a       # R2_* by name; values never printed
    export R2_ENDPOINT=file:///tmp/claudit-mirror/
    export R2_BUCKET=claude+codex+kimi+zai+llama+openrouter
    export PGHOST=<private-postgres-socket> PGPORT=<port> PGUSER=<user>
    python3 -I scripts/uuid_gate.py --workers 4
"""
from __future__ import annotations

import argparse
import lzma
import multiprocessing
import pathlib
import sys
from collections.abc import Callable, Iterable
from typing import NamedTuple

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend import key_layout, parse, r2, uuid_split  # noqa: E402  pylint: disable=wrong-import-position

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES_ROOT = REPO_ROOT / "fixtures"

# Per-row measure columns stored as INT4; bounds prove no value
# reaches 2^31-1. Mirrors the lists ingest_persist guards.
RECORD_MEASURES = (
    "fresh_tokens", "cache_creation_tokens", "cache_read_tokens",
    "cache_write_tokens", "output_tokens", "eph5_tokens", "eph1h_tokens",
    "thinking_tokens", "text_chars",
)
TOOL_MEASURES = ("lines_added", "lines_deleted", "result_chars")
INT4_MAX = (1 << 31) - 1


class GateResult(NamedTuple):
    """Whether every distinct value is UUID-castable."""

    convert: bool
    failures: tuple[str, ...]   # distinct, input order, verbatim as stored
    total: int                  # values seen (multiplicity included)


class SplitCounts(NamedTuple):
    """The split classification of a value set.

    ``coexistence`` holds the heads stored BOTH as a pure uuid and with
    a counter — the evidence for the partition decision: those heads are
    exactly the ones a partition keyed on ``uuid_col`` alone would merge
    into one winner group despite their distinct identities.
    """

    distinct: dict[str, int]    # per slot: distinct values
    totals: dict[str, int]      # per slot: values with multiplicity
    coexistence: frozenset[str]  # uuid_col heads in both uuid and seq slots


class _Part(NamedTuple):
    kind: str
    uuids: tuple[str, ...]
    non_str: tuple[str, ...]
    bounds: tuple[tuple[str, int, int], ...]   # (column, min, max)
    errors: tuple[str, ...]
    files: int


def gate(values: Iterable[str],
         cast: Callable[[str], bool]) -> GateResult:
    """Report whether every distinct stored value casts, with verbatim refusals.

    An empty scan reports convert=False: no evidence is not evidence.
    Inject PostgreSQL's uuid cast for scans or a test predicate.
    """
    seen: set[str] = set()
    failures: list[str] = []
    total = 0
    for value in values:
        total += 1
        if value in seen:
            continue
        seen.add(value)
        if not cast(value):
            failures.append(value)
    return GateResult(bool(seen) and not failures, tuple(failures), total)


def split_counts(values: Iterable[str]) -> SplitCounts:
    """Classify every value into its storage slot.

    Uses the SHIPPED classifier (backend/uuid_split.split_uuid), so the
    corpus counts describe the code the ingest actually runs. Totals
    count with multiplicity, distinct dedups on the value, and
    ``coexistence`` collects the heads stored BOTH pure and split — the
    evidence for the winner-rule partition decision.
    """
    distinct = {"uuid": 0, "seq": 0, "fallback": 0}
    totals = {"uuid": 0, "seq": 0, "fallback": 0}
    pure_heads: set[str] = set()
    split_heads: set[str] = set()
    seen: set[str] = set()
    for value in values:
        parts = uuid_split.split_uuid(value)
        slot = ("fallback" if parts.fallback is not None
                else "seq" if parts.seq is not None else "uuid")
        totals[slot] += 1
        if value in seen:
            continue
        seen.add(value)
        distinct[slot] += 1
        if slot == "uuid":
            pure_heads.add(value)
        elif parts.uuid_col is not None:
            split_heads.add(parts.uuid_col)
    return SplitCounts(distinct, totals,
                       frozenset(pure_heads & split_heads))


def pg_roundtrip_disagreements(values: Iterable[str]) -> list[str]:
    """The uuid_col heads PostgreSQL would NOT re-render identically.

    ``uuid_col`` stores the native uuid type, so what PostgreSQL writes
    back IS the stored text — a head it renders differently would mean
    a changed identity on every read. Must be empty; disagreements come
    back verbatim. The CASE keeps evaluation order (a head the cast
    refuses must be REPORTED, not raise out of the check).
    """
    heads = sorted({
        parts.uuid_col for value in values
        if (parts := uuid_split.split_uuid(value)) is not None
        and parts.uuid_col is not None
    })
    if not heads:
        return []
    with psycopg.connect("") as conn:
        conn.execute("CREATE TEMP TABLE uuid_vals (v text)")
        conn.execute(_PG_UUID_PREDICATE)
        _insert_values(conn, heads)
        rows = conn.execute(
            "SELECT v FROM uuid_vals WHERE CASE "
            "  WHEN pg_temp._uuid_ok(v) THEN v::uuid::text <> v "
            "  ELSE true END").fetchall()
    return [row[0] for row in rows]


def _track(bounds: dict[str, list[int]], column: str, value) -> None:
    if value is None:
        return
    got = bounds.setdefault(column, [value, value])
    if value < got[0]:
        got[0] = value
    if value > got[1]:
        got[1] = value


def _extract(out: dict) -> tuple[list[str], list[str], dict[str, list[int]]]:
    uuids: list[str] = []
    non_str: list[str] = []
    bounds: dict[str, list[int]] = {}
    for rec in out.get("records", ()):
        value = rec.get("uuid")
        if value is None:
            continue
        if isinstance(value, str):
            uuids.append(value)
        else:
            non_str.append(repr(value))
        for column in RECORD_MEASURES:
            _track(bounds, column, rec.get(column))
    for tool in out.get("tool_uses", ()):
        for column in TOOL_MEASURES:
            _track(bounds, column, tool.get(column))
    return uuids, non_str, bounds


def _read_key(key: str) -> bytes:
    stream = r2.get_stream(key)
    try:
        return stream.read()
    finally:
        stream.close()


def _parse_job(job: tuple[str, str]) -> _Part:
    kind, name = job
    try:
        if kind == "r2":
            blob = _read_key(name)
        else:
            blob = pathlib.Path(name).read_bytes()
            if name.endswith(".xz"):
                blob = lzma.decompress(blob)
        out = parse.parse_file(name, blob)
        uuids, non_str, bounds = _extract(out)
        flat = tuple((col, lo, hi) for col, (lo, hi) in bounds.items())
        return _Part("ok", tuple(uuids), tuple(non_str), flat, (), 1)
    except Exception as err:  # noqa: BLE001 - reported, not swallowed
        return _Part("err", (), (), (), (f"{name}: {err!r}",), 1)


def _jobs() -> list[tuple[str, str]]:
    """Transcripts ingest would parse: R2 keys classify accepts + fixtures."""
    jobs: list[tuple[str, str]] = []
    for obj in r2.list_keys():
        object_key = r2.split_key(obj.key)[1]
        if not object_key.endswith((".jsonl", ".jsonl.xz")):
            continue
        if key_layout.classify(object_key) is None:
            continue
        jobs.append(("r2", obj.key))
    for path in sorted(FIXTURES_ROOT.rglob("*")):
        if path.is_file() and path.name.endswith((".jsonl", ".jsonl.xz")):
            jobs.append(("fixture", str(path)))
    return jobs


def _merge(parts: Iterable[_Part]) -> tuple[list[str], set[str],
                                            dict[str, list[int]],
                                            list[str], int, int]:
    uuids: list[str] = []
    non_str: set[str] = set()
    bounds: dict[str, list[int]] = {}
    errors: list[str] = []
    files_ok = 0
    files_failed = 0
    for part in parts:
        if part.kind == "ok":
            uuids.extend(part.uuids)
            non_str.update(part.non_str)
            for column, lo, hi in part.bounds:
                have = bounds.setdefault(column, [lo, hi])
                have[0] = min(have[0], lo)
                have[1] = max(have[1], hi)
            files_ok += part.files
        else:
            files_failed += part.files
        errors.extend(part.errors)
    return uuids, non_str, bounds, errors, files_ok, files_failed


_PG_UUID_PREDICATE = """
CREATE FUNCTION pg_temp._uuid_ok(t text) RETURNS boolean AS $$
BEGIN
  PERFORM t::uuid;
  RETURN true;
EXCEPTION WHEN invalid_text_representation THEN
  RETURN false;
END;
$$ LANGUAGE plpgsql
"""

_INSERT_VALUES = "INSERT INTO uuid_vals (v) VALUES (%s)"


def _insert_values(conn, values: list[str]) -> None:
    batch: list[tuple[str, ...]] = []
    with conn.cursor() as cur:
        for value in values:
            batch.append((value,))
            if len(batch) >= 5000:
                cur.executemany(_INSERT_VALUES, batch)
                batch = []
        if batch:
            cur.executemany(_INSERT_VALUES, batch)


def pg_uuid_failures(values: set[str]) -> set[str]:
    """The distinct values POSTGRESQL refuses to cast to uuid.

    Authoritative over any lexical guess: the migration would run this
    same cast, so whatever this rejects is what would abort it.
    """
    failures: set[str] = set()
    with psycopg.connect("") as conn:
        conn.execute("CREATE TEMP TABLE uuid_vals (v text)")
        conn.execute(_PG_UUID_PREDICATE)
        _insert_values(conn, sorted(values))
        rows = conn.execute(
            "SELECT v FROM uuid_vals WHERE NOT pg_temp._uuid_ok(v)"
        ).fetchall()
        failures.update(row[0] for row in rows)
    return failures


def _collect(workers: int) -> tuple[list[str], set[str],
                                    dict[str, list[int]], list[str],
                                    int, int]:
    jobs = _jobs()
    print(f"scan jobs={len(jobs)} workers={workers}", flush=True)
    ctx = multiprocessing.get_context("fork")
    with ctx.Pool(processes=workers) as pool:
        parts = list(pool.imap_unordered(_parse_job, jobs, chunksize=8))
    return _merge(parts)


def _report_scan(errors: list[str], non_str: set[str]) -> None:
    for line in errors[:20]:
        print(f"PARSE-ERROR {line}", flush=True)
    for value in sorted(non_str)[:20]:
        print(f"NON-STR-UUID {value}", flush=True)


def _report_verdict(result: GateResult, sample: int) -> None:
    # This line reports WHICH values refuse a bare uuid cast — exactly the
    # ones the split stores in seq/fallback instead of losing.
    verdict = ("SPLIT: every distinct value would cast"
               if result.convert else
               "SPLIT: refusing values fall to seq/fallback")
    print(f"uuid gate: {verdict} (distinct refusing a bare uuid cast="
          f"{len(result.failures)}, values={result.total})", flush=True)
    for value in result.failures[:sample]:
        print(f"FAILING-UUID {value!r}", flush=True)
    if len(result.failures) > sample:
        print(f"... {len(result.failures) - sample} more failing "
              "uuid values not shown", flush=True)


def _report_split(counts: SplitCounts, disagreements: list[str],
                  sample: int) -> None:
    print("uuid split classification (backend/uuid_split.split_uuid):",
          flush=True)
    for slot in ("uuid", "seq", "fallback"):
        print(f"  {slot}: distinct={counts.distinct[slot]} "
              f"values={counts.totals[slot]}", flush=True)
    print(f"  pure/split uuid_col coexistence: {len(counts.coexistence)}",
          flush=True)
    for head in sorted(counts.coexistence)[:sample]:
        print(f"COEXIST {head!r}", flush=True)
    if len(counts.coexistence) > sample:
        print(f"... {len(counts.coexistence) - sample} more coexisting "
              "heads not shown", flush=True)
    print(f"  PG uuid_col round-trip disagreements: {len(disagreements)}",
          flush=True)
    for head in disagreements[:sample]:
        print(f"ROUNDTRIP {head!r}", flush=True)


def _report_bounds(bounds: dict[str, list[int]]) -> None:
    print("measure bounds (R7 evidence; int4 max "
          f"{INT4_MAX}):", flush=True)
    for column in sorted(bounds):
        low, high = bounds[column]
        ok = "ok" if low >= -(1 << 31) and high <= INT4_MAX else "OVERFLOW"
        print(f"  {column}: min={low} max={high} {ok}", flush=True)
    for column in sorted(set(RECORD_MEASURES + TOOL_MEASURES)
                         - set(bounds)):
        print(f"  {column}: no values observed")


def _summary_line(files_ok: int, files_failed: int, errors: list[str],
                  uuids: list[str], non_str: set[str]) -> str:
    """The scan summary: ok and failed file counts stay apart, so the
    report can never count an errored file as files_ok."""
    return (f"files_ok={files_ok} files_failed={files_failed} "
            f"parse_errors={len(errors)} uuid_values={len(uuids)} "
            f"distinct={len(set(uuids))} non_str={len(non_str)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--sample", type=int, default=50,
                        help="failing uuid values to print verbatim")
    args = parser.parse_args(argv)

    uuids, non_str, bounds, errors, files_ok, files_failed = \
        _collect(args.workers)
    print(_summary_line(files_ok, files_failed, errors, uuids, non_str),
          flush=True)
    _report_scan(errors, non_str)

    # One PG round of casts for the whole distinct set, then the gate.
    failing = pg_uuid_failures(set(uuids))
    result = gate(uuids, lambda value: value not in failing)
    if non_str:
        # The split classifies text, so report non-string values and
        # let the write fail rather than coercing an invalid identity.
        result = result._replace(convert=False)
    _report_verdict(result, args.sample)
    _report_split(split_counts(uuids),
                  pg_roundtrip_disagreements(set(uuids)), args.sample)
    _report_bounds(bounds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
