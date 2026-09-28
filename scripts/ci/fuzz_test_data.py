#!/usr/bin/env python3
"""Fuzz SV-TEST-DATA with valid pricing changes and the full suite.

Each iteration restores a clean baseline, appends five independently
drawn non-negative fields to a seeded nonempty subset of rows, and stamps
each entry one second after its row's newest instant. Offset spellings
exercise issue #264; the written document uses canonical JSON layout.
A seeded chance also adds one model or provider-host row under the
reserved `zz-fuzz-local/<seeded-suffix>` namespace. Existing-row appends
simulate the refresh bot. New rows stay in this namespace because a REAL
model addition is a maintainer edit that updates tests in the same commit;
SV-TEST-DATA's own invariant is against unannounced moves, a boundary this
branch records deliberately. No real data or live test uses this namespace.

The backend loader validates each document before the full suite runs.
Failures save the document as `fuzz-fail-<i>.json` and report the seed,
changed row keys and pytest tail. The run refuses a dirty pricing file,
then restores the original bytes after the run, green or failing.

Run `python3 scripts/ci/fuzz_test_data.py [--iterations N] [--seed N]
[--jobs J]`. Sharded runs use the same global iteration seeds as sequential
runs. Each temporary snapshot includes git-visible files and every regular
file under `tests/`, including nested ignored tests, so each shard collects
the same suite. Non-regular entries under `tests/` refuse sharding.
"""
from __future__ import annotations

import argparse
import json
import os
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
# The only names pruned from the collection-root union: never tests.
PRUNED_TEST_DIRS = frozenset({"__pycache__", ".pytest_cache"})
DEFAULT_ITERATIONS = 200
OFFSET_CHANCE = 0.25
# Believable timezone offsets; (0, 0) is the +00:00 spelling, which
# exercises the same parse path as a named offset.
OFFSET_POOL = ((5, 30), (-5, 0), (2, 0), (-8, 0), (10, 45), (0, 0))
NEW_ROW_CHANCE = 0.25
RESERVED_NAMESPACE = "zz-fuzz-local/"
NEW_ROW_NOTE = "sv-test-data fuzz: reserved new row"
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
    """Restore, perturb, validate and run one iteration."""
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
    new_row_key = _maybe_add_new_row(doc, doc_max, rng, base_seed, iteration)
    pricing.load_tables(doc)
    pricing_path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    suite = run_suite(repo_root)
    result: dict = {"iteration": iteration, "seed": base_seed,
                    "ok": suite[0] == 0, "rows_touched": len(touched),
                    "keys": [key for key, _entries in touched],
                    "output": suite[1] if suite[0] else ""}
    if new_row_key is not None:
        result["keys"].append(new_row_key)
    result["rows_touched"] = len(result["keys"])
    if suite[0] != 0:
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


def _maybe_add_new_row(doc: dict, doc_max: datetime | None,
                       rng: random.Random, base_seed: int,
                       iteration: int) -> str | None:
    """With a seeded chance, add one valid row in the reserved namespace."""
    if rng.random() >= NEW_ROW_CHANCE:
        return None

    key = (f"{RESERVED_NAMESPACE}{base_seed}-{iteration}-"
           f"{rng.getrandbits(64):016x}")
    while (key in doc["models"] or key in doc["providers"]
           or any(key in hosts for hosts in doc["providers"].values())):
        key = (f"{RESERVED_NAMESPACE}{base_seed}-{iteration}-"
               f"{rng.getrandbits(64):016x}")

    is_host = rng.choice((False, True))
    entry = {"from": None, "note": NEW_ROW_NOTE,
             **{field: rng.choice(VALUE_POOL)
                for field in pricing.RATE_FIELDS}}
    if is_host and doc_max is not None and rng.random() < 0.5:
        entry["from"] = (doc_max + timedelta(seconds=1)).astimezone(
            timezone.utc).strftime(STAMP_FORMAT)
    if not is_host:
        doc["models"][key] = [entry]
        return key

    model = rng.choice(list(doc["providers"])) if doc["providers"] else key
    doc["providers"].setdefault(model, {})[key] = [entry]
    return f"{model} via {key}"


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
    completed = [(shard_dir, result_file, child, child.wait())
                 for shard_dir, result_file, child in pending]
    results: list[dict] = []
    for shard_dir, result_file, _child, code in completed:
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
    """A disposable git checkout of the tree AS GIT KNOWS IT — tracked
    files plus untracked, unignored ones (`git ls-files -co
    --exclude-standard`) — so a shard runs committed and uncommitted
    work alike, exactly what the sequential run would: a HEAD-only
    clone would silently drop uncommitted tests or code edits and
    could report a false green over stale code. Only listed files are
    copied, so ignored runtime artifacts — a live socket, a fifo, a
    pid file — can neither break the copy nor reach a shard. The copy
    becomes its own git repository whose snapshot commit is the
    per-iteration restore's baseline."""
    listing = subprocess.run(
        ["git", "-C", str(source), "ls-files", "-z", "-co",
         "--exclude-standard"],
        check=True, capture_output=True, text=True).stdout
    dest.mkdir(parents=True)
    for name in listing.split("\x00"):
        if not name:
            continue
        src = source / name
        dst = dest / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
        elif src.is_file():
            shutil.copyfile(src, dst)
    _snapshot_collection_root(source, dest)
    _git(dest, "init", "-q")
    _git(dest, "add", "-A")
    _git(dest, "-c", "user.name=fuzz test", "-c",
         "user.email=fuzz@localhost", "commit", "-q", "-m",
         "fuzz shard baseline")


def _snapshot_collection_root(source: Path, dest: Path) -> None:
    """The collection-root union: every regular file under tests/ that
    the git-known set may have missed is copied too — a NESTED test
    directory is invisible to a deny-by-default .gitignore, so git
    ignores it, yet pytest collects it; a shard without it would run a
    smaller suite than the sequential run. Only __pycache__ and
    .pytest_cache are pruned; any other non-regular entry under tests/
    refuses the shard loudly: it cannot be snapshotted, so the
    population guarantee would fail silently. Outside tests/ nothing
    extra is walked."""
    tests_root = source / "tests"
    if not tests_root.is_dir():
        return
    for current, dirs, files in os.walk(tests_root):
        here = Path(current)
        dirs[:] = [d for d in dirs if d not in PRUNED_TEST_DIRS]
        for d in dirs:
            if (here / d).is_symlink():
                raise SystemExit(_shard_refusal(here / d, source))
        for name in files:
            src = here / name
            dst = dest / src.relative_to(source)
            if src.is_symlink() or not src.is_file():
                raise SystemExit(_shard_refusal(src, source))
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)


def _shard_refusal(path: Path, repo_root: Path) -> str:
    """Format the refused path as a repository-relative POSIX path."""
    display_path = (path.relative_to(repo_root).as_posix()
                    if path.is_relative_to(repo_root)
                    else path.absolute().as_posix())
    return (f"fuzz: refusing to shard: {display_path} is not a "
            "regular file, so it cannot be snapshotted and the shard "
            "would silently miss it; remove it or run without --jobs")


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
