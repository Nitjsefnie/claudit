"""Clobber and stream redirects booking write targets (issue #794)."""
from __future__ import annotations

import pytest

from backend.bash_reads import scan


@pytest.mark.parametrize("command,target", [
    ("echo hi >| pf.txt", "/work/pf.txt"),
    ("echo hi &> af.txt", "/work/af.txt"),
    ("echo hi &>> ap.txt", "/work/ap.txt"),
    ("echo hi 2>| f2.txt", "/work/f2.txt"),
])
def test_clobber_and_stream_redirects_book_write_targets(command, target):
    """`>|` clobbers and `&>` / `&>>` redirect stdout+stderr; bash opens
    each target, so every spelling books its write like `>` (#794)."""
    assert scan(command, "/work")[2] == [target]


@pytest.mark.parametrize("redirect", [">|", "&>", "&>>"])
def test_stream_redirects_refuse_when_incomplete(redirect):
    """A targetless spelling is a bash syntax error — nothing runs, so
    no operand inference and no phantom write via the old `&` split."""
    assert not scan("cp src.txt dst.txt " + redirect, "/work")[2]


@pytest.mark.parametrize("command,target", [
    ("echo hi >& both.txt", "/work/both.txt"),
    ("echo hi 1>& one.txt", "/work/one.txt"),
])
def test_fd_dup_spelling_to_a_filename_books_write_target(command, target):
    """`>& file` with the fd omitted (or spelled 1) is bash's
    stdout+stderr-to-file spelling, `&>`'s twin: the file is created and
    both streams land in it (#802)."""
    assert scan(command, "/work")[2] == [target]


@pytest.mark.parametrize("command", [
    "((x)) >& both.txt",
    "((x)) 1>& both.txt",
])
def test_arithmetic_tail_fd_dup_filename_books_write_target(command):
    """The compound tail's `>& file` books like the plain-command one:
    bash opens the file before the (failing) evaluation (#802, #785)."""
    assert scan(command, "/work")[2] == ["/work/both.txt"]


@pytest.mark.parametrize("command", [
    "echo hi >&2",
    "echo hi >&-",
    "echo hi 2>&1",
])
def test_descriptor_targets_stay_non_writes(command):
    """`>& digits` duplicates and `>&-` closes; nothing touches disk."""
    assert scan(command, "/work")[2] == []


def test_fd_digit_two_with_filename_books_nothing():
    """`2>& file` is a bash ambiguous-redirect error — nothing runs, so
    the scan books nothing rather than a guessed write."""
    assert scan("echo hi 2>& two.txt", "/work") == (None, [], [])


@pytest.mark.parametrize("command,target", [
    ("echo hi >& 2f.txt", "/work/2f.txt"),
    ("echo hi >& 22sep.txt", "/work/22sep.txt"),
])
def test_fd_dup_predicate_is_not_a_leading_digit_read(command, target):
    """The dup predicate is all-digits-or-`-`, fullmatch: a filename that
    merely STARTS with a digit is a file and books (#802)."""
    assert scan(command, "/work")[2] == [target]
