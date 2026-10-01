#!/usr/bin/env python3
"""Measure the CPU cost of one reparse pass over the mini fixture mirror.

The maintainer's reparse lever (issue #436): the full reparse of a bucket
is the app's biggest CPU consumer, and its per-file cost has already
moved 2.5x once. This bench is the deterministic, ratcheted-down
instrument for that number: a fixed corpus, a fixed amount of work, CPU
time only, and a recorded unit a ratchet can tighten.

What is measured, and why each piece is here:

- CORPUS: the committed ``fixtures/r2_mini`` mirror, always, whatever
  ``R2_ENDPOINT``/``R2_BUCKET`` the ambient environment says. A recorded
  floor is only comparable against one fixed corpus, so the bench pins
  both settings itself rather than inheriting a developer's bucket.
- THE REAL HOT PATH: the corpus is collected with the ingest's own
  listing walk (``ingest_scan._scan_objects``, the same walk the ingest
  run lists with) and each file is parsed with
  ``ingest_fetch.fetch_and_parse`` — the per-file unit the ingest pool
  calls (``backend/ingest.py``'s ``_fetch_and_parse``), which sniffs the
  format and runs ``parse.parse_file``. Nothing here re-implements key
  classification or parsing; a bench that did would measure something
  the ingest never runs.
- BYTES BEFORE THE CLOCK: each transcript's bytes are read once, before
  the measured region. A fetch inside it would measure syscalls and
  page-cache state — on this corpus roughly twenty times the parse cost
  itself — which is fetch noise, not the per-file parse work the budget
  is about. The reader the pass calls serves the same bytes from memory.
- CPU TIME, NEVER WALL CLOCK: ``time.process_time()``, so a loaded
  runner cannot move the number. No wall-clock function appears in this
  file, and ``tests/test_reparse_bench.py`` pins that.
- ONE RUN, NO STATISTICS: a single timed region, reported as measured.
  Repetition is a fixed, documented amplification (the corpus is a few
  hundred microseconds of work, far too small to time once), not a
  statistic: ``PASSES`` passes are timed together and divided by their
  own count, and no minimum, median or repeat is taken.
- WARM-UP: the first passes of a fresh interpreter cost two orders of
  magnitude more than steady state (bytecode specialisation, first-call
  caches, regex compilation). ``WARMUP_PASSES`` of them run before the
  clock starts, so the recorded number is the steady-state per-file cost
  a reparse of thousands of files actually pays.
- PARSE ORDER FIXED: the listing walk yields filesystem order, so the
  corpus is sorted by key; the measured pass then walks the same order
  on every machine.

No database and no network: the mirror is read through ``r2``'s
``file://`` mode, and the parse path opens no connection (a test asserts
it with a tripwire on psycopg).

  python3 scripts/ci/reparse_bench.py              # human report
  python3 scripts/ci/reparse_bench.py --value      # the recorded number
  python3 scripts/ci/reparse_bench.py --machine    # key=value fields
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

if __package__:
    # pylint: disable-next=relative-beyond-top-level,no-name-in-module
    from . import thresholds
else:
    thresholds = importlib.import_module('thresholds')

ROOT = Path(__file__).resolve().parents[2]
# Run as `python3 scripts/ci/reparse_bench.py` from anywhere: sys.path[0]
# is scripts/ci, and the measured code under test is the backend package.
sys.path.insert(0, str(ROOT))
MIRROR = ROOT / 'fixtures' / 'r2_mini'
BUCKET = 'claude'
# The recorded unit and its scale live with the loader that validates the
# document, so the number's meaning has one source of truth.
UNIT = thresholds.REPARSE_UNIT
UNIT_FILES = thresholds.REPARSE_FILES_PER_UNIT
# Fixed amplification: passes timed together and divided by their count.
# 2000 passes of this corpus is roughly 0.2 s of CPU, far enough above
# the clock's resolution to be stable, far enough below the job's budget
# to be invisible in it.
PASSES = 2000
WARMUP_PASSES = 100
_QUANTUM = Decimal('0.1')


def _pin_corpus_env():
    """Point the R2 client at the committed mirror, unconditionally.

    Called at import (before the backend modules are read, so no import
    can observe another endpoint) and again from ``corpus()``, so a
    process that changed the environment after importing the bench still
    measures the fixture.
    """
    os.environ['R2_ENDPOINT'] = f'file://{MIRROR}'
    os.environ['R2_BUCKET'] = BUCKET


_pin_corpus_env()

# pylint: disable-next=wrong-import-position
from backend import ingest_fetch, ingest_scan, r2  # noqa: E402


class Entry(NamedTuple):
    """One transcript of the fixed corpus, with its bytes already read."""
    key: str
    sidecar_key: str | None
    blob: bytes
    sidecar_blob: bytes | None


class Measurement(NamedTuple):
    """One bench run: CPU seconds, and the recorded number they became."""
    cpu_s: float
    files: int
    passes: int
    records: int
    value: Decimal
    ms_per_file: float


def corpus() -> list[Entry]:
    """The fixed corpus: every transcript the ingest's own listing walk
    finds in the mini mirror, sorted by key, bytes read once."""
    _pin_corpus_env()
    # _scan_objects is the ingest's listing walk, private because ingest
    # is its only caller. Calling it is the point: the alternative is a
    # second implementation of the listing rules, which is exactly the
    # drift this bench must not have.
    # pylint: disable-next=protected-access
    wires, _markers = ingest_scan._scan_objects()
    entries = []
    for wire in sorted(wires, key=lambda item: item.key):
        sidecar = wire.sidecar_key
        entries.append(Entry(
            wire.key, sidecar, r2.get_object(wire.key),
            r2.get_object(sidecar) if sidecar else None))
    return entries


def run_pass(entries: list[Entry]) -> int:
    """Parse every transcript once, through the ingest's per-file unit.

    The reader serves the pre-read bytes: nothing in this loop touches
    the filesystem, a socket or a database. Returns the number of records
    the pass parsed, so a caller can see the work was real.
    """
    blobs = {}
    for entry in entries:
        blobs[entry.key] = entry.blob
        if entry.sidecar_key is not None:
            blobs[entry.sidecar_key] = entry.sidecar_blob

    def reader(key: str) -> bytes:
        return blobs[key]

    records = 0
    for entry in entries:
        parsed = ingest_fetch.fetch_and_parse(
            entry.key, entry.sidecar_key, reader)
        records += len(parsed['records'])
    return records


def value_for(cpu_s: float, files: int, passes: int) -> Decimal:
    """The recorded number: CPU milliseconds per UNIT_FILES transcripts.

    Pure arithmetic over the measurement, so it is exact for a given
    reading and rounds to the one decimal place the document records.
    """
    if files <= 0 or passes <= 0:
        raise ValueError(
            'a measurement needs at least one file and one pass: '
            f'files={files}, passes={passes}')
    per_file_ms = (Decimal(repr(cpu_s)) / Decimal(passes * files)) * 1000
    return (per_file_ms * UNIT_FILES).quantize(_QUANTUM)


def measure(entries: list[Entry], passes: int = PASSES,
            warmup: int = WARMUP_PASSES) -> Measurement:
    """Time `passes` passes over the corpus and normalise the reading."""
    if passes <= 0:
        raise ValueError(f'passes must be positive: {passes}')
    if warmup < 0:
        raise ValueError(f'warmup must not be negative: {warmup}')
    if not entries:
        raise ValueError(f'no transcripts to parse under {MIRROR}')
    for _ in range(warmup):
        run_pass(entries)
    records = 0
    start = time.process_time()
    for _ in range(passes):
        records = run_pass(entries)
    cpu_s = time.process_time() - start
    files = len(entries)
    value = value_for(cpu_s, files, passes)
    return Measurement(cpu_s, files, passes, records, value,
                       cpu_s / (passes * files) * 1000)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    output = parser.add_mutually_exclusive_group()
    output.add_argument('--value', action='store_true',
                        help='print the recorded number and nothing else')
    output.add_argument('--machine', action='store_true',
                        help='print the reading as key=value fields')
    parser.add_argument('--passes', type=int, default=PASSES,
                        help='timed passes over the corpus')
    parser.add_argument('--warmup', type=int, default=WARMUP_PASSES,
                        help='untimed passes run before the clock starts')
    return parser


def _report(measurement: Measurement) -> str:
    return (f'reparse CPU {measurement.value} {UNIT} '
            f'({measurement.ms_per_file:.4f} ms/file) over '
            f'{measurement.files} transcripts, {measurement.passes} passes, '
            f'{measurement.records} records per pass, '
            f'{measurement.cpu_s:.4f} CPU s')


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        measurement = measure(corpus(), passes=args.passes,
                              warmup=args.warmup)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    if args.value:
        print(f'{measurement.value:.1f}')
    elif args.machine:
        print(f'reparse_bench measured={measurement.value:.1f} unit={UNIT} '
              f'ms_per_file={measurement.ms_per_file:.4f} '
              f'files={measurement.files} passes={measurement.passes} '
              f'records={measurement.records} cpu_s={measurement.cpu_s:.4f}')
    else:
        print(_report(measurement))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
