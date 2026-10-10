"""Read/write-target scanning of fd-prefixed redirects (backend/bash_segments.py).

A redirect's fd prefix is read by VALUE, not by spelling: bash ignores
leading zeros, so `01>&` is fd 1 and `00<` is fd 0. `_fold_fd` is the
shared primitive that folds a spelling to its value; it lives in
`bash_literals`, the shell-syntax layer both `bash_effects` and
`bash_segments` import from (#856, #857, #912).
"""
from __future__ import annotations

import pytest

from backend.bash_churn_errors import _verbatim_targets
from backend.bash_literals import _fold_fd
from backend.bash_reads import scan


@pytest.mark.parametrize("fd,expected", [
    ("", ""),
    ("0", "0"),
    ("1", "1"),
    ("2", "2"),
    ("01", "1"),
    ("001", "1"),
    ("00", "0"),
    ("000", "0"),
    ("10", "10"),
    ("010", "10"),
    ("12", "12"),
    ("100", "100"),
])
def test_fold_fd_reads_the_fd_by_value(fd, expected):
    """Leading zeros are insignificant (`01` is fd 1), an all-zero
    spelling folds to `0`, and an omitted fd stays empty."""
    assert _fold_fd(fd) == expected


@pytest.mark.parametrize("command,expected", [
    # plain spellings: unchanged by the fold.
    ("cat < f.txt", ("whole", ["f.txt"], [])),
    ("cat 0< f.txt", ("whole", ["f.txt"], [])),
    ("tee 1>& f.txt", (None, [], ["f.txt"])),
    ("tee >& f.txt", (None, [], ["f.txt"])),
    # a zero-padded fd is read by value: `00<` is fd 0, so it IS an
    # input redirect and f.txt is booked as read (#912).
    ("cat 00< f.txt", ("whole", ["f.txt"], [])),
    # `01>&` is fd 1, so it IS bash's stdout-to-file write of f.txt
    # (the un-folded spelling fell to the descriptor-target refusal and
    # dropped the whole command) (#912).
    ("tee 01>& f.txt", (None, [], ["f.txt"])),
])
def test_scan_reads_a_redirect_fd_by_value(command, expected):
    """`_split_redirects` folds the fd prefix before its fd tests, so a
    zero-padded spelling books the target bash would (#912)."""
    assert scan(command) == expected


def test_scan_leaves_fd_one_input_redirect_alone():
    """`01<` is fd 1 — a read-only open of f.txt onto fd 1 — NOT fd 0:
    bash refuses to write it (`echo out 01< f.txt` -> "Bad file
    descriptor") and `cat` never reads f.txt through it, so no input is
    booked. bash's oracle for the fd-0 case is `00<`, pinned above."""
    assert scan("cat 01< f.txt") == (None, [], [])


@pytest.mark.parametrize("command,expected", [
    ("cat 2> f.txt", (None, [], ["f.txt"])),
    ("cat 2>&1", (None, [], [])),
    ("tee >&2", (None, [], [])),
])
def test_scan_keeps_non_fd_one_and_zero_redirections(command, expected):
    """The fold narrows nothing away: an fd of 2 still writes its target
    and a duplication (`2>&1`, `>&2`) still books no file."""
    assert scan(command) == expected


@pytest.mark.parametrize("context,expected", [
    ("cat <<E | tee 2<<EOF", []),
    ("cat <<E | tee <<EOF", []),
])
def test_tee_arm_filters_an_fd_prefixed_heredoc_marker(context, expected):
    """A `<<`-led token is a heredoc marker, never a path, and the
    fd-prefixed form (`2<<EOF`) filters with it — a redirect shape, never
    a real file — so a later widening of the operator class cannot
    silently book it (#856 review N1)."""
    assert _verbatim_targets(context) == expected
