#!/usr/bin/env python3
"""Run a command; stop it only once the paths it writes stop changing.

Contract: tests/test_ci_progress_watchdog.py. Every --sample seconds the
watchdog sums the sizes of the files under each --watch path; any change is
progress. After --stall seconds without one it sends SIGTERM to the command's
process group, SIGKILL --grace seconds later, waits at most --grace more, and
exits 124 as timeout(1) does. Otherwise it exits with the command's status.
A TERM, INT or HUP it receives stops the command the same way and exits
128 plus that signal's number.
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
    """A signal the watchdog received; it ends the command."""


def footprint(paths: list[str]) -> int:
    """Total size of the files under `paths`; a file gone mid-walk counts 0."""
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
    """TERM the command's group, KILL it after `grace`, then wait at most `grace`."""
    for sig in SIGNALS:
        signal.signal(sig, signal.SIG_IGN)  # a second signal must not orphan the command
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            return
        try:
            child.wait(grace)
            if sig == signal.SIGTERM:
                os.killpg(child.pid, signal.SIGKILL)  # the group's other members
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
        # Not `with`: its exit would wait without a bound on a command that outlived SIGKILL.
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
