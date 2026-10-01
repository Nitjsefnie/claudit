#!/usr/bin/env python3
"""Measure the CPU work of one reparse pass over the mini fixture mirror.

The maintainer's reparse lever (issue #436): the full reparse of a bucket
is the app's biggest CPU consumer, its per-file cost has already moved
2.5x once (motivation, not evidence — those were wall numbers taken on a
shared box), and what keeps that lever honest is a deterministic
per-file budget. This is that instrument, shaped by the performance
field guide's findings rather than by a first instinct:

- BUDGET CPU WORK, NEVER WALL (guide: "co-tenant load moved wall-clock
  measurements by 2-4x without changing the work"). Everything timed
  here is ``time.process_time()``. No wall-clock function appears in
  this file, and ``tests/test_reparse_bench.py`` pins that.

- DECOMPOSE, THEN RATCHET EACH PHASE (guide: "a total ratchet reds on
  tree growth with no product change"). The pass is timed in the phases
  it is actually made of, by wrapping the exact callables the real path
  calls — no reimplementation of the dispatch, no second copy of the
  parse to drift from the first. The ratcheted number is each phase's
  SHARE of the run's own CPU (a ratio within one process's own run,
  the most load-resistant form available): on this box's shared CPU the
  pass total moved +-28% between identical runs while the phase shares
  moved about a point.

- COUNTS BEAT TIMES WHERE THEY EXIST (guide: "instruction counts do not
  drift with machine speed"). ``perf stat``'s user-space instruction
  count is measured when the probe says it works, net of a baseline run
  that prices the interpreter's own startup and the backend's imports,
  and reported. It is not the ratcheted quantity, for two reasons: a
  `perf stat` reading covers a whole process, so it cannot attribute a
  count to one of the phases above; and whether it works at all is a
  property of the machine the bench runs on, so a recorded family whose
  unit changes between runners is not a ratchet. When the probe fails
  the bench says so on every line it prints — a measurement that is
  absent must never read as a measurement that passed.

- THE INSTRUMENT ACCOUNTS FOR THE WHOLE (guide: "the named phases
  summed to less than the reported total ... treat any divergence as an
  unmeasured phase"). The fourth phase, ``residual``, IS the divergence:
  everything in the pass the three wrapped callables do not account for
  — the reader, the fetch_and_parse body, the loop, and this bench's own
  wrapper overhead. It is named, measured and ratcheted rather than
  rounded away, and every output prints the share sum so a hole in the
  partition cannot hide.

- HERMETIC INPUTS (guide: "a differential proof that compared two
  different inputs"). The corpus is the committed ``fixtures/r2_mini``
  mirror and the settings that select it are pinned by the bench, so
  nothing from a live bucket, no network, no database and no wall-clock
  component can reach the number. The listing is sorted by key, so the
  pass walks the corpus in the same order on every machine.

- ONE RUN, NO STATISTICS, with a fixed amplification. The corpus is a
  few hundred microseconds of work — far too small to time once — so
  ``PASSES`` passes are timed together inside one region and divided by
  their own count. No minimum, median or repeat is taken. ``WARMUP``
  passes run before the clock starts: the first passes of a fresh
  interpreter cost two orders of magnitude more than steady state, and
  an un-warmed reading would price the interpreter rather than the
  parse.

  python3 scripts/ci/reparse_bench.py                  # human report
  python3 scripts/ci/reparse_bench.py --machine        # key=value fields
  python3 scripts/ci/reparse_bench.py --write m.json   # measurement file
  python3 scripts/ci/reparse_bench.py --check m.json   # gate the file
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

if __package__:
    # pylint: disable-next=relative-beyond-top-level,no-name-in-module
    from . import reparse_report, thresholds
else:
    thresholds = importlib.import_module('thresholds')
    reparse_report = importlib.import_module('reparse_report')

# The reading's shape and every way it is written, printed and gated live
# in the report module; this one measures.
Measurement = reparse_report.Measurement

ROOT = Path(__file__).resolve().parents[2]
# Run as `python3 scripts/ci/reparse_bench.py` from anywhere: sys.path[0]
# is scripts/ci, and the measured code under test is the backend package.
sys.path.insert(0, str(ROOT))
MIRROR = ROOT / 'fixtures' / 'r2_mini'
BUCKET = 'claude'
# The unit the recorded per-phase numbers are in: percent of the pass's
# own CPU. It lives with the loader that validates the document carrying
# it, so the number's meaning has one source of truth.
UNIT = thresholds.REPARSE_UNIT
# Every phase of the pass, and the partition they make: the three wrapped
# callables' work plus the residual, which is whatever they do not
# account for. The names come from the loader, which validates the
# document carrying them, so a phase cannot be measured under one name
# and recorded under another.
PHASES = thresholds.REPARSE_PHASES
# Fixed amplification, and the warm-up that precedes it. Measured on the
# shared seeding box, four runs each, the run-to-run spread of a phase
# share was about 2 points at 5000 passes and about 0.7 points at 20000
# — inside the 1.5-point yardstick the ratchet moves on, which is what
# makes a recorded floor mean something. 20000 passes is roughly 8 CPU
# seconds, negligible against a 30-minute job.
PASSES = 20000
# 400 warm-up passes is past the knee: an un-warmed pass costs about 15%
# more (8.9 CPU s over 20000 passes cold, against 7.2-8.1 warm), while
# 2000 warm-up passes measure the same shares as 400.
WARMUP_PASSES = 400
PERF_TIMEOUT_S = 120
_QUANTUM = Decimal('0.1')
_OVER_BUDGET_REMEDY = (
    'A reparse phase is over its recorded share of the pass: make that '
    'phase cheaper. The recorded budget is never raised by hand.')


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
from backend import agent_sidecar, ingest_fetch, ingest_scan, parse, r2  # noqa: E402


class Entry(NamedTuple):
    """One transcript of the fixed corpus, with its bytes already read."""
    key: str
    sidecar_key: str | None
    blob: bytes
    sidecar_blob: bytes | None


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


@contextlib.contextmanager
def instrumented(totals: dict):
    """Time the callables the real path is made of, then put them back.

    Three public callables, wrapped in place and restored on exit:
    ``parse.sniff_format`` (nested inside ``parse.parse_file``, so
    ``parse_body`` is parse_file's own time minus it), ``parse.parse_file``
    (the whole parse, whatever format it dispatches to) and
    ``agent_sidecar.apply_agent_sidecar`` (the meta.json sidecar step,
    which sits outside parse_file, in fetch_and_parse). Whatever the
    pass does beyond those three falls to the residual.
    """
    originals = (
        (parse, 'sniff_format', parse.sniff_format),
        (parse, 'parse_file', parse.parse_file),
        (agent_sidecar, 'apply_agent_sidecar',
         agent_sidecar.apply_agent_sidecar),
    )

    def timed(name, function):
        def wrapper(*args, **kwargs):
            start = time.process_time()
            try:
                return function(*args, **kwargs)
            finally:
                totals[name] += time.process_time() - start
        return wrapper

    try:
        for module, attribute, _original in originals:
            setattr(module, attribute,
                    timed(attribute, getattr(module, attribute)))
        yield
    finally:
        for module, attribute, original in originals:
            setattr(module, attribute, original)


def share_of(cpu_s: float, total_cpu_s: float) -> Decimal:
    """One phase's percent of the run's own CPU, to one decimal place."""
    if total_cpu_s <= 0:
        raise ValueError(
            f'the pass measured {total_cpu_s} CPU s: nothing to share')
    share = (Decimal(repr(cpu_s)) / Decimal(repr(total_cpu_s))) * 100
    return share.quantize(_QUANTUM)


def measure(entries: list[Entry], passes: int = PASSES,
            warmup: int = WARMUP_PASSES,
            instructions_per_file: int | None = None,
            instruction_note: str = '') -> Measurement:
    """Time `passes` passes over the corpus and split the CPU by phase."""
    if passes <= 0:
        raise ValueError(f'passes must be positive: {passes}')
    if warmup < 0:
        raise ValueError(f'warmup must not be negative: {warmup}')
    if not entries:
        raise ValueError(f'no transcripts to parse under {MIRROR}')
    totals = {'sniff_format': 0.0, 'parse_file': 0.0,
              'apply_agent_sidecar': 0.0}
    with instrumented(totals):
        for _ in range(warmup):
            run_pass(entries)
        for name in totals:
            totals[name] = 0.0
        records = 0
        start = time.process_time()
        for _ in range(passes):
            records = run_pass(entries)
        total = time.process_time() - start
    phase_cpu_s = {
        'sniff': totals['sniff_format'],
        'parse_body': totals['parse_file'] - totals['sniff_format'],
        'sidecar': totals['apply_agent_sidecar'],
        # The divergence between the named phases and the run: whatever
        # the wrapped callables do not account for.
        'residual': total - totals['parse_file']
        - totals['apply_agent_sidecar'],
    }
    shares = {name: share_of(phase_cpu_s[name], total) for name in PHASES}
    return Measurement(total, len(entries), passes, records, phase_cpu_s,
                       shares, instructions_per_file, instruction_note)


# --- the instruction count ---------------------------------------------------

def parse_perf_count(text: str) -> int | None:
    """The instruction count out of `perf stat -x,` output.

    The CSV carries value, unit, event, ... in that order, so the first
    field that is a plain integer is the count. An unsupported event, a
    permission refusal, or no line at all reads as None, which the caller
    reports rather than guesses at.
    """
    for line in text.splitlines():
        fields = line.strip().split(',')
        if fields and fields[0].strip().isdigit():
            return int(fields[0].strip())
    return None


def _child_command(path, passes: int, warmup: int) -> list:
    return [sys.executable, str(Path(__file__).resolve()), '--child',
            '--write', str(path), '--passes', str(passes),
            '--warmup', str(warmup)]


def _perf_child(scratch: Path, label: str, passes: int,
                warmup: int) -> tuple:
    """Run one measurement child under `perf stat`.

    Returns (instruction count or None, the child's measurement or None,
    why there is no count). perf counts the WHOLE process, so the
    reading carries the interpreter's startup and the backend's
    imports with it; the caller differences a near-empty run against
    this one to price the work alone.
    """
    perf = shutil.which('perf')
    if perf is None:
        return None, None, 'perf is not installed'
    report = scratch / f'perf-{label}.csv'
    measurement_path = scratch / f'measurement-{label}.json'
    done = subprocess.run(  # pylint: disable=subprocess-run-check
        [perf, 'stat', '-e', 'instructions:u', '-x,', '-o', str(report),
         '--', *_child_command(measurement_path, passes, warmup)],
        capture_output=True, text=True, timeout=PERF_TIMEOUT_S, check=False)
    if done.returncode != 0:
        return None, None, (f'perf stat exited {done.returncode}: '
                            f'{(done.stderr or "").strip()[:120]}')
    text = (report.read_text(encoding='utf-8', errors='replace')
            if report.exists() else '')
    count = parse_perf_count(text)
    if count is None:
        return None, None, 'perf stat reported no instruction count'
    if not measurement_path.exists():
        return None, None, 'the perf-wrapped bench run wrote no measurement'
    return count, reparse_report.measurement_from_file(
        measurement_path), ''


def _plain_child(scratch: Path, passes: int, warmup: int) -> Measurement:
    """The measurement with no counter facility: CPU time only."""
    path = scratch / 'measurement-plain.json'
    done = subprocess.run(  # pylint: disable=subprocess-run-check
        _child_command(path, passes, warmup),
        capture_output=True, text=True, timeout=PERF_TIMEOUT_S, check=False)
    if done.returncode != 0 or not path.exists():
        raise ValueError('the bench run failed: '
                         f'{(done.stderr or "").strip()[:200]}')
    return reparse_report.measurement_from_file(path)


def measure_in_child(passes: int = PASSES,
                     warmup: int = WARMUP_PASSES) -> Measurement:
    """The bench's measurement, taken in a fresh child process.

    Always a child, with or without perf, so the reading starts from the
    same fresh interpreter either way. With perf there are two runs: the
    timed one, and a one-pass run pricing the constant the first carries
    (startup plus imports); the difference is the work the passes did,
    divided by the file-parses it did. Without a usable instruction
    count the measurement still stands — CPU time, with the reason it is
    not a count printed on every line that reports the run.
    """
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        count, measurement, note = _perf_child(root, 'work', passes, warmup)
        per_file = None
        if measurement is None:
            measurement = _plain_child(root, passes, warmup)
            note = f'{note}: CPU time is the measurement'
        else:
            baseline, _unused, baseline_note = _perf_child(
                root, 'baseline', 1, 0)
            if baseline is None:
                note = baseline_note
            else:
                per_file = ((count - baseline)
                            // max(passes * measurement.files, 1))
                note = 'net of a startup-and-imports baseline run'
        return measurement._replace(
            instructions_per_file=per_file, instruction_note=note)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', action='store_true',
                        help=argparse.SUPPRESS)
    parser.add_argument('--write', type=Path,
                        help='write the measurement to this JSON file')
    parser.add_argument('--check', type=Path, metavar='MEASUREMENT',
                        help='gate a written measurement against the '
                             'committed floors; nonzero when a phase is over')
    parser.add_argument('--machine', action='store_true',
                        help='print the reading as key=value fields')
    parser.add_argument('--passes', type=int, default=PASSES,
                        help='timed passes over the corpus')
    parser.add_argument('--warmup', type=int, default=WARMUP_PASSES,
                        help='untimed passes run before the clock starts')
    parser.add_argument('--thresholds', type=Path,
                        default=thresholds.THRESHOLDS,
                        help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.child:
            if args.write is None:
                raise ValueError('--child needs --write')
            reparse_report.write_measurement(
                args.write,
                measure(corpus(), passes=args.passes, warmup=args.warmup))
            return 0
        if args.check is not None:
            return reparse_report.check(args.check, args.thresholds)
        measurement = measure_in_child(args.passes, args.warmup)
        if args.write is not None:
            reparse_report.write_measurement(args.write, measurement)
        print(reparse_report.machine_line(measurement) if args.machine
              else reparse_report.report(measurement))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
