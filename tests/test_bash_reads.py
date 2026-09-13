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


# --- what a Bash command WROTE, recovered from its text ---------------

@pytest.mark.parametrize("command", [
    "sed -i 's/a/b/' foo.py",
    "sed -i.bak -e 's/a/b/' foo.py",
    "sed --in-place 's/a/b/' foo.py",
    "sed -i -n 's/a/b/p' foo.py",
])
def test_in_place_sed_is_a_write_not_a_read(command):
    """`sed -i` changes the file. Booking it as a slice READ inverts
    both signals: the edit is missed and a phantom read is added."""
    kind, reads, writes = scan(command, "/repo")
    assert (kind, reads) == (None, [])
    assert writes == ["/repo/foo.py"]


def test_sed_script_operand_is_never_a_file():
    """`s/a/b/` is path-shaped (it has slashes) but it is sed's
    program, always — unlike grep, sed has no ambiguity to resolve."""
    kind, reads, writes = scan("sed 's/a/b/' foo.py", "/repo")
    assert (kind, reads, writes) == ("slice", ["/repo/foo.py"], [])


def test_sed_n_takes_no_value():
    kind, reads, _ = scan("sed -n 's/a/b/p' foo.py", "/repo")
    assert (kind, reads) == ("slice", ["/repo/foo.py"])


@pytest.mark.parametrize("command", [
    "S=/tmp/s && cat > $S/f.py <<'EOF'\nx\nEOF",
    "S=/tmp/s; cat > ${S}/f.py <<'EOF'\nx\nEOF",
    "S=/tmp/s cat > $S/f.py <<'EOF'\nx\nEOF",
])
def test_variable_assigned_in_the_same_command_is_expanded(command):
    """`S=/tmp/s && cat > $S/f.py` names the file as surely as the
    literal does — the value is right there in the text."""
    _, _, writes = scan(command, "/repo")
    assert writes == ["/tmp/s/f.py"]


def test_variable_assigned_in_the_same_command_resolves_a_read():
    kind, reads, _ = scan("D=backend; grep -n x $D/parse.py", "/repo")
    assert (kind, reads) == ("slice", ["/repo/backend/parse.py"])


def test_unresolved_variable_names_no_file():
    """`$1/x.py` is built at runtime; a verbatim `/repo/$1/x.py` is a
    key nothing else will ever match."""
    _, reads, writes = scan("cat $1/x.py > $OUT/y.py", "/repo")
    assert (reads, writes) == ([], [])


def test_heredoc_body_is_not_scanned_for_paths():
    """A docstring inside a heredoc that mentions INDEX.md did not read
    INDEX.md."""
    cmd = ("cat > f.py <<'EOF'\n"
           "\"\"\"Rebuild INDEX.md from a/b.txt. Run os.path.dirname.\"\"\"\n"
           "EOF\n")
    kind, reads, writes = scan(cmd, "/repo")
    assert (kind, reads) == (None, [])
    assert writes == ["/repo/f.py"]


def test_null_sink_redirect_is_not_a_write():
    _, _, writes = scan("python3 x.py >/dev/null 2>&1", "/repo")
    assert not writes


@pytest.mark.parametrize("body,expected", [
    ("open('out.md', 'w').write('x')", ["/repo/out.md"]),
    ("with open('out.md', mode='a') as fh:\n    fh.write('x')",
     ["/repo/out.md"]),
    ("import pathlib\npathlib.Path('out.md').write_text('x')",
     ["/repo/out.md"]),
    ("from pathlib import Path\np = Path('/abs/out.md')\n"
     "p.write_text(p.read_text().replace('a', 'b'))", ["/abs/out.md"]),
    ("p = 'out.md'\nt = open(p).read()\nopen(p, 'w').write(t)",
     ["/repo/out.md"]),
    ("print(open('in.md').read())", []),
])
def test_python_heredoc_write_paths_are_write_targets(body, expected):
    """The python body names the file it opens for writing as plainly
    as `cat > f` does."""
    cmd = "python3 - <<'PY'\n" + body + "\nPY\n"
    _, _, writes = scan(cmd, "/repo")
    assert writes == expected


def test_python_helper_called_with_literal_paths_yields_write_targets():
    cmd = ("python3 - <<'PY'\n"
           "def sub(path, old, new):\n"
           "    t = open(path).read(); assert old in t\n"
           "    open(path, 'w').write(t.replace(old, new))\n"
           "sub('a/x.md', 'old', 'new')\n"
           "sub('a/y.md', 'old', 'new')\n"
           "PY\n")
    _, _, writes = scan(cmd, "/repo")
    assert writes == ["/repo/a/x.md", "/repo/a/y.md"]


def test_python_dash_c_write_path_is_a_write_target():
    _, _, writes = scan(
        "python3 -c \"open('gen.txt','w').write('x')\"", "/repo")
    assert writes == ["/repo/gen.txt"]
