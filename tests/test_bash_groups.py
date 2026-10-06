"""Subshell and brace-group scanning in Bash read targets (issue #748)."""
from __future__ import annotations

import pytest

from backend.bash_reads import scan


@pytest.mark.parametrize("command,expected", [
    ("(echo x > out.txt)", (None, [], ["/w/out.txt"])),
    ("(cd sub && cat a.py)", ("whole", ["/w/sub/a.py"], [])),
    ("(cat a.py; cat b.py) > c.txt", ("whole", ["/w/a.py", "/w/b.py"], ["/w/c.txt"])),
    ("cat a.py && (echo y > o.txt)", ("whole", ["/w/a.py"], ["/w/o.txt"])),
    ("(cd /sub && cat a.py); cat b.py", ("whole", ["/sub/a.py", "/w/b.py"], [])),
    # `(( … ))` — the unspaced spelling — is bash's arithmetic command
    # and books nothing; the same pair spaced apart is a real nested
    # subshell, whose commands run and book (issue #771).
    ("( ( echo x > deep.txt ) )", (None, [], ["/w/deep.txt"])),
    ("( ( ( echo x > deepest.txt ) ) )", (None, [], ["/w/deepest.txt"])),
    ("( (cat a.py) )", ("whole", ["/w/a.py"], [])),
    ("( (cat a.py) ) > c.txt", ("whole", ["/w/a.py"], ["/w/c.txt"])),
    ("((x > y))", (None, [], [])),
    # The adjacent opener with an UNDOUBLED close never terminates the
    # arithmetic command: bash falls back to re-reading it as a nested
    # subshell, and the tail redirect runs (issue #778).
    ("((x) > wide.txt)", (None, [], ["/w/wide.txt"])),
    ("((cat a.py) > c.txt)", ("whole", ["/w/a.py"], ["/w/c.txt"])),
    # A doubled close still re-lexes when the arithmetic expression
    # carries an unmatched `)` — the parens cannot balance — and bash
    # runs the construct as nested subshells.
    ("((cat a.py); (echo y > f.txt))", ("whole", ["/w/a.py"], ["/w/f.txt"])),
    ("((echo hi); echo y > f.txt)", (None, [], ["/w/f.txt"])),
    ("((x); y)", (None, [], [])),
    # A balancing expression is arithmetic whatever it holds: bash
    # evaluates or refuses to parse, and never touches a file.
    ("((x > y ; z))", (None, [], [])),
    ("(( (x) ; y ))", (None, [], [])),
    ("((x > y && z))", (None, [], [])),
    ("( (cat a.py); echo x > b.txt )", ("whole", ["/w/a.py"], ["/w/b.txt"])),
    ("{ cat f.py; }", ("whole", ["/w/f.py"], [])),
    ("{ (cd /x && cat f.py); cat g.py; }", ("whole", ["/x/f.py", "/w/g.py"], [])),
    ("(cat a.py) | head -c 1", ("slice", ["/w/a.py"], [])),
    ("(cat a.py) < in.txt", ("whole", ["/w/a.py", "/w/in.txt"], [])),
])
def test_subshell_and_group_commands_are_scanned(command, expected):
    """#748: commands inside `( … )` are scanned like those in a
    `{ …; }` group — reads, slice/whole kind and writes all book, a
    `cd` inside stays inside, and a redirect on the closing paren books
    its target."""
    assert scan(command, "/w") == expected


@pytest.mark.parametrize("command", [
    "(cat a.py",            # unclosed group books nothing
    "(cat a.py) foo",       # words after the close: not a redirect
    "(echo x) < in.txt",    # a non-reading group takes no stdin input
    # Issue #771 negative space: the unspaced spelling is arithmetic
    # either way — spaced `(( … ))` included — and its `>` is a
    # comparison, never a redirect, so inventing `o.txt` is a phantom.
    "(( echo x > o.txt ))",
    "(( x + 1 ))",
])
def test_subshell_refusals_stay_conservative(command):
    assert scan(command, "/w") == (None, [], [])


@pytest.mark.parametrize("depth,expected", [
    (32, (None, [], ["/w/f.txt"])),
    (33, (None, [], [])),
])
def test_nested_capture_depth_bound(depth, expected):
    # Captures nest where the unspaced arithmetic spelling cannot be
    # scanned, so the capture path drives the bound.
    command = "echo x > f.txt"
    for k in range(depth):
        command = f"d{k}=$(" + command + ")"
    assert scan(command, "/w") == expected


@pytest.mark.parametrize("depth,expected", [
    (32, (None, [], ["/w/f.txt"])),
    (33, (None, [], [])),
])
def test_nested_group_depth_bound(depth, expected):
    # Spaced nested subshells recurse through nested scans, so the same
    # depth bound holds on the group path.
    command = "echo x > f.txt"
    for _ in range(depth):
        command = "( " + command + " )"
    assert scan(command, "/w") == expected


def test_deeply_nested_groups_stay_bounded():
    assert scan("(" * 100_000 + ")" * 100_000, "/w") == (None, [], [])
