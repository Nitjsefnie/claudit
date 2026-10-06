"""Directory write targets from mkdir, mktemp and git (backend/bash_directories.py)."""
from __future__ import annotations

import pytest

from backend.bash_reads import scan


@pytest.mark.parametrize("command,expected", [
    ("mkdir /tmp/rc", ["/tmp/rc"]),
    ("mkdir -p /tmp/rc", ["/tmp/rc"]),
    ("mkdir rc", ["/work/rc"]),
    ("mkdir -p a b", ["/work/a", "/work/b"]),
    ("mkdir -m 700 secret", ["/work/secret"]),
    ("mkdir --mode=700 secret", ["/work/secret"]),
    ("mkdir -pm700 one two", ["/work/one", "/work/two"]),
    ("mkdir --unknown x", []),
    ("mkdir *.py", []),
    ("mkdir $UNKNOWN/x", []),
    ("FLAGS=' -p'; mkdir $FLAGS x", []),
    ("mkdir -p /tmp/ai-researcher-wt && git worktree add"
     " /tmp/ai-researcher-wt/issue200 -b issue-200 main",
     ["/tmp/ai-researcher-wt", "/tmp/ai-researcher-wt/issue200"]),
])
def test_mkdir_operands_are_write_targets(command, expected):
    assert scan(command, "/work")[2] == expected


@pytest.mark.parametrize("command,expected", [
    ("git worktree add /tmp/wt main", ["/tmp/wt"]),
    ("git worktree add /tmp/wt", ["/tmp/wt"]),
    ("git worktree add -b feat /tmp/wt main", ["/tmp/wt"]),
    ("git worktree add --detach /tmp/wt", ["/tmp/wt"]),
    ("git worktree add --track=inherit /tmp/wt main", ["/tmp/wt"]),
    ("git worktree add", []),
    ("git worktree prune", []),
    ("git worktree list", []),
    ("git worktree add --unknown /tmp/wt", []),
    ("git -C /x worktree add /tmp/wt", []),
    ("git worktree add *.py", []),
    ("git worktree add $UNKNOWN/wt", []),
    ("git worktree add '$D/wt'", ["/work/$D/wt"]),
    ("git log --oneline -5", []),
])
def test_worktree_add_path_is_a_write_target(command, expected):
    assert scan(command, "/work")[2] == expected


@pytest.mark.parametrize("command,expected", [
    ("git clone https://github.com/x/y /tmp/clone-x", ["/tmp/clone-x"]),
    ("git clone https://github.com/x/y", ["/work/y"]),
    ("git clone https://github.com/x/y.git", ["/work/y"]),
    ("git clone git@github.com:x/y.git", ["/work/y"]),
    ("git clone https://github.com/x/y/", ["/work/y"]),
    ("git clone -b main https://github.com/x/y /tmp/clone-x", ["/tmp/clone-x"]),
    ("git clone --origin upstream https://github.com/x/y /tmp/clone-x",
     ["/tmp/clone-x"]),
    ("git clone --depth 1 https://github.com/x/y", ["/work/y"]),
    ("git clone https://github.com/x/y.git /tmp/clone-x", ["/tmp/clone-x"]),
    ("git clone '$U'", ["/work/$U"]),
    ("git clone \"$U\"", []),
    ("git clone -c http.sslVerify=false https://github.com/x/y", ["/work/y"]),
    ("git clone -c http.sslVerify=false https://github.com/x/y /tmp/clone-x",
     ["/tmp/clone-x"]),
    ("git clone --config http.sslVerify https://github.com/x/y /tmp/clone-x",
     []),
    ("git clone --unknown-flag value https://github.com/x/y /tmp/clone-x", []),
    ("git clone", []),
    ("git clone https://github.com/x/y /tmp/one /tmp/two", ["/tmp/one"]),
    ("git clone https://github.com/x/*.git", []),
])
def test_clone_directory_is_a_write_target(command, expected):
    assert scan(command, "/work")[2] == expected


@pytest.mark.parametrize("command,expected", [
    ("mktemp -d /tmp/probe.XXXXXX", ["/tmp/probe.XXXXXX"]),
    ("mktemp /tmp/f.XXXXXX", ["/tmp/f.XXXXXX"]),
    ("mktemp -d", []),
    ("mktemp -d '$T'", ["/work/$T"]),
    ("mktemp -d *.py", []),
    ("mktemp -d -p /x probe.XXXXXX", []),
    ("mktemp -d --tmpdir=/x probe.XXXXXX", []),
    ("mktemp --dry-run /tmp/f.XXXXXX", []),
    ("mktemp -u /tmp/f.XXXXXX", []),
    ("mktemp --unknown x", []),
])
def test_mktemp_template_is_a_write_target(command, expected):
    assert scan(command, "/work")[2] == expected


@pytest.mark.parametrize("command,expected", [
    ("d=$(mktemp -d /tmp/probe.XXXXXX)", ["/tmp/probe.XXXXXX"]),
    ('d="$(mktemp -d /tmp/probe.XXXXXX)"', ["/tmp/probe.XXXXXX"]),
    ("d=$(mkdir /tmp/rc)", ["/tmp/rc"]),
    ("d=$(mkdir -p /tmp/a /tmp/b)", ["/tmp/a", "/tmp/b"]),
    ("d=$(cd /tmp && mktemp -d probe.XXXXXX)", ["/tmp/probe.XXXXXX"]),
    ("T=/tmp d=$(mktemp -d $T/probe.XXXXXX)", ["/tmp/probe.XXXXXX"]),
    ("d=$(mktemp -d /tmp/p.XX", []),
    ("d=$(mktemp -d /tmp/p.XX)/f", []),
    ("e=y d=$(mktemp -d /tmp/p.XX) cmd", []),
    ("d=`mktemp -d /tmp/probe.XXXXXX`", []),
    ("d=$(mktemp -d \"$(dirname f)/p.XX\")", []),
    # Issue #741: a literal paren inside the quoted template is quote
    # provenance, not substitution nesting.
    ("d=\"$(mktemp '/tmp/p(1).XXXXXX')\"", ["/tmp/p(1).XXXXXX"]),
    ("d=\"$( (mktemp -d /tmp/probe.XXXXXX) )\"", ["/tmp/probe.XXXXXX"]),
    ("d=\"$(mktemp -d x)y)\"", []),
    ("d=\"$(mktemp -d `x`)\"", []),
    ("d=\"$(mktemp -d \\\"/tmp/p.XX\\\")\"", []),
    # Issue #747: a declaration builtin runs the same substitution and
    # its assignment arguments carry into the capture's inner scan.
    ("export d=$(mktemp -d /tmp/probe.XXXXXX)", ["/tmp/probe.XXXXXX"]),
    ("local d=$(mktemp -d /tmp/probe.XXXXXX)", ["/tmp/probe.XXXXXX"]),
    ("readonly d=$(mktemp -d /tmp/probe.XXXXXX)", ["/tmp/probe.XXXXXX"]),
    ("declare d=$(mktemp -d /tmp/probe.XXXXXX)", ["/tmp/probe.XXXXXX"]),
    ("typeset d=$(mktemp -d /tmp/probe.XXXXXX)", ["/tmp/probe.XXXXXX"]),
    ("export d=$(mktemp -d /tmp/p.XX) extra", []),
    ("export T=/tmp d=$(mktemp -d $T/probe.XXXXXX)", ["/tmp/probe.XXXXXX"]),
])
def test_captured_substitution_templates_are_write_targets(command, expected):
    assert scan(command, "/work")[2] == expected


@pytest.mark.parametrize("command,expected", [
    ("d=$(head -50 f.py)", (None, [], [])),
    ("d=$(head -50 f.py); cat g.py", ("whole", ["/work/g.py"], [])),
    ("d=$(cd /tmp && mktemp -d probe.XXXXXX); cat out.py",
     ("whole", ["/work/out.py"], ["/tmp/probe.XXXXXX"])),
])
def test_captured_substitution_books_no_intake(command, expected):
    """A capture's stdout feeds a variable, not the transcript: its inner
    reads and slice/whole kind stay unbooked, and a `cd` inside moves
    only the nested scan."""
    assert scan(command, "/work") == expected


def test_stray_close_paren_keeps_operator_splitting():
    assert scan("(a)) | cat f.py", "/work") == ("whole", ["/work/f.py"], [])


def test_directory_targets_join_file_writes_in_order():
    writes = scan("mkdir -p /tmp/w && cd /tmp/w && git clone https://github.com/x/y",
                  "/repo")[2]
    assert writes == ["/tmp/w", "/tmp/w/y"]
