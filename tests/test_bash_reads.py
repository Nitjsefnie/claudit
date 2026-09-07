"""Read-target recovery from Bash command text (backend/bash_reads.py)."""
from __future__ import annotations

import pytest

from backend.bash_reads import scan


@pytest.mark.parametrize("command,expected", [
    ("cat backend/parse.py", ("whole", ["/repo/backend/parse.py"])),
    ("less /etc/hosts", ("whole", ["/etc/hosts"])),
    ("cat a.py b.py", ("whole", ["/repo/a.py", "/repo/b.py"])),
])
def test_whole_file_reads(command, expected):
    kind, reads, _ = scan(command, "/repo")
    assert (kind, reads) == expected


@pytest.mark.parametrize("command,expected_reads", [
    ("grep -n TODO notes.md", ["/repo/notes.md"]),
    ("sed -n '1,200p' backend/schema.sql", ["/repo/backend/schema.sql"]),
    ("head -n 50 out.log", ["/repo/out.log"]),
    ("tail -20 build.txt", ["/repo/build.txt"]),
    ("awk '{print $1}' data.csv", ["/repo/data.csv"]),
])
def test_slice_reads_are_not_whole(command, expected_reads):
    kind, reads, _ = scan(command, "/repo")
    assert kind == "slice"
    assert reads == expected_reads


def test_pipeline_narrowing_demotes_a_whole_read():
    """`cat f | grep x` put a FILTERED file in context, not the file.

    Scoring it whole is what inverts the metric: grep-then-narrow would
    rank as more wasteful than one indiscriminate cat.
    """
    kind, reads, _ = scan("cat big.py | grep import", "/repo")
    assert kind == "slice"
    assert reads == ["/repo/big.py"]


def test_pattern_operand_is_not_a_file():
    _, reads, _ = scan("grep TODO notes.md", "/repo")
    assert reads == ["/repo/notes.md"]


def test_quoted_alternation_does_not_split_the_segment():
    """A `|` inside quotes is one token; splitting textually loses the
    file and invents a pipeline stage."""
    kind, reads, _ = scan(r"grep -n 'a\|b\|c' backend/parse.py", "/repo")
    assert kind == "slice"
    assert reads == ["/repo/backend/parse.py"]


def test_value_flag_argument_is_not_a_file():
    _, reads, _ = scan("grep -e config.py -n TODO notes.md", "/repo")
    assert "/repo/notes.md" in reads


def test_cd_rebases_relative_paths():
    _, reads, _ = scan("cd /srv/app && cat conf.yml", "/repo")
    assert reads == ["/srv/app/conf.yml"]


def test_absolute_path_ignores_cwd():
    _, reads, _ = scan("cat /etc/passwd", "/repo")
    assert reads == ["/etc/passwd"]


def test_relative_path_without_cwd_is_kept_verbatim():
    _, reads, _ = scan("cat conf.yml")
    assert reads == ["conf.yml"]


@pytest.mark.parametrize("command", [
    "wc -l backend/parse.py",
    "ls -la backend/",
    "stat backend/parse.py",
    "git log --oneline -5",
    "python3 script.py",
])
def test_non_content_commands_yield_no_read(command):
    """These return a MEASUREMENT or run a program; neither puts the
    named file into the transcript."""
    kind, reads, _ = scan(command, "/repo")
    assert (kind, reads) == (None, [])


def test_unexpanded_glob_is_not_a_path():
    kind, reads, _ = scan("cat *.py", "/repo")
    assert (kind, reads) == (None, [])


def test_redirect_target_is_a_write_not_a_read():
    kind, reads, writes = scan("grep TODO src.py > out.txt", "/repo")
    assert kind == "slice"
    assert reads == ["/repo/src.py"]
    assert writes == ["/repo/out.txt"]


def test_append_redirect_and_tee_are_writes():
    _, _, writes = scan("cat a.py >> log.txt", "/repo")
    assert writes == ["/repo/log.txt"]
    _, _, tee_writes = scan("cat a.py | tee copy.py", "/repo")
    assert tee_writes == ["/repo/copy.py"]


def test_env_prefix_is_skipped():
    _, reads, _ = scan("LC_ALL=C grep TODO notes.md", "/repo")
    assert reads == ["/repo/notes.md"]


def test_unparsable_command_yields_nothing():
    """An unbalanced quote must produce no partial misreading."""
    kind, reads, writes = scan("cat 'unterminated.py", "/repo")
    assert (kind, reads, writes) == (None, [], [])


def test_duplicate_paths_collapse():
    _, reads, _ = scan("cat a.py a.py", "/repo")
    assert reads == ["/repo/a.py"]
