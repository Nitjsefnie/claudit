"""Read-target recovery from Bash command text (backend/bash_reads.py)."""
from __future__ import annotations

import pytest

from backend.bash_reads import scan


@pytest.mark.parametrize("program", ["cp", "mv", "install -m 644"])
@pytest.mark.parametrize("target,expected", [
    ("a{b,c}", []),
    ("a{1..3}", []),
    ("{a..z..2}", []),
    ("a{b,'c'}", []),
    ("{dest}", ["/work/{dest}"]),
    ("a{dest}b", ["/work/a{dest}b"]),
    ("'{a,b}'", ["/work/{a,b}"]),
    ('"{1..3}"', ["/work/{1..3}"]),
    (r"a\{b,c\}", ["/work/a{b,c}"]),
    (r"{a\,b}", ["/work/{a,b}"]),
    (")", []),
])
def test_brace_words_never_fabricate_copy_destinations(program, target, expected):
    assert scan(f"{program} src.txt {target}", "/work")[2] == expected


@pytest.mark.parametrize("command,target", [
    ("cp src.txt dst.txt", "/work/dst.txt"),
    ("install -m 644 src.txt dst.txt", "/work/dst.txt"),
    ("mv README.new.md README.md", "/work/README.md"),
    ("sed -i 's/a/b/' README", "/work/README"),
    ("perl -pi -e 's/a/b/' README", "/work/README"),
])
@pytest.mark.parametrize("redirect", ["2>&1", "< input.txt", "0<&3", "<&-", "3< input.txt", "2>&-"])
def test_input_and_fd_redirections_are_not_write_operands(command, target, redirect):
    assert scan(command + " " + redirect, "/work")[2] == [target]


@pytest.mark.parametrize("command", [
    "cp src.txt '2>&1'",
    "install -m 644 src.txt '2>&1'",
    "mv src.txt '2>&1'",
    "sed -i 's/a/b/' '2>&1'",
    "perl -pi -e 's/a/b/' '2>&1'",
])
def test_quoted_fd_spelling_is_a_literal_filename(command):
    assert scan(command, "/work")[2] == ["/work/2>&1"]


@pytest.mark.parametrize("redirect", ["<>", "><", "2>&", "<", ")"])
def test_unsupported_or_incomplete_redirects_refuse_operand_inference(redirect):
    assert not scan("cp src.txt dst.txt " + redirect, "/work")[2]


def test_file_output_redirect_stays_separate_from_destination():
    assert scan("cp src.txt dst.txt > log.txt 2>&1 < input.txt", "/work")[2] == ["/work/log.txt", "/work/dst.txt"]


def test_stdin_file_remains_a_content_read_for_cat():
    assert scan("cat < input.txt", "/work") == ("whole", ["/work/input.txt"], [])


@pytest.mark.parametrize("command,expected", [
    ("DIR=/work; sed -i 's/a/b/' $DIR/file.txt", ["/work/file.txt"]),
    ('DIR=/work; cp source.txt "$DIR/file.txt"', ["/work/file.txt"]),
    ("DIR=/work; install -m 644 source.txt $DIR/file.txt", ["/work/file.txt"]),
    ("DIR=/work; mv source.txt $DIR/file.txt", ["/work/file.txt"]),
    ('DIR=dest; cp -t "$DIR" source.txt', ["/work/dest/source.txt"]),
    ('DIR=dest; install --target-directory="$DIR" source.txt', ["/work/dest/source.txt"]),
    ('DIR=dest; mv -t "$DIR" source.txt', ["/work/dest/source.txt"]),
    ("SRC=source.txt; cp $SRC dest/", ["/work/dest/source.txt"]),
    ("DIR=dest; cp source.txt '$DIR/file.txt'", ["/work/$DIR/file.txt"]),
    ("DIR=dest; sed -i 's/a/b/' '$DIR/file.txt'", ["/work/$DIR/file.txt"]),
    ("DIR=dest; cp -t '$DIR' source.txt", ["/work/$DIR/source.txt"]),
    ('cp source.txt "$UNKNOWN/file.txt"', []),
    ("sed -i 's/a/b/' $UNKNOWN/file.txt", []),
    ("cp -t $UNKNOWN source.txt", []),
    ("DIR=$UNKNOWN; cp source.txt $DIR/file.txt", []),
    ("DIR=/old; DIR=$UNKNOWN; mv source.txt $DIR/file.txt", []),
    ("DIR='$LITERAL'; cp source.txt \"$DIR/file.txt\"", ["/work/$LITERAL/file.txt"]),
])
def test_recorded_variable_operands_resolve_before_literal_gates(command, expected):
    assert scan(command, "/work")[2] == expected


@pytest.mark.parametrize("program", ["cp", "mv", "install -m 644"])
@pytest.mark.parametrize("target,expected", [
    ("dest/.", ["/work/dest/source.txt"]),
    ("dest/..", ["/work/source.txt"]),
])
def test_terminal_dot_component_establishes_destination_directory(program, target, expected):
    assert scan(f"{program} source.txt {target}", "/work")[2] == expected


@pytest.mark.parametrize("program", ["cp", "mv", "install -m 644"])
@pytest.mark.parametrize("target", ["dest/.", "dest/..", "dest/", ".", ".."])
def test_no_target_directory_refuses_explicit_directory_destination(program, target):
    assert not scan(f"{program} -T source.txt {target}", "/work")[2]


@pytest.mark.parametrize("expression", ["'b.md'", "unknown"])
def test_loop_condition_assignment_never_reuses_prior_path(expression):
    command = ("python3 - <<'PY'\nfor p in ['a.md']:\n"
               f"    if (p := {expression}):\n"
               "        open(p,'w')\n"
               "    else:\n        open(p,'w')\nPY")
    # The condition can rebind p before either branch. Refuse to infer a
    # value from condition evaluation instead of retaining the stale a.md.
    assert not scan(command, "/work")[2]


@pytest.mark.parametrize("body,expected", [
    ("p='old.md'\nfor item in ['a.md','b.md']:\n    open(p,'w')\n    p=item",
     ["/work/old.md", "/work/a.md"]),
    ("p='old.md'\nfor item in ('a.md','b.md'):\n    alias=p\n    open(alias,'w')\n    p=item",
     ["/work/old.md", "/work/a.md"]),
    ("p='old.md'\nfor item in ['a.md','b.md']:\n    open(p,'w')\n    if unknown:\n        p=item",
     ["/work/old.md"]),
    ("p='old.md'\nfor item in ['a.md','b.md']:\n    open(p,'w')\n    p=unknown",
     ["/work/old.md"]),
])
def test_literal_iterations_carry_only_established_body_bindings(body, expected):
    assert scan("python3 - <<'PY'\n" + body + "\nPY", "/work")[2] == expected


@pytest.mark.parametrize("command,expected", [
    ("cp src.txt dst.txt", ["/work/dst.txt"]),
    ("cp a.txt b.txt dest/", ["/work/dest/a.txt", "/work/dest/b.txt"]),
    ("cp src.txt dest", ["/work/dest"]),
    ("cp -t dest src.txt", ["/work/dest/src.txt"]),
    ("cp --target-directory=dest a.txt", ["/work/dest/a.txt"]),
    ("cp -T src.txt dst.txt", ["/work/dst.txt"]),
    ("cp --suffix=.bak src.txt dst.txt", ["/work/dst.txt"]),
    ("cp --no-preserve mode src.txt dst.txt", ["/work/dst.txt"]),
    ("cp -vt dest src.txt", ["/work/dest/src.txt"]),
    ("cp -t '$DIR' src.txt", ["/work/$DIR/src.txt"]),
    ("cp -t dest", []),
    ("cp --unknown value src.txt dst.txt", []),
    ("cp *.txt dest/", []),
    ('cp src.txt "$OUT"', []),
    ("cp src.txt '$OUT'", ["/work/$OUT"]),
    ("install -o postgres -g postgres -m 0400 src.dump dst.dump", ["/work/dst.dump"]),
    ("install --owner postgres --group=postgres --mode=0400 -t dest src.dump", ["/work/dest/src.dump"]),
    ("install -d directory", []),
    ("install --strip-program striptool -m0400 src.txt dst.txt", ["/work/dst.txt"]),
    ("install --directory dir.txt", []),
    ("mv README.new.md README.md", ["/work/README.md"]),
    (r"printf 'a\n' > README.new.md; mv README.new.md README.md", ["/work/README.new.md", "/work/README.md"]),
    ("mv -t dest src.txt", ["/work/dest/src.txt"]),
    ("perl -pi -e 's/old/new/g' file.ts", ["/work/file.ts"]),
    ("perl -i.bak -pe 's/old/new/g' README", ["/work/README"]),
    ("perl -i -I lib/ -M Foo -e 's/a/b/' VERSION", ["/work/VERSION"]),
    ("perl -pe 's/old/new/g' file.ts", []),
    (r"sed -i '311a !tests/docs/\ntests/docs/*\n!tests/docs/*.ts' .gitignore", ["/work/.gitignore"]),
    ("sed -i -e 's/a/b/' README VERSION", ["/work/README", "/work/VERSION"]),
    ("sed -i --expression='s/a/b/' README", ["/work/README"]),
    ("sed -i -f program.sed README", ["/work/README"]),
    ("sed -i -- 's/a/b/' README", ["/work/README"]),
    ("printf x > README", ["/work/README"]),
    (r'printf x > "\$OUT"', ["/work/$OUT"]),
    ("printf x 2> errors.txt", ["/work/errors.txt"]),
    ("echo '>' example.txt", []),
    ("cd /other && mv a.md README.md", ["/other/README.md"]),
])
def test_explicit_write_destinations(command, expected):
    assert scan(command, "/work")[2] == expected


@pytest.mark.parametrize("body,expected", [
    ("for path in ['a.md','b.md']:\n    open(path,'w').write(text)", ["/work/a.md", "/work/b.md"]),
    ("for path in ('a.md','b.md'):\n    alias=path\n    p=Path(alias)\n    p.write_text(text)", ["/work/a.md", "/work/b.md"]),
    ("for path in paths:\n    open(path,'w').write(text)", []),
    ("path='old.md'\nfor path in paths:\n    open(path,'w').write(text)\nopen(path,'w').write(text)", []),
    ("for path in ['a.md']:\n    path=unknown\n    open(path,'w').write(text)", []),
    ("for path in ['a.md']:\n    def unused():\n        open(path,'w').write(text)", []),
    ("for path in ['a.md']:\n    if Path(path).exists():\n        Path(path).write_text(text)", ["/work/a.md"]),
    ("p='a.md'; alias=p; p=unknown\nopen(alias,'w').write(text)", ["/work/a.md"]),
    ("p='a.md'\nimport unknown as p\nopen(p,'w').write(text)", []),
    ("for p in ['a.md']:\n    import unknown as p\n    open(p,'w').write(text)", []),
    ("for p in ['a.md']:\n    alias=p\n    with open(alias,'w') as handle:\n        handle.write(text)", ["/work/a.md"]),
])
def test_python_literal_path_iterations(body, expected):
    assert scan("python3 - <<'PY'\n" + body + "\nPY", "/work")[2] == expected


def test_python_path_iteration_is_bounded():
    paths = repr([f"{idx}.md" for idx in range(1000)])
    command = "python3 - <<'PY'\nfor p in " + paths + ":\n    open(p,'w')\nPY"
    assert not scan(command, "/work")[2]


def test_nested_python_path_iteration_is_bounded():
    body = ""
    for depth in range(10):
        body += "    " * depth + "for p in ['a.md','b.md']:\n"
    body += "    " * 10 + "open(p,'w')"
    assert not scan("python3 - <<'PY'\n" + body + "\nPY", "/work")[2]


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


def test_interpreter_given_by_path_yields_write_targets():
    cmd = (".venv/bin/python - <<'PY'\n"
           "p = 'backend/x.py'; t = open(p).read()\n"
           "open(p, 'w').write(t)\n"
           "PY\n")
    _, _, writes = scan(cmd, "/repo")
    assert writes == ["/repo/backend/x.py"]


@pytest.mark.parametrize("command,expected", [
    ("FILES='a.md b.md'; sed -i 's/a/b/' $FILES", []),
    ('FILES="a.md b.md"; sed -i \'s/a/b/\' "$FILES"', ["/work/a.md b.md"]),
    ("DEST='a b'; cp source.txt $DEST", []),
    ('DEST="a b"; cp source.txt "$DEST"', ["/work/a b"]),
    ("DEST='a b'; install -t $DEST source.txt", []),
    ('DEST="a b"; install -t "$DEST" source.txt', ["/work/a b/source.txt"]),
    ("DEST='a b'; mv source.txt prefix${DEST}suffix", []),
    ('DEST="a b"; mv source.txt prefix"${DEST}"suffix', ["/work/prefixa bsuffix"]),
    ("FILES='a.md\tb.md'; sed -i 's/a/b/' $FILES", []),
    ("FILES='a.md\nb.md'; sed -i 's/a/b/' $FILES", []),
    ("IFS=,; DEST='a,b'; cp source.txt $DEST", []),
    ("IFS=$UNKNOWN; DEST='a,b'; cp source.txt $DEST", []),
    ('IFS=,; DEST="a,b"; cp source.txt "$DEST"', ["/work/a,b"]),
    ("EXTRA='-t elsewhere'; cp source.txt $EXTRA final", []),
    ("EXTRA=$UNKNOWN; cp source.txt $EXTRA final", []),
    ("EMPTY=''; cp source.txt $EMPTY final", []),
    ("cp source.txt 'literal space.txt'", ["/work/literal space.txt"]),
    ('DEST="a b"; cp source.txt \'$DEST\'', ["/work/$DEST"]),
])
def test_ambiguous_unquoted_operand_expansion_is_refused(command, expected):
    assert scan(command, "/work")[2] == expected


@pytest.mark.parametrize("body,expected", [
    ("p='old.md'\nfor item in ['a.md','b.md']:\n    open(p,'w')\n    continue\n    p='unreachable.md'", ["/work/old.md"]),
    ("p='old.md'\nfor item in ['a.md','b.md']:\n    open(p,'w')\n    p=item\n    continue\n    p='unreachable.md'", ["/work/old.md", "/work/a.md"]),
    ("p='old.md'\nfor item in ['a.md','b.md']:\n    open(p,'w')\n    p=item\n    break\n    p='unreachable.md'", ["/work/old.md"]),
    ("p='old.md'\nfor item in ['a.md','b.md']:\n    open(p,'w')\n    if unknown:\n        continue\n        p='unreachable.md'", ["/work/old.md"]),
    ("p='old.md'\nfor item in ['a.md','b.md']:\n    open(p,'w')\n    with manager():\n        continue\n        p='unreachable.md'", ["/work/old.md"]),
    ("p='old.md'\nfor item in ['a.md','b.md']:\n    open(p,'w')\n    try:\n        break\n    finally:\n        p='unreachable.md'", ["/work/old.md"]),
])
def test_loop_exits_do_not_carry_unreachable_assignments(body, expected):
    assert scan("python3 - <<'PY'\n" + body + "\nPY", "/work")[2] == expected


@pytest.mark.parametrize("command,cwd,expected", [
    ("cat 'C:/Users/Sample/f.py'", r"D:\other", r"C:\Users\Sample\f.py"),
    (r"cat 'C:\Users\Sample\f.py'", r"D:\other", r"C:\Users\Sample\f.py"),
    ("cat 'C:/Users/Sample/f.py'", "/work", r"C:\Users\Sample\f.py"),
    ("cat 'C:/Users/Sample/./pkg/../f.py'", "", r"C:\Users\Sample\f.py"),
    ("cat 'src/f.py'", r"C:/Users\Sample/work", r"C:\Users\Sample\work\src\f.py"),
    (r"cat 'src\f.py'", r"C:\work", r"C:\work\src\f.py"),
    (r"cat '..\f.py'", r"C:\work\pkg", r"C:\work\f.py"),
    (r"cat 'C:\work\README'", r"D:\other", r"C:\work\README"),
    (r"cat 'src\README'", r"C:\work", r"C:\work\src\README"),
    (r"cat 'C:f.py'", r"C:\work", r"C:\work\f.py"),
    (r"cat 'D:f.py'", r"C:\work", "D:f.py"),
    ("cat /etc/hosts", r"C:\work", "/etc/hosts"),
    ("cat /c/Users/Sample/f.py", r"C:\work", "/c/Users/Sample/f.py"),
    ("cat //server/share/f.py", "/work", "//server/share/f.py"),
    ("cat pkg/../f.py", "/work", "/work/f.py"),
    (r"cat 'pkg\f.py'", "/work", r"/work/pkg\f.py"),
    (r"cat '\\server\share\pkg\..\f.py'", r"C:\work", r"\\server\share\f.py"),
    ("cat f.py", r"\\server\share\work", r"\\server\share\work\f.py"),
    (r"cat '\\?\C:\work\.\f.py'", r"D:\work", r"\\?\C:\work\.\f.py"),
])
def test_target_resolution_uses_recorded_path_flavor(command, cwd, expected):
    assert scan(command, cwd) == ("whole", [expected], [])


@pytest.mark.parametrize("program", ["cp", "mv", "install -m 644"])
@pytest.mark.parametrize("operands,cwd,expected", [
    (r"'D:\src\f.py' out/", r"C:\work", r"C:\work\out\f.py"),
    (r"'src\f.py' 'out\'", r"C:\work", r"C:\work\out\f.py"),
    (r"'D:\src\f.py' 'C:\out\'", r"C:\work", r"C:\out\f.py"),
    (r"'src\f.py' out/", "/work", r"/work/out/src\f.py"),
    (r"f.py '\\server\share'", r"C:\work", r"\\server\share\f.py"),
])
def test_copy_destinations_use_path_flavor(program, operands, cwd, expected):
    assert scan(program + " " + operands, cwd)[2] == [expected]


@pytest.mark.parametrize("command", [
    r"cat C:/work/*.py",
    r'cat "C:/work/$MISSING/f.py"',
    r'sed -i "s/a/b/" "$MISSING/f.py"',
    r'cp f.py "$MISSING/out.py"',
])
def test_windows_targets_do_not_resolve_expansions_or_globs(command):
    assert scan(command, r"C:\work") == (None, [], [])


def test_windows_cd_does_not_duplicate_the_recorded_cwd():
    command = 'cd "C:/Users/Sample/project" && sed -i "s/a/b/" pkg/f.py'
    assert scan(command, r"C:\Users\Sample\project")[2] == [r"C:\Users\Sample\project\pkg\f.py"]


def test_windows_read_pattern_backslash_is_not_a_target():
    assert scan(r"grep 'word\b' 'src\f.py'", r"C:\work") == ("slice", [r"C:\work\src\f.py"], [])


@pytest.mark.parametrize("command", [
    'cd "$MISSING" && cat f.py',
    'cd "C:/work/$MISSING" && cat f.py',
    'cd "$MISSING" && cp f.py out/',
])
def test_unknown_cd_does_not_invent_a_resolved_target(command):
    assert scan(command, "C:/work") == (None, [], [])


def test_explicit_absolute_target_is_known_after_unknown_cd():
    assert scan('cd "$MISSING" && cat "C:/known/f.py"', "D:/work") == ("whole", ["C:\\known\\f.py"], [])


@pytest.mark.parametrize("cwd,expected", [("/work", "/work/docs.txt"), ("C:/work", "C:\\work\\docs.txt")])
def test_windows_spelling_in_grep_pattern_is_not_a_read_target(cwd, expected):
    assert scan(r"grep 'C:\Users\Sample' docs.txt", cwd) == ("slice", [expected], [])


@pytest.mark.parametrize("program", ["cp", "mv", "install -m 644"])
@pytest.mark.parametrize("directory,expected", [
    ('\\\\?\\C:\\out\\', '\\\\?\\C:\\out\\file.py'),
    ('\\\\?\\UNC\\server\\share\\out\\', '\\\\?\\UNC\\server\\share\\out\\file.py'),
    ('\\\\?\\C:\\out\\.\\', '\\\\?\\C:\\out\\.\\file.py'),
    ('\\\\server\\share\\out\\', '\\\\server\\share\\out\\file.py'),
    ('/work/out/', '/work/out/file.py'),
])
def test_copy_children_use_destination_flavor_without_changing_verbatim_components(program, directory, expected):
    command = program + " file.py '" + directory + "'"
    assert scan(command, r"C:\work")[2] == [expected]
