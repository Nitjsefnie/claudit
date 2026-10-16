#!/usr/bin/env python3
"""Measure the CPU work of one reparse pass over the mini fixture mirror.

The real parse path is partitioned into sniff, parse_body, sidecar and
residual phases. A warmed process-time pass reports CPU shares as telemetry;
a separate sys.monitoring pass counts bytecode work per file. Available
perf instruction counts provide a whole-process cross-check. The parse
surface check verifies that the corpus still exercises the measured path.

  python3 scripts/ci/reparse_bench.py                  # human report
  python3 scripts/ci/reparse_bench.py --machine        # key=value fields
  python3 scripts/ci/reparse_bench.py --write m.json    # measurement file
  python3 scripts/ci/reparse_bench.py --report m.json   # report saved reading
"""
from __future__ import annotations

import argparse
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
    from . import reparse_phases, reparse_report
else:
    reparse_phases = importlib.import_module('reparse_phases')
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
# The parse-surface gate (issue #503), run beside the measurement it
# qualifies: one counted pass over the corpus in a fresh child, no
# amplification, so it is bounded by the corpus rather than by a budget.
SURFACE_GATE = Path(__file__).resolve().with_name('reparse_surface.py')
SURFACE_TIMEOUT_S = 300
# The measurement and its report share one vocabulary.
UNIT = reparse_report.UNIT
PHASES = reparse_report.PHASES
COUNT_PASSES = reparse_phases.COUNT_PASSES
# perf prices a whole process, so its cross-check runs a short pass of its
# own rather than the amplified timed one.
PERF_PASSES = 200
# Fixed amplification, and the warm-up that precedes it. Chosen so the
# reported share and ms/file stay readable run to run: measured on the
# shared seeding box, four runs at 5000 passes spread a phase share over
# about 2 points, four at 20000 over about 2, and four at 60000 over
# about 0.5 — while the pass TOTAL moved 17% between the same runs,
# which is why a share is the only time-based reading here and not a
# total. Since issue #513 that spread gates nothing, so the number sets
# how steady the TELEMETRY reads, not whether a run passes; the gate's
# own instrument is the exact count, which needs no amplification at all.
# 60000 passes is roughly 21 CPU seconds, about 3% of this job's budget.
PASSES = 60000
# 600 warm-up passes is past the knee: an un-warmed pass costs about 15%
# more, while 2000 warm-up passes measure the same shares as 400.
WARMUP_PASSES = 600
PERF_TIMEOUT_S = 120
# The surface gate is one counted pass over the corpus in a fresh child:
# no amplification, so it is bounded by the corpus, not by a budget.
_QUANTUM = Decimal('0.1')


def _pin_corpus_env():
    """Point the R2 client at the committed mirror, unconditionally.

    Called at import (before the backend modules are read, so no import
    can observe another endpoint) and again from ``corpus()``, so a
    process that changed the environment after importing the bench still
    measures the fixture.
    """
    # `as_uri()`, never an f-string. On Windows a path is `C:\\mirror`, and
    # f'file://{path}' then reads as the URL `file://C:\mirror` — a HOST named
    # C: with an EMPTY path, which r2._is_file_mode() hands the walk as the
    # bucket root and it refuses ("bucket root '' is not a directory"). Every
    # Windows leg of the matrix failed on exactly that before this line was
    # as_uri(); r2.py's own comment on Windows file URLs names the shape
    # (file:///C:/mirror/) and as_uri() is what produces it.
    os.environ['CLAUDIT_R2_ENDPOINT'] = MIRROR.as_uri()
    os.environ['CLAUDIT_R2_BUCKET'] = BUCKET


_pin_corpus_env()

# pylint: disable-next=wrong-import-position
from backend import ingest_fetch, ingest_scan, r2  # noqa: E402


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


def share_of(cpu_s: float, total_cpu_s: float) -> Decimal:
    """One phase's percent of the run's own CPU, to one decimal place."""
    if total_cpu_s <= 0:
        raise ValueError(
            f'the pass measured {total_cpu_s} CPU s: nothing to share')
    share = (Decimal(repr(cpu_s)) / Decimal(repr(total_cpu_s))) * 100
    return share.quantize(_QUANTUM)


def measure(entries: list[Entry], passes: int = PASSES,
            warmup: int = WARMUP_PASSES,
            instruction_counts=None, instruction_per_file=None,
            instruction_note: str = '', perf_per_file: int | None = None,
            perf_note: str = '') -> Measurement:
    """Time `passes` passes over the corpus and split the CPU by phase.

    The instruction counts travel with the measurement rather than being
    derived from it: they come from a separate, much shorter counted run,
    because the callback that makes a count exact costs about four times
    the CPU it measures (measured on this corpus) and would distort every
    time-based share if both ran in one region.
    """
    if passes <= 0:
        raise ValueError(f'passes must be positive: {passes}')
    if warmup < 0:
        raise ValueError(f'warmup must not be negative: {warmup}')
    if not entries:
        raise ValueError(f'no transcripts to parse under {MIRROR}')
    totals = {'sniff': 0.0, 'parse_file': 0.0, 'sidecar': 0.0}
    with reparse_phases.instrumented(totals):
        for _ in range(warmup):
            run_pass(entries)
        for name in totals:
            totals[name] = 0.0
        records = 0
        start = time.process_time()
        for _ in range(passes):
            records = run_pass(entries)
        total = time.process_time() - start
    phase_cpu_s = reparse_phases.partition(
        total, totals['sniff'], totals['parse_file'], totals['sidecar'])
    shares = {name: share_of(phase_cpu_s[name], total) for name in PHASES}
    return Measurement(total, len(entries), passes, records, phase_cpu_s,
                       shares, instruction_counts, instruction_per_file,
                       instruction_note, perf_per_file, perf_note)


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


def check_surface() -> int:
    """The parse-surface gate (issue #503), as a fresh child.

    Always a child, like the measurement: several of the surface's
    functions sit behind an ``lru_cache``, so a process that had already
    parsed the corpus once would see the cached path and report a function
    the corpus does in fact walk.
    """
    done = subprocess.run(  # pylint: disable=subprocess-run-check
        [sys.executable, str(SURFACE_GATE)],
        capture_output=True, text=True, timeout=SURFACE_TIMEOUT_S, check=False)
    sys.stderr.write(done.stderr or '')
    return done.returncode


def _child_measurement(path: Path, passes: int, warmup: int,
                       count_passes: int) -> Measurement:  # noqa: D401
    """One child's whole reading: the timed pass and the counted pass."""
    entries = corpus()
    counts = reparse_phases.measure_counts(
        entries, run_pass, passes=count_passes)
    per_file = (reparse_phases.bytecodes_per_file(counts)
                if counts.available else None)
    return measure(entries, passes=passes, warmup=warmup,
                   instruction_counts=reparse_report.counts_payload(counts),
                   instruction_per_file=per_file,
                   instruction_note=counts.reason)


def measure_in_child(passes: int = PASSES,
                     warmup: int = WARMUP_PASSES,
                     count_passes: int = COUNT_PASSES,
                     perf_passes: int = PERF_PASSES) -> Measurement:
    """The bench's measurement, taken in a fresh child process.

    Always a child, so the reading starts from the same fresh interpreter
    every time. The child takes both instruments: the timed pass that
    gives each phase its share, and the much shorter counted pass that
    gives each phase its bytecodes.

    `perf stat`'s machine-instruction count rides along as a CROSS-CHECK
    on the counting instrument and nothing more: it needs a counter
    facility a runner may not have, it prices the whole process rather
    than a phase, and the recorded families must not depend on it.
    """
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        path = root / 'measurement.json'
        measurement = _run_counting_child(root, path, passes, warmup,
                                          count_passes)
        perf_total, _unused, perf_note = _perf_child(
            root, 'perf', perf_passes, 0)
        perf_base, _unused2, perf_base_note = _perf_child(root, 'baseline', 1, 0)
        perf_per_file = None
        if perf_total is None:
            perf_note = perf_note or perf_base_note
        elif perf_base is None:
            perf_note = perf_base_note
        else:
            perf_per_file = ((perf_total - perf_base)
                             // max(perf_passes * measurement.files, 1))
            perf_note = 'net of a startup-and-imports baseline run'
        return measurement._replace(
            perf_per_file=perf_per_file, perf_note=perf_note)


def _run_counting_child(root: Path, path: Path, passes: int, warmup: int,
                        count_passes: int) -> Measurement:
    """The child's own reading, without perf: both instruments, in one
    fresh interpreter."""
    command = [sys.executable, str(Path(__file__).resolve()), '--child',
               '--write', str(path), '--passes', str(passes),
               '--warmup', str(warmup), '--count-passes', str(count_passes)]
    done = subprocess.run(  # pylint: disable=subprocess-run-check
        command, capture_output=True, text=True, timeout=PERF_TIMEOUT_S,
        check=False)
    if done.returncode != 0 or not path.exists():
        raise ValueError('the bench run failed: '
                         f'{(done.stderr or "").strip()[:200]}')
    return reparse_report.measurement_from_file(path)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', action='store_true',
                        help=argparse.SUPPRESS)
    parser.add_argument('--write', type=Path,
                        help='write the measurement to this JSON file')
    parser.add_argument('--machine', action='store_true',
                        help='print the reading as key=value fields')
    parser.add_argument('--report', type=Path, metavar='MEASUREMENT',
                        help='print the human report of a written '
                             'measurement, measuring nothing')
    parser.add_argument('--passes', type=int, default=PASSES,
                        help='timed passes over the corpus')
    parser.add_argument('--warmup', type=int, default=WARMUP_PASSES,
                        help='untimed passes run before the clock starts')
    parser.add_argument('--count-passes', type=int, default=COUNT_PASSES,
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
                _child_measurement(args.write, args.passes, args.warmup,
                                   args.count_passes))
            return 0
        if args.report is not None and args.write is None:
            # Printing a measurement already on disk, measuring nothing:
            # re-measuring would print a different run's numbers.
            print(reparse_report.report(
                reparse_report.measurement_from_file(args.report)))
            return 0
        measurement = measure_in_child(args.passes, args.warmup,
                                       args.count_passes)
        if args.write is not None:
            reparse_report.write_measurement(args.write, measurement)
        print(reparse_report.machine_line(measurement) if args.machine
              else reparse_report.report(measurement))
        # The measured number and the surface gate are one verdict: a
        # corpus that stopped exercising a parse path is a worse
        # measurement than no measurement, because the number it produced
        # no longer means what the bench says it means.
        return check_surface()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
