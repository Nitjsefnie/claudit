"""Compare single-file parser CPU work with a baseline checkout, without ingest.

Example: .venv/bin/python scripts/bench_parser.py --baseline /tmp/baseline \
    --repeat 7 --json /tmp/parser-times.json /path/to/archive/*.jsonl

Workers run sequentially. File reads, imports, hashing and process startup are
outside the parse timer; parser caches are cleared before every file. Complete
outputs must match the baseline on every round. No transcript content is emitted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def _worker(checkout: Path, paths: list[str]) -> list[dict]:
    # Each checkout gets a fresh interpreter so absolute backend imports and
    # module caches cannot accidentally mix the baseline and candidate code.
    sys.path.insert(0, str(checkout))
    from orjson import OPT_SORT_KEYS, dumps  # pylint: disable=import-outside-toplevel
    from backend.parse import parse_file  # pylint: disable=import-outside-toplevel
    from backend.bash_churn import _python_scan  # pylint: disable=import-outside-toplevel

    rows = []
    for name in paths:
        blob = Path(name).read_bytes()
        _python_scan.cache_clear()
        started = time.perf_counter()
        result = parse_file(name, blob)
        elapsed = time.perf_counter() - started
        rows.append({
            "path": name, "bytes": len(blob), "seconds": elapsed,
            "sha256": hashlib.sha256(dumps(result, option=OPT_SORT_KEYS)).hexdigest(),
        })
        del result
    return rows


def _run(checkout: Path, paths: list[str], timeout: int) -> list[dict]:
    command = [sys.executable, str(Path(__file__).resolve()),
               "--worker", str(checkout)]
    completed = subprocess.run(
        command, input=json.dumps(paths), text=True, capture_output=True,
        check=True, timeout=timeout,
    )
    return json.loads(completed.stdout)


def compare(checkout: Path, baseline: Path, paths: list[str],
            repeat: int, timeout: int) -> list[dict]:
    """Warm both versions, then alternate their order on successive rounds."""
    expected = _run(baseline, paths, timeout)
    warm = _run(checkout, paths, timeout)
    _check_outputs(expected, warm)
    samples: dict[str, list[list[dict]]] = {"baseline": [], "candidate": []}
    roots = {"baseline": baseline, "candidate": checkout}
    for index in range(repeat):
        order = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
        for variant in order:
            rows = _run(roots[variant], paths, timeout)
            _check_outputs(expected, rows)
            samples[variant].append(rows)
        print(f"round {index + 1}/{repeat}: all {len(paths)} file outputs match", file=sys.stderr)
    return _summarize(expected, samples)


def _check_outputs(expected: list[dict], actual: list[dict]) -> None:
    for oracle, row in zip(expected, actual, strict=True):
        if (oracle["path"], oracle["sha256"]) != (row["path"], row["sha256"]):
            raise ValueError(f"parser output differs: {row['path']}")


def _summarize(expected: list[dict], samples: dict[str, list[list[dict]]]) -> list[dict]:
    result = []
    for index, oracle in enumerate(expected):
        timings = {variant: [round_rows[index]["seconds"] for round_rows in rounds]
                   for variant, rounds in samples.items()}
        medians = {variant: statistics.median(values) for variant, values in timings.items()}
        result.append({
            **oracle, "seconds": timings, "median_seconds": medians,
            "reduction_pct": 100 * (1 - medians["candidate"] / medians["baseline"]),
        })
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="JSONLs or archive directories")
    parser.add_argument("--baseline", type=Path, help="unchanged checkout to compare")
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--repeat", type=_positive, default=5)
    parser.add_argument("--timeout", type=_positive, default=120, help="seconds per sequential worker")
    parser.add_argument("--json", type=Path, help="save per-file hashes and all timing samples")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker is not None:
        print(json.dumps(_worker(args.worker.resolve(), json.load(sys.stdin))))
        return
    if args.baseline is None or not args.paths:
        parser.error("--baseline and at least one JSONL or archive directory are required")
    paths = sorted({str(file.resolve()) for path in args.paths
                    for file in (path.rglob("*.jsonl") if path.is_dir() else [path])})
    if not paths:
        parser.error("no JSONL files found")
    results = compare(args.checkout.resolve(), args.baseline.resolve(), paths, args.repeat, args.timeout)
    report = {
        "python": sys.version, "baseline": str(args.baseline.resolve()),
        "candidate": str(args.checkout.resolve()), "all_outputs_equal": True,
        "repeat": args.repeat, "files": results,
    }
    if args.json is not None:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for row in results:
        medians = row["median_seconds"]
        print(f"{row['bytes']:>10} bytes  {medians['baseline'] * 1000:9.3f} -> "
              f"{medians['candidate'] * 1000:9.3f} ms  {row['reduction_pct']:6.1f}%  {row['path']}")


if __name__ == "__main__":
    main()
