#!/usr/bin/env python3
"""Validate four coverage shards and publish only a complete combination."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from coverage import Coverage, CoverageData
from coverage.exceptions import CoverageException

SUMMARY = re.compile(
    r"Combined (\d+) files?(?:, skipped (\d+))?(?:, (\d+) files? errored)?")


def _validated_files(directories: list[Path]) -> list[Path]:
    if len(directories) != 4 or len({directory.resolve() for directory in directories}) != 4:
        raise ValueError("Expected four distinct coverage shard directories")
    files = []
    for shard, directory in enumerate(directories, 1):
        matches = sorted(directory.glob(".coverage.*"))
        if len(matches) != 1 or not matches[0].is_file():
            raise ValueError(f"shard {shard} ({directory}): expected exactly one coverage file")
        if matches[0].stat().st_size == 0:
            raise ValueError(f"shard {shard} ({matches[0]}): empty coverage data file")
        data = CoverageData(basename=str(matches[0]))
        try:
            data.read()
        except (CoverageException, OSError) as exc:
            raise ValueError(f"shard {shard} ({matches[0]}): unreadable coverage data: {exc}") from exc
        finally:
            data.close()
        files.append(matches[0])
    return files


def _combine_one(path: Path, output: Path, shard: int) -> int:
    command = [sys.executable, "-m", "coverage", "combine", "--keep", f"--data-file={output}"]
    if shard > 1:
        command.append("--append")
    result = subprocess.run([*command, str(path)], capture_output=True, text=True, check=False)
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    summaries = [match for line in (result.stdout + "\n" + result.stderr).splitlines()
                 if (match := SUMMARY.fullmatch(line))]
    if result.returncode or len(summaries) != 1:
        raise ValueError(f"shard {shard} ({path}): coverage combine failed or omitted its summary")
    counts = [int(value or 0) for value in summaries[0].groups()]
    if counts != [1, 0, 0]:
        raise ValueError(f"shard {shard} ({path}): incomplete combine: "
                         f"combined={counts[0]}, skipped={counts[1]}, errored={counts[2]}")
    return counts[0]


def validate_and_combine(directories: list[Path]) -> None:
    """Open every input before combining and preserve the inputs for diagnosis."""
    files = _validated_files(directories)
    output = Path(Coverage().get_data().base_filename())
    with tempfile.TemporaryDirectory(prefix=".coverage-shards-", dir=output.parent) as temporary:
        staged = Path(temporary) / output.name
        combined = 0
        # Separate invocations consume identical valid datasets without duplicate skips.
        for shard, path in enumerate(files, 1):
            combined += _combine_one(path, staged, shard)
        if combined != len(files):
            raise ValueError(f"Expected {len(files)} combined coverage shards, got {combined}")
        os.replace(staged, output)
    print(f"Validated and combined {combined} coverage shards")


def main() -> int:
    """Combine four shard directories, reporting validation failures on stderr."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-and-combine", nargs="+", type=Path, required=True, metavar="DIR")
    args = parser.parse_args()
    try:
        validate_and_combine(args.validate_and_combine)
    except (CoverageException, OSError, ValueError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
