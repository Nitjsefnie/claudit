from __future__ import annotations

import pytest

from backend.bash_reads import scan


@pytest.mark.parametrize("command,target", [
    ("echo hi >| pf.txt", "/work/pf.txt"),
    ("echo hi &> af.txt", "/work/af.txt"),
    ("echo hi &>> ap.txt", "/work/ap.txt"),
    ("echo hi 2>| f2.txt", "/work/f2.txt"),
    ("echo hi >& 2f.txt", "/work/2f.txt"),
    ("echo hi >& 22sep.txt", "/work/22sep.txt"),
])
def test_clobber_and_stream_redirects_book_write_targets(command, target):
    """`>|` clobbers and `&>` / `&>>` redirect stdout+stderr; bash opens
    each target, so every spelling books its write like `>`. A digit-leading
    filename is still a file; only all-digit targets name descriptors."""
    assert scan(command, "/work")[2] == [target]


@pytest.mark.parametrize("command", [
    "echo hi >&2",
    "echo hi >&-",
    "echo hi 2>&1",
    "echo hi 2>& two.txt",
])
def test_descriptor_targets_stay_non_writes(command):
    """`>& digits` duplicates, `>&-` closes, and `2>& file` refuses; none book a file."""
    assert scan(command, "/work") == (None, [], [])
