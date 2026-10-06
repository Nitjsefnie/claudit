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
    # `(( … ))` is arithmetic unless its body re-lexes; the ambiguous
    # lone-nested spelling refuses instead of inventing a target.
    ("( ( echo x > deep.txt ) )", (None, [], [])),
    ("((x > y))", (None, [], [])),
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
])
def test_subshell_refusals_stay_conservative(command):
    assert scan(command, "/w") == (None, [], [])


@pytest.mark.parametrize("depth,expected", [
    (32, (None, [], ["/w/f.txt"])),
    (33, (None, [], [])),
])
def test_nested_capture_depth_bound(depth, expected):
    # Captures nest where groups cannot (a lone-nested group refuses
    # before depth matters), so the bound is driven on the capture path.
    command = "echo x > f.txt"
    for k in range(depth):
        command = f"d{k}=$(" + command + ")"
    assert scan(command, "/w") == expected


def test_deeply_nested_groups_stay_bounded():
    assert scan("(" * 100_000 + ")" * 100_000, "/w") == (None, [], [])
