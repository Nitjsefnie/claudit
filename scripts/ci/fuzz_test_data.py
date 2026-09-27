#!/usr/bin/env python3
"""Fuzz SV-TEST-DATA: random VALID appends to src/pricing.json, then
the full suite.

The perturbed-data CI leg (scripts/ci/perturb_test_data.py) runs three
fixed perturbations; this harness is the open-ended half. Per
iteration it restores the pristine document, appends to each row of a
seeded random NONEMPTY subset of rate rows ONE valid appended entry —
the five rate fields drawn INDEPENDENTLY from a pool mixing zero, one,
ordinary magnitudes and repr-awkward floats — stamps it one second
past the row's newest real instant, occasionally re-spelling that same
LATER instant with a ±HH:MM offset (the normalisation issue #264
fixed), writes the canonical layout, validates the document through
the backend's own loader, and runs the FULL suite against the result.
A test whose verdict is not append-invariant fails as a test failure
here, with the failing document saved for a repro.

Per iteration:
  1. `git checkout -- src/pricing.json` — a clean baseline each time,
     whatever the previous iteration appended.
  2. Append one entry per touched row; the canonical layout
     json.dumps(doc, indent=2, sort_keys=True) + "\\n" is written back.
  3. Validate through `pricing.load_tables` before anything runs.
  4. Run the full suite (python3 -m pytest tests/ -q --tb=short -ra).
     The environment passes through to the pytest child unchanged —
     the private-Postgres variables among them; nothing is hardcoded.
  5. A failing suite saves src/pricing.json to fuzz-fail-<i>.json in
     the artifact directory, prints the iteration, the seed, the row
     keys touched and the pytest tail, and the run exits 1. Every
     iteration green prints one summary line and exits 0.

The run refuses to START when src/pricing.json carries local changes:
the per-iteration restore is `git checkout --`, which would silently
discard them. The file's baseline is recorded at start and restored in
a `finally` — after any failure artifact is saved — so the tree is
left exactly as it was found, green or failing.

    python3 scripts/ci/fuzz_test_data.py [--iterations N] [--seed N]
        [--jobs J]

With --jobs J > 1 the SAME iterations run in J child processes, each
in its own disposable copy of the tree under a temp dir: iteration i
is served by shard i % J, and every child derives its per-iteration
randomness from (base seed, GLOBAL iteration number), so a sharded run
covers exactly the iterations — the same draws — the sequential run
would, merged in iteration order. A shard's copy is a snapshot of the
WORKING tree — committed and uncommitted content alike — turned into
its own git repository whose snapshot commit is the restore's
baseline; a HEAD-only clone would silently drop uncommitted tests or
code edits from every shard and could report a false green over stale
code. The run's temp dir goes away when the run does; the failing
documents are saved OUTSIDE it, in the artifact directory.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# pylint: disable=wrong-import-position
from backend import pricing  # noqa: E402

PRICING_REL = Path("src") / "pricing.json"
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
NOTE = "sv-test-data fuzz: independent field draws"
TAIL_LINES = 30
DEFAULT_ITERATIONS = 200
OFFSET_CHANCE = 0.25
# Believable timezone offsets; (0, 0) is the +00:00 spelling, which
# exercises the same parse path as a named offset.
OFFSET_POOL = ((5, 30), (-5, 0), (2, 0), (-8, 0), (10, 45), (0, 0))
# The five fields draw INDEPENDENTLY and uniformly from this pool:
# zero, one, ordinary magnitudes, and the repr-awkward floats the #263
# repros and the #232 tolerance sweep turn on. An entry may keep an
# int: the loader takes both, and json renders 0 and 0.0 distinctly.
VALUE_POOL = (
    0, 1.0, 0.1, 0.25, 2.0, 3.5, 12.0, 60.0, 750.0,
    0.06974999999999999, 2.9999999999999996, 1e-4, 12345.6789,
    0.30000000000000004, 5.000000000000001,
)


def fuzz_iteration(repo_root: Path, iteration: int, base_seed: int,
                   artifact_dir: Path) -> dict:
    """One iteration: restore, append, validate, run the suite.

    Returns the iteration's result record: {iteration, seed, ok,
    rows_touched, keys, output, artifact?}. `output` is carried only
    for a failing suite — the report's tail — and `artifact` names the
    saved failing document, present only when the suite failed.
    """
    restore_baseline(repo_root)
    pricing_path = repo_root / PRICING_REL
    doc = json.loads(pricing_path.read_text(encoding="utf-8"))
    rng = iteration_rng(base_seed, iteration)
    rows = _rows(doc)
    if not rows:
        raise ValueError(f"{pricing_path}: no rate rows under "
                         "models/providers; fuzzing nothing proves nothing")
    doc_max = _max_real_stamp(rows)
    touched = _choose_rows(rows, rng)
    for _key, entries in touched:
        entries.append(_appended_entry(entries, doc_max, rng))
    pricing.load_tables(doc)
    pricing_path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    code, output = run_suite(repo_root)
    result: dict = {"iteration": iteration, "seed": base_seed,
                    "ok": code == 0, "rows_touched": len(touched),
                    "keys": [key for key, _entries in touched],
                    "output": output if code else ""}
    if code != 0:
        result["artifact"] = str(
            artifact_dir / f"fuzz-fail-{iteration}.json")
        Path(result["artifact"]).write_text(
            pricing_path.read_text(encoding="utf-8"), encoding="utf-8")
    return result


def fuzz_run(repo_root: Path, base_seed: int, count: int, artifact_dir: Path,
             first: int = 0, step: int = 1) -> list[dict]:
    """Run this process's iterations in order; stop at the first
    failing suite, whose report is printed as it happens."""
    results = []
    for n in range(count):
        iteration = first + n * step
        result = fuzz_iteration(repo_root, iteration, base_seed, artifact_dir)
        results.append(result)
        print(f"fuzz: iteration {iteration}: "
              f"{'ok' if result['ok'] else 'FAIL'}, "
              f"{result['rows_touched']} rows touched", flush=True)
        if not result["ok"]:
            _print_failure(result)
            break
    return results


def iteration_rng(base_seed: int, iteration: int) -> random.Random:
    """The iteration's own generator, keyed on the base seed and the
    GLOBAL iteration number: a sharded run and the sequential run serve
    a given iteration the same draws."""
    return random.Random(f"{base_seed}:{iteration}")


def restore_baseline(repo_root: Path) -> None:
    """`git checkout -- src/pricing.json`: whatever the previous
    iteration appended is gone; the tracked document is the baseline."""
    _git(repo_root, "checkout", "--", PRICING_REL.as_posix())


def _git(target: Path, *args: str) -> None:
    """One git command in `target`, failing loudly on a nonzero exit."""
    subprocess.run(["git", "-C", str(target), *args], check=True,
                   capture_output=True, text=True)


def _require_clean_pricing(repo_root: Path) -> None:
    """Refuse to start over a pricing document that carries local
    changes: the per-iteration restore is `git checkout --`, which
    would silently discard them, and the run's own rewrites would sit
    on top of edits the operator might mistake for the baseline."""
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain", "--",
         PRICING_REL.as_posix()],
        capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(
            f"fuzz: refusing to run: {repo_root} is not a usable git "
            f"checkout ({proc.stderr.strip()})")
    if proc.stdout.strip():
        raise SystemExit(
            "fuzz: refusing to run: src/pricing.json carries local "
            "changes; commit or restore them first — the harness "
            "restores and rewrites that file every iteration")


def run_suite(repo_root: Path) -> tuple[int, str]:
    """The full suite, as the perturbed CI leg runs it. The environment
    passes through to the pytest child unchanged — the private-Postgres
    variables among them — so nothing about the database is hardcoded
    here. Returns (exit code, combined output)."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short",
         "-ra"],
        cwd=str(repo_root), capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout + proc.stderr


def _appended_entry(entries: list[dict], doc_max: datetime | None,
                    rng: random.Random) -> dict:
    """One valid appended entry for the row: five independent pool
    draws, stamped one second past the row's floor instant."""
    instant = _floor_instant(entries, doc_max) + timedelta(seconds=1)
    return {"from": _spell(instant, rng),
            **{field: rng.choice(VALUE_POOL)
               for field in pricing.RATE_FIELDS},
            "note": NOTE}


def _spell(instant: datetime, rng: random.Random) -> str:
    """The appended stamp: Z, or — a quarter of the time, seeded — the
    same later instant re-spelled with a ±HH:MM offset (issue #264's
    normalisation: the offset spelling carries the instant, never the
    offset-local wall time mislabeled)."""
    if rng.random() < OFFSET_CHANCE:
        hours, minutes = rng.choice(OFFSET_POOL)
        return instant.astimezone(
            timezone(timedelta(hours=hours, minutes=minutes))).isoformat()
    return instant.astimezone(timezone.utc).strftime(STAMP_FORMAT)


def _floor_instant(entries: list[dict],
                   doc_max: datetime | None) -> datetime:
    """The instant the appended entry must follow: the row's own newest
    real `from`, else the document's newest real `from` — a row whose
    only entry is null-from has nothing older of its own — else the
    wall clock, a document where NOTHING is dated having nothing older
    to protect."""
    newest = entries[-1].get("from")
    if newest is not None:
        return _parse_stamp(newest)
    if doc_max is not None:
        return doc_max
    return datetime.now(timezone.utc).replace(microsecond=0)


def _choose_rows(rows: list[tuple[str, list]], rng: random.Random
                 ) -> list[tuple[str, list]]:
    """A seeded random NONEMPTY subset of the rows."""
    return rng.sample(rows, rng.randint(1, len(rows)))


def _rows(doc: dict) -> list[tuple[str, list]]:
    """Every rate row as (row key, entry list): models first, then
    hosts — the perturber's own row shape."""
    rows = list(doc["models"].items())
    for model, hosts in doc["providers"].items():
        rows.extend((f"{model} via {host}", entries)
                    for host, entries in hosts.items())
    return rows


def _parse_stamp(text: str) -> datetime:
    """A `from` stamp as a UTC instant."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _max_real_stamp(rows: list[tuple[str, list]]) -> datetime | None:
    """The document's newest real `from`, over every entry of every
    row; None when no entry carries one."""
    stamps = [_parse_stamp(entry["from"])
              for _key, entries in rows for entry in entries
              if entry.get("from") is not None]
    return max(stamps) if stamps else None


def _shards(total: int, jobs: int) -> list[tuple[int, int, int]]:
    """(first, step, count) per shard, round-robin: shard j owns the
    iterations j, j + jobs, ... The union over the shards is
    range(total), each iteration exactly once; `count` is 0 for a
    shard with more shards than iterations."""
    return [(j, jobs, (total - j + jobs - 1) // jobs) for j in range(jobs)]


def _run_sharded(repo_root: Path, base_seed: int, total: int, jobs: int,
                 artifact_dir: Path) -> list[dict]:
    """The iterations sharded across J children, each in its own clone
    of the tree; results merged in iteration order."""
    script = Path(__file__).resolve()
    with tempfile.TemporaryDirectory(prefix="fuzz-test-data-") as tmp:
        pending: list[tuple[Path, Path, subprocess.Popen]] = []
        for first, step, count in _shards(total, jobs):
            if not count:
                continue
            shard_dir = Path(tmp) / f"shard-{first}"
            _snapshot_tree(repo_root, shard_dir)
            result_file = shard_dir / "shard-result.json"
            child = subprocess.Popen(  # pylint: disable=consider-using-with
                [sys.executable,
                 str(shard_dir / "scripts" / "ci" / script.name),
                 "--iterations", str(count),
                 "--iteration-first", str(first),
                 "--iteration-step", str(step),
                 "--seed", str(base_seed),
                 "--artifact-dir", str(artifact_dir),
                 "--result-file", str(result_file)],
                cwd=str(shard_dir))
            pending.append((shard_dir, result_file, child))
        merged = _collect_shards(pending)
    merged.sort(key=lambda result: result["iteration"])
    return merged


def _collect_shards(
        pending: list[tuple[Path, Path, subprocess.Popen]]) -> list[dict]:
    """Wait for every shard child and gather its results, failing
    loudly on a child that died without writing its result file."""
    results: list[dict] = []
    for shard_dir, result_file, child in pending:
        code = child.wait()
        # Exit 1 is the child's documented "failing suite" exit, with
        # its results written; only a child that died WITHOUT them is
        # a crash worth raising on.
        if code not in (0, 1) or not result_file.exists():
            raise RuntimeError(
                f"fuzz shard {shard_dir.name}: child exited {code} "
                "without a result file")
        shard = json.loads(result_file.read_text(encoding="utf-8"))
        results.extend(shard["results"])
    return results


def _snapshot_tree(source: Path, dest: Path) -> None:
    """A disposable git checkout of the tree AS IT IS — committed and
    uncommitted content alike — so a shard runs exactly what the
    sequential run would: a HEAD-only clone would silently drop
    uncommitted tests or code edits from every shard and could report
    a false green over stale code. The copy becomes its own git
    repository whose snapshot commit is the per-iteration restore's
    baseline."""
    shutil.copytree(
        source, dest,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc",
                                      ".pytest_cache"))
    _git(dest, "init", "-q")
    _git(dest, "add", "-A")
    _git(dest, "-c", "user.name=fuzz test", "-c",
         "user.email=fuzz@localhost", "commit", "-q", "-m",
         "fuzz shard baseline")


def _print_failure(result: dict) -> None:
    """The failure report: iteration, seed and its derivation, the row
    keys touched, the artifact, and the pytest tail."""
    print(f"fuzz: FAIL at iteration {result['iteration']} "
          f"(seed {result['seed']}, derivation "
          f"{result['seed']}:{result['iteration']})")
    print(f"fuzz: rows touched: {', '.join(result['keys'])}")
    print(f"fuzz: failing document saved to {result['artifact']}")
    print(f"fuzz: pytest tail (last {TAIL_LINES} lines):")
    for line in result["output"].splitlines()[-TAIL_LINES:]:
        print(f"  {line}")


def _print_summary(seed: int, results: list[dict]) -> None:
    counts = [result["rows_touched"] for result in results]
    print(f"fuzz OK: {len(results)} iterations green, seed={seed}, "
          f"rows touched per iteration: {counts}")


def main(argv: list[str] | None = None,
         repo_root: Path | None = None) -> int:
    """The operator's entry point: fuzz, run the suite per iteration,
    report. `repo_root` is the tests' seam — the operator's invocation
    always fuzzes the tree this script lives in."""
    parser = argparse.ArgumentParser(
        prog="fuzz_test_data",
        description="Fuzz SV-TEST-DATA: per iteration, restore "
                    "src/pricing.json, append one valid entry to each "
                    "row of a seeded random nonempty subset (five "
                    "independent pool draws per field set, stamped one "
                    "second past the row's newest real instant, "
                    "occasionally with a ±HH:MM offset spelling), "
                    "validate through the backend loader, and run the "
                    "FULL suite. A failing suite saves the failing "
                    "document to fuzz-fail-<i>.json, prints the "
                    "iteration, seed, row keys and pytest tail, and "
                    "exits 1.")
    parser.add_argument("--iterations", type=int,
                        default=DEFAULT_ITERATIONS,
                        help="how many iterations to run "
                             "(default: 200)")
    parser.add_argument("--seed", type=int, default=None,
                        help="the run seed: each iteration derives its "
                             "row subset, field draws and stamp spelling "
                             "from (seed, iteration number) "
                             "(default: the wall clock)")
    parser.add_argument("--jobs", type=int, default=1,
                        help="shard the iterations across J child "
                             "processes, each in its own snapshot of "
                             "the working tree under a temp dir; "
                             "results merge in iteration order "
                             "(default: 1, sequential in this tree)")
    parser.add_argument("--artifact-dir", type=Path, default=Path.cwd(),
                        help="where fuzz-fail-<i>.json lands "
                             "(default: the current directory)")
    parser.add_argument("--iteration-first", type=int, default=0,
                        help=argparse.SUPPRESS)
    parser.add_argument("--iteration-step", type=int, default=1,
                        help=argparse.SUPPRESS)
    parser.add_argument("--result-file", type=Path, default=None,
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations must be at least 1")
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if args.iteration_step < 1:
        parser.error("--iteration-step must be at least 1")
    seed = (args.seed if args.seed is not None
            else int(datetime.now(timezone.utc).timestamp()))
    artifact_dir = args.artifact_dir.resolve()
    root = repo_root if repo_root is not None else REPO_ROOT
    _require_clean_pricing(root)
    baseline = (root / PRICING_REL).read_bytes()
    try:
        if args.jobs == 1:
            results = fuzz_run(root, seed, args.iterations, artifact_dir,
                               first=args.iteration_first,
                               step=args.iteration_step)
        else:
            results = _run_sharded(root, seed, args.iterations, args.jobs,
                                   artifact_dir)
        if args.result_file is not None:
            args.result_file.write_text(
                json.dumps({"seed": seed, "results": results}, indent=2)
                + "\n", encoding="utf-8")
        if not all(result["ok"] for result in results):
            if args.jobs > 1:
                for result in results:
                    if not result["ok"]:
                        _print_failure(result)
            return 1
        _print_summary(seed, results)
        return 0
    finally:
        (root / PRICING_REL).write_bytes(baseline)


if __name__ == "__main__":
    sys.exit(main())
