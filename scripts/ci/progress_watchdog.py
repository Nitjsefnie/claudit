#!/usr/bin/env python3
"""Run a command; stop it, exit 124, once the --watch paths' total size holds for --stall s.

Contract: tests/test_ci_progress_watchdog.py. A signal received stops it too, exit 128 + N.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

STALLED = 124
SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


class Stop(Exception):
    """A signal received."""


def footprint(paths: list[str]) -> int:
    total = 0
    for path in paths:
        files = [path] if not os.path.isdir(path) else (
            os.path.join(root, name) for root, _, names in os.walk(path) for name in names)
        for name in files:
            try:
                total += os.lstat(name).st_size
            except OSError:
                pass
    return total


def stop(child: subprocess.Popen, grace: float) -> None:
    """TERM the group; KILL it, then wait, `grace` s apart."""
    for sig in SIGNALS:
        signal.signal(sig, signal.SIG_IGN)  # a second must not orphan it
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            return
        try:
            child.wait(grace)
            if sig == signal.SIGTERM:
                os.killpg(child.pid, signal.SIGKILL)
            return
        except subprocess.TimeoutExpired:
            continue
        except ProcessLookupError:
            return
    print(f"progress_watchdog.py: process group {child.pid} outlived SIGKILL", file=sys.stderr)


def _raise(signum: int, _frame) -> None:
    raise Stop(signum)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    for name in ("--stall", "--sample", "--grace"):
        parser.add_argument(name, type=float, required=True)
    parser.add_argument("--watch", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[args.command[:1] == ["--"]:]
    if not command or min(args.stall, args.sample, args.grace) <= 0:
        parser.error("needs a command after -- and --stall, --sample and --grace above 0")
    for sig in SIGNALS:
        signal.signal(sig, _raise)
    child = None
    try:
        seen = footprint(args.watch)
        # Not `with`: its exit waits unbounded on a command that outlives SIGKILL.
        child = subprocess.Popen(command, start_new_session=True)  # pylint: disable=consider-using-with
        quiet_since = time.monotonic()
        while True:
            try:
                status = child.wait(args.sample)
                return status if status >= 0 else 128 - status
            except subprocess.TimeoutExpired:
                pass
            now = footprint(args.watch)
            if now != seen:
                seen, quiet_since = now, time.monotonic()
            elif time.monotonic() - quiet_since >= args.stall:
                break
    except Stop as received:
        if child:
            stop(child, args.grace)
        return 128 + received.args[0]
    stop(child, args.grace)
    print(f"progress_watchdog.py: {command[0]} wrote nothing to {' '.join(args.watch) or 'no watched path'}"
          f" for {args.stall:g} s; stopped it", file=sys.stderr)
    return STALLED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
