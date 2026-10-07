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
