from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from backend.bash_churn import MAX_COMMAND_CHARS, BashCommand
from backend.bash_reads import scan, scan_command


def _run_limited_python(code: str, input_text: str = "", *,
                        args: tuple[str, ...] = (),
                        timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    """Run scanner regressions with a hang guard and Linux memory ceiling."""
    run_options: dict[str, Any] = {
        "input": input_text,
        "text": True,
        "capture_output": True,
        "timeout": timeout,
    }
    if sys.platform.startswith("linux"):
        def set_memory_limit() -> None:
            import resource  # pylint: disable=import-outside-toplevel

            limit = 768 * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

        run_options["preexec_fn"] = set_memory_limit

    try:
        completed = subprocess.run(
            [sys.executable, "-c", code, *args],
            check=False,
            **run_options,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"child process hang guard tripped after {timeout:g} seconds")
    return completed


@pytest.mark.parametrize("program", ["cp", "mv", "install -m 644"])
@pytest.mark.parametrize("operands,expected", [
    pytest.param('src.txt a{b,c}', ['/work/ac/src.txt', '/work/ac/ab'], id="brace-0"),
    pytest.param('src.txt a{1..3}', [], id="brace-1"),
    pytest.param('src.txt {a..z..2}', [], id="brace-2"),
    pytest.param("src.txt a{b,'c'}", ['/work/ac/src.txt', '/work/ac/ab'], id="brace-3"),
    pytest.param('src.txt {dest}', ['/work/{dest}'], id="brace-4"),
    pytest.param('src.txt a{dest}b', ['/work/a{dest}b'], id="brace-5"),
    pytest.param("src.txt '{a,b}'", ['/work/{a,b}'], id="brace-6"),
    pytest.param('src.txt "{1..3}"', ['/work/{1..3}'], id="brace-7"),
    pytest.param('src.txt a\\{b,c\\}', ['/work/a{b,c}'], id="brace-8"),
    pytest.param('src.txt {a\\,b}', ['/work/{a,b}'], id="brace-9"),
    pytest.param('src.txt )', [], id="brace-10"),
    pytest.param('source.txt dest/.', ['/work/dest/source.txt'], id="terminal-dot"),
    pytest.param('source.txt dest/..', ['/work/source.txt'], id="terminal-dotdot"),
    pytest.param('-T source.txt dest/.', [], id="no-target-directory-0"),
    pytest.param('-T source.txt dest/..', [], id="no-target-directory-1"),
    pytest.param('-T source.txt dest/', [], id="no-target-directory-2"),
    pytest.param('-T source.txt .', [], id="no-target-directory-3"),
    pytest.param('-T source.txt ..', [], id="no-target-directory-4"),
])
def test_copy_destination_spellings(program, operands, expected):
    assert scan(f"{program} {operands}", "/work")[2] == expected


@pytest.mark.parametrize("command,expected", [
    ("mkdir -p /tmp/a && echo {a,b}", ["/tmp/a"]),
    ("cp a.{s,t} dir/", ["/tmp/dir/a.s", "/tmp/dir/a.t"]),
    ("mkdir -p /tmp/a && cp -p /e/x.{s,t} /tmp/a/",
     ["/tmp/a", "/tmp/a/x.s", "/tmp/a/x.t"]),
    ("cp a.{a..z} dir/", ["/tmp/dir"]),
    ("cp a.{$x,y} dir/", ["/tmp/dir"]),
    ("cp $x.{s,t} dir/", ["/tmp/dir"]),
    ('cp "$x"{s,t} dir/', ["/tmp/dir"]),
    ("cp a.{s*,t} dir/", ["/tmp/dir"]),
    ("S=source; cp $S.{s,t} dir/", ["/tmp/dir/source.s", "/tmp/dir/source.t"]),
    ("cp a.{s,t}{1,2} dir/",
     ["/tmp/dir/a.s1", "/tmp/dir/a.s2", "/tmp/dir/a.t1", "/tmp/dir/a.t2"]),
    ("cp a.{s,{t,u}} dir/", ["/tmp/dir/a.s", "/tmp/dir/a.t", "/tmp/dir/a.u"]),
    ("cp a.{s,'t,u'} dir/", ["/tmp/dir/a.s", "/tmp/dir/a.t,u"]),
    ("cp a.{s,'$literal'} dir/", ["/tmp/dir/a.s", "/tmp/dir/a.$literal"]),
    (r"cp a.{s,\*} dir/", ["/tmp/dir/a.s", "/tmp/dir/a.*"]),
    ("cp a.{s,t} -t dir", ["/tmp/dir/a.s", "/tmp/dir/a.t"]),
    ("mkdir -p /tmp/a; cp a.{a..z} dir/; mkdir last", ["/tmp/a", "/tmp/dir", "/tmp/last"]),
    ("cp a.{s,t} 'literal{dir}'/", ["/tmp/literal{dir}/a.s", "/tmp/literal{dir}/a.t"]),
    ("mkdir {one,two}", ["/tmp/one", "/tmp/two"]),
    ("cp a" + "{s,t}" * 20 + " dir/", ["/tmp/dir"]),
])
def test_brace_expansion_write_targets(command, expected):
    assert scan(command, "/tmp")[2] == expected


@pytest.mark.parametrize("command,expected", [
    ('D={a,b}; mkdir "$D"', ["/tmp/{a,b}"]),
    ('D={a,b}; cp source "$D"', ["/tmp/{a,b}"]),
    ('D={a,b}; mkdir "$D"; echo {x,y}', ["/tmp/{a,b}"]),
    ('D={a,b} E=destination; mkdir "$D" "$E"', ["/tmp/{a,b}", "/tmp/destination"]),
    ("mkdir D={a,b}", ["/tmp/D=a", "/tmp/D=b"]),
    ('export D={a,b}; mkdir "$D"', ["/tmp/b"]),
])
def test_brace_recovery_preserves_scalar_assignment_values(command, expected):
    assert scan(command, "/tmp")[2] == expected


@pytest.mark.parametrize("quote", ["", '"'])
@pytest.mark.parametrize("inner,expected", [
    ("mkdir {one,two}", ["/tmp/one", "/tmp/two"]),
    ("mkdir -p /tmp/a && echo {a,b}", ["/tmp/a"]),
    ("cat input.txt; mkdir {one,two}", ["/tmp/one", "/tmp/two"]),
    ("D={a,b}; mkdir $D", ["/tmp/{a,b}"]),
    ("gh repo clone o/r", ["/tmp/r"]),
])
def test_brace_recovery_in_captured_substitutions(inner, expected, quote):
    command = BashCommand(f"d={quote}$({inner}){quote}")
    assert scan_command(command, "/tmp") == (None, [], expected)
    assert command.churn() == (0, 0)


@pytest.mark.parametrize("command,expected", [
    ("gh repo clone o/r /tmp/d", ["/tmp/d"]),
    ("gh repo clone o/r", ["/tmp/r"]),
    ("gh repo clone https://example.test/o/r.git", ["/tmp/r"]),
    ("gh repo clone --branch topic o/r checkout", ["/tmp/checkout"]),
    ("gh repo clone o/r -- --branch main ignored", ["/tmp/r"]),
    ("gh repo clone o/r checkout -- --depth 1 ignored", ["/tmp/checkout"]),
    ("gh repo clone o/r -- $UNKNOWN", ["/tmp/r"]),
    ("cd elsewhere; gh repo clone o/r checkout", ["/tmp/elsewhere/checkout"]),
    ("gh repo view o/r", []),
    ("gh repo clone --unknown o/r", []),
])
def test_gh_repo_clone_write_targets(command, expected):
    assert scan(command, "/tmp")[2] == expected


def test_brace_target_scan_preserves_unknown_heredoc_repeat_count():
    command = BashCommand(
        "echo {x,y}; for i in a b; do cat > out <<EOF\none\ntwo\nEOF\ndone")
    assert command.heredoc_repeats == [1]
    assert scan_command(command, "/tmp")[2] == ["/tmp/out"]
    assert command.heredoc_repeats == [1]
    assert command.churn() == (2, 0)


@pytest.mark.parametrize("command,expected", [
    pytest.param('cp src.txt dst.txt 2>&1', ['/work/dst.txt'], id="input-or-fd-0"),
    pytest.param('cp src.txt dst.txt < input.txt', ['/work/dst.txt'], id="input-or-fd-1"),
    pytest.param('cp src.txt dst.txt 0<&3', ['/work/dst.txt'], id="input-or-fd-2"),
    pytest.param('cp src.txt dst.txt <&-', ['/work/dst.txt'], id="input-or-fd-3"),
    pytest.param('cp src.txt dst.txt 3< input.txt', ['/work/dst.txt'], id="input-or-fd-4"),
    pytest.param('cp src.txt dst.txt 2>&-', ['/work/dst.txt'], id="input-or-fd-5"),
    pytest.param('install -m 644 src.txt dst.txt 2>&1', ['/work/dst.txt'], id="input-or-fd-6"),
    pytest.param('install -m 644 src.txt dst.txt < input.txt', ['/work/dst.txt'], id="input-or-fd-7"),
    pytest.param('install -m 644 src.txt dst.txt 0<&3', ['/work/dst.txt'], id="input-or-fd-8"),
    pytest.param('install -m 644 src.txt dst.txt <&-', ['/work/dst.txt'], id="input-or-fd-9"),
    pytest.param('install -m 644 src.txt dst.txt 3< input.txt', ['/work/dst.txt'], id="input-or-fd-10"),
    pytest.param('install -m 644 src.txt dst.txt 2>&-', ['/work/dst.txt'], id="input-or-fd-11"),
    pytest.param('mv README.new.md README.md 2>&1', ['/work/README.md'], id="input-or-fd-12"),
    pytest.param('mv README.new.md README.md < input.txt', ['/work/README.md'], id="input-or-fd-13"),
    pytest.param('mv README.new.md README.md 0<&3', ['/work/README.md'], id="input-or-fd-14"),
    pytest.param('mv README.new.md README.md <&-', ['/work/README.md'], id="input-or-fd-15"),
    pytest.param('mv README.new.md README.md 3< input.txt', ['/work/README.md'], id="input-or-fd-16"),
    pytest.param('mv README.new.md README.md 2>&-', ['/work/README.md'], id="input-or-fd-17"),
    pytest.param("sed -i 's/a/b/' README 2>&1", ['/work/README'], id="input-or-fd-18"),
    pytest.param("sed -i 's/a/b/' README < input.txt", ['/work/README'], id="input-or-fd-19"),
    pytest.param("sed -i 's/a/b/' README 0<&3", ['/work/README'], id="input-or-fd-20"),
    pytest.param("sed -i 's/a/b/' README <&-", ['/work/README'], id="input-or-fd-21"),
    pytest.param("sed -i 's/a/b/' README 3< input.txt", ['/work/README'], id="input-or-fd-22"),
    pytest.param("sed -i 's/a/b/' README 2>&-", ['/work/README'], id="input-or-fd-23"),
    pytest.param("perl -pi -e 's/a/b/' README 2>&1", ['/work/README'], id="input-or-fd-24"),
    pytest.param("perl -pi -e 's/a/b/' README < input.txt", ['/work/README'], id="input-or-fd-25"),
    pytest.param("perl -pi -e 's/a/b/' README 0<&3", ['/work/README'], id="input-or-fd-26"),
    pytest.param("perl -pi -e 's/a/b/' README <&-", ['/work/README'], id="input-or-fd-27"),
    pytest.param("perl -pi -e 's/a/b/' README 3< input.txt", ['/work/README'], id="input-or-fd-28"),
    pytest.param("perl -pi -e 's/a/b/' README 2>&-", ['/work/README'], id="input-or-fd-29"),
    pytest.param("cp src.txt '2>&1'", ['/work/2>&1'], id="quoted-fd-0"),
    pytest.param("install -m 644 src.txt '2>&1'", ['/work/2>&1'], id="quoted-fd-1"),
    pytest.param("mv src.txt '2>&1'", ['/work/2>&1'], id="quoted-fd-2"),
    pytest.param("sed -i 's/a/b/' '2>&1'", ['/work/2>&1'], id="quoted-fd-3"),
    pytest.param("perl -pi -e 's/a/b/' '2>&1'", ['/work/2>&1'], id="quoted-fd-4"),
    pytest.param('cp src.txt dst.txt > log.txt 2>&1 < input.txt', ['/work/log.txt', '/work/dst.txt'], id="combined-file-input-fd"),
])
def test_redirects_preserve_write_operands(command, expected):
    assert scan(command, "/work")[2] == expected


@pytest.mark.parametrize("redirect", ["<>", "><", "2>&", "<", ")"])
def test_unsupported_or_incomplete_redirects_refuse_operand_inference(redirect):
    assert not scan("cp src.txt dst.txt " + redirect, "/work")[2]


@pytest.mark.parametrize("command,cwd,expected", [
    pytest.param("DIR=/work; sed -i 's/a/b/' $DIR/file.txt", '/work', ['/work/file.txt'], id="recorded-0"),
    pytest.param('DIR=/work; cp source.txt "$DIR/file.txt"', '/work', ['/work/file.txt'], id="recorded-1"),
    pytest.param('DIR=/work; install -m 644 source.txt $DIR/file.txt', '/work', ['/work/file.txt'], id="recorded-2"),
    pytest.param('DIR=/work; mv source.txt $DIR/file.txt', '/work', ['/work/file.txt'], id="recorded-3"),
    pytest.param('DIR=dest; cp -t "$DIR" source.txt', '/work', ['/work/dest/source.txt'], id="recorded-4"),
    pytest.param('DIR=dest; install --target-directory="$DIR" source.txt', '/work', ['/work/dest/source.txt'], id="recorded-5"),
    pytest.param('DIR=dest; mv -t "$DIR" source.txt', '/work', ['/work/dest/source.txt'], id="recorded-6"),
    pytest.param('SRC=source.txt; cp $SRC dest/', '/work', ['/work/dest/source.txt'], id="recorded-7"),
    pytest.param("DIR=dest; cp source.txt '$DIR/file.txt'", '/work', ['/work/$DIR/file.txt'], id="recorded-8"),
    pytest.param("DIR=dest; sed -i 's/a/b/' '$DIR/file.txt'", '/work', ['/work/$DIR/file.txt'], id="recorded-9"),
    pytest.param("DIR=dest; cp -t '$DIR' source.txt", '/work', ['/work/$DIR/source.txt'], id="recorded-10"),
    pytest.param('cp source.txt "$UNKNOWN/file.txt"', '/work', [], id="recorded-11"),
    pytest.param("sed -i 's/a/b/' $UNKNOWN/file.txt", '/work', [], id="recorded-12"),
    pytest.param('cp -t $UNKNOWN source.txt', '/work', [], id="recorded-13"),
    pytest.param('DIR=$UNKNOWN; cp source.txt $DIR/file.txt', '/work', [], id="recorded-14"),
    pytest.param('DIR=/old; DIR=$UNKNOWN; mv source.txt $DIR/file.txt', '/work', [], id="recorded-15"),
    pytest.param('DIR=\'$LITERAL\'; cp source.txt "$DIR/file.txt"', '/work', ['/work/$LITERAL/file.txt'], id="recorded-16"),
    pytest.param("FILES='a.md b.md'; sed -i 's/a/b/' $FILES", '/work', [], id="quoted-or-ambiguous-0"),
    pytest.param('FILES="a.md b.md"; sed -i \'s/a/b/\' "$FILES"', '/work', ['/work/a.md b.md'], id="quoted-or-ambiguous-1"),
    pytest.param("DEST='a b'; cp source.txt $DEST", '/work', [], id="quoted-or-ambiguous-2"),
    pytest.param('DEST="a b"; cp source.txt "$DEST"', '/work', ['/work/a b'], id="quoted-or-ambiguous-3"),
    pytest.param("DEST='a b'; install -t $DEST source.txt", '/work', [], id="quoted-or-ambiguous-4"),
    pytest.param('DEST="a b"; install -t "$DEST" source.txt', '/work', ['/work/a b/source.txt'], id="quoted-or-ambiguous-5"),
    pytest.param("DEST='a b'; mv source.txt prefix${DEST}suffix", '/work', [], id="quoted-or-ambiguous-6"),
    pytest.param('DEST="a b"; mv source.txt prefix"${DEST}"suffix', '/work', ['/work/prefixa bsuffix'], id="quoted-or-ambiguous-7"),
    pytest.param("FILES='a.md\tb.md'; sed -i 's/a/b/' $FILES", '/work', [], id="quoted-or-ambiguous-8"),
    pytest.param("FILES='a.md\nb.md'; sed -i 's/a/b/' $FILES", '/work', [], id="quoted-or-ambiguous-9"),
    pytest.param("IFS=,; DEST='a,b'; cp source.txt $DEST", '/work', [], id="quoted-or-ambiguous-10"),
    pytest.param("IFS=$UNKNOWN; DEST='a,b'; cp source.txt $DEST", '/work', [], id="quoted-or-ambiguous-11"),
    pytest.param('IFS=,; DEST="a,b"; cp source.txt "$DEST"', '/work', ['/work/a,b'], id="quoted-or-ambiguous-12"),
    pytest.param("EXTRA='-t elsewhere'; cp source.txt $EXTRA final", '/work', [], id="quoted-or-ambiguous-13"),
    pytest.param('EXTRA=$UNKNOWN; cp source.txt $EXTRA final', '/work', [], id="quoted-or-ambiguous-14"),
    pytest.param("EMPTY=''; cp source.txt $EMPTY final", '/work', [], id="quoted-or-ambiguous-15"),
    pytest.param("cp source.txt 'literal space.txt'", '/work', ['/work/literal space.txt'], id="quoted-or-ambiguous-16"),
    pytest.param('DEST="a b"; cp source.txt \'$DEST\'', '/work', ['/work/$DEST'], id="quoted-or-ambiguous-17"),
    pytest.param("S=/tmp/s && cat > $S/f.py <<'EOF'\nx\nEOF", '/repo', ['/tmp/s/f.py'], id="assignment-and"),
    pytest.param("S=/tmp/s; cat > ${S}/f.py <<'EOF'\nx\nEOF", '/repo', ['/tmp/s/f.py'], id="assignment-semicolon"),
    pytest.param("S=/tmp/s cat > $S/f.py <<'EOF'\nx\nEOF", '/repo', ['/tmp/s/f.py'], id="assignment-prefix"),
])
def test_variable_write_operands(command, cwd, expected):
    assert scan(command, cwd)[2] == expected


@pytest.mark.parametrize("body,expected", [
    pytest.param("for path in ['a.md','b.md']:\n    open(path,'w').write(text)", ['/work/a.md', '/work/b.md'], id="literal-path-0"),
    pytest.param("for path in ('a.md','b.md'):\n    alias=path\n    p=Path(alias)\n    p.write_text(text)", ['/work/a.md', '/work/b.md'], id="literal-path-1"),
    pytest.param("for path in paths:\n    open(path,'w').write(text)", [], id="literal-path-2"),
    pytest.param("path='old.md'\nfor path in paths:\n    open(path,'w').write(text)\nopen(path,'w').write(text)", [], id="literal-path-3"),
    pytest.param("for path in ['a.md']:\n    path=unknown\n    open(path,'w').write(text)", [], id="literal-path-4"),
    pytest.param("for path in ['a.md']:\n    def unused():\n        open(path,'w').write(text)", [], id="literal-path-5"),
    pytest.param("for path in ['a.md']:\n    if Path(path).exists():\n        Path(path).write_text(text)", ['/work/a.md'], id="literal-path-6"),
    pytest.param("p='a.md'; alias=p; p=unknown\nopen(alias,'w').write(text)", ['/work/a.md'], id="literal-path-7"),
    pytest.param("p='a.md'\nimport unknown as p\nopen(p,'w').write(text)", [], id="literal-path-8"),
    pytest.param("for p in ['a.md']:\n    import unknown as p\n    open(p,'w').write(text)", [], id="literal-path-9"),
    pytest.param("for p in ['a.md']:\n    alias=p\n    with open(alias,'w') as handle:\n        handle.write(text)", ['/work/a.md'], id="literal-path-10"),
    pytest.param("p='old.md'\nfor item in ['a.md','b.md']:\n    open(p,'w')\n    p=item", ['/work/old.md', '/work/a.md'], id="body-binding-0"),
    pytest.param("p='old.md'\nfor item in ('a.md','b.md'):\n    alias=p\n    open(alias,'w')\n    p=item", ['/work/old.md', '/work/a.md'], id="body-binding-1"),
    pytest.param("p='old.md'\nfor item in ['a.md','b.md']:\n    open(p,'w')\n    if unknown:\n        p=item", ['/work/old.md'], id="body-binding-2"),
    pytest.param("p='old.md'\nfor item in ['a.md','b.md']:\n    open(p,'w')\n    p=unknown", ['/work/old.md'], id="body-binding-3"),
    pytest.param("for p in ['a.md']:\n    if (p := 'b.md'):\n        open(p,'w')\n    else:\n        open(p,'w')", [], id="condition-literal"),
    pytest.param("for p in ['a.md']:\n    if (p := unknown):\n        open(p,'w')\n    else:\n        open(p,'w')", [], id="condition-unknown"),
])
def test_python_literal_path_iterations(body, expected):
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


@pytest.mark.parametrize("body", [
    "for p in " + repr([f"{idx}.md" for idx in range(1000)]) + ":\n    open(p,'w')",
    "".join("    " * depth + "for p in ['a.md','b.md']:\n" for depth in range(10))
    + "    " * 10 + "open(p,'w')",
], ids=["flat-count", "nesting-depth"])
def test_python_path_iteration_bounds(body):
    assert not scan("python3 - <<'PY'\n" + body + "\nPY", "/work")[2]


@pytest.mark.parametrize("command,expected", [
    pytest.param('cat backend/parse.py', ('whole', ['/repo/backend/parse.py']), id="whole-0"),
    pytest.param('less /etc/hosts', ('whole', ['/etc/hosts']), id="whole-1"),
    pytest.param('cat a.py b.py', ('whole', ['/repo/a.py', '/repo/b.py']), id="whole-2"),
    pytest.param('grep -n TODO notes.md', ('slice', ['/repo/notes.md']), id="slice-0"),
    pytest.param("sed -n '1,200p' backend/schema.sql", ('slice', ['/repo/backend/schema.sql']), id="slice-1"),
    pytest.param('head -n 50 out.log', ('slice', ['/repo/out.log']), id="slice-2"),
    pytest.param('tail -20 build.txt', ('slice', ['/repo/build.txt']), id="slice-3"),
    pytest.param("awk '{print $1}' data.csv", ('slice', ['/repo/data.csv']), id="slice-4"),
    pytest.param('wc -l backend/parse.py', (None, []), id="measurement-wc"),
    pytest.param('ls -la backend/', (None, []), id="listing"),
    pytest.param('stat backend/parse.py', (None, []), id="metadata-stat"),
    pytest.param('git log --oneline -5', (None, []), id="git-log"),
    pytest.param('python3 script.py', (None, []), id="opaque-script"),
    pytest.param("grep -n 'a\\|b\\|c' backend/parse.py", ('slice', ['/repo/backend/parse.py']), id="quoted-alternation"),
])
def test_content_read_classification(command, expected):
    kind, reads, _ = scan(command, "/repo")
    assert (kind, reads) == expected


def test_value_flag_argument_is_not_a_file():
    _, reads, _ = scan("grep -e config.py -n TODO notes.md", "/repo")
    assert "/repo/notes.md" in reads


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


def test_unparsable_command_yields_nothing():
    """An unbalanced quote must produce no partial misreading."""
    kind, reads, writes = scan("cat 'unterminated.py", "/repo")
    assert (kind, reads, writes) == (None, [], [])


def test_duplicate_paths_collapse():
    _, reads, _ = scan("cat a.py a.py", "/repo")
    assert reads == ["/repo/a.py"]


@pytest.mark.parametrize("command,expected", [
    pytest.param("sed -i 's/a/b/' foo.py", (None, [], ['/repo/foo.py']), id="in-place"),
    pytest.param("sed -i.bak -e 's/a/b/' foo.py", (None, [], ['/repo/foo.py']), id="backup-expression"),
    pytest.param("sed --in-place 's/a/b/' foo.py", (None, [], ['/repo/foo.py']), id="long-in-place"),
    pytest.param("sed -i -n 's/a/b/p' foo.py", (None, [], ['/repo/foo.py']), id="in-place-quiet"),
    pytest.param("sed 's/a/b/' foo.py", ('slice', ['/repo/foo.py'], []), id="script-with-slashes"),
])
def test_sed_read_and_write_classification(command, expected):
    assert scan(command, "/repo") == expected


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


@pytest.mark.parametrize("command,expected", [
    pytest.param("python3 - <<'PY'\nopen('out.md', 'w').write('x')\nPY\n", ['/repo/out.md'], id="open-write"),
    pytest.param("python3 - <<'PY'\nwith open('out.md', mode='a') as fh:\n    fh.write('x')\nPY\n", ['/repo/out.md'], id="open-append-keyword"),
    pytest.param("python3 - <<'PY'\nimport pathlib\npathlib.Path('out.md').write_text('x')\nPY\n", ['/repo/out.md'], id="pathlib"),
    pytest.param("python3 - <<'PY'\nfrom pathlib import Path\np = Path('/abs/out.md')\np.write_text(p.read_text().replace('a', 'b'))\nPY\n", ['/abs/out.md'], id="path-replace"),
    pytest.param("python3 - <<'PY'\np = 'out.md'\nt = open(p).read()\nopen(p, 'w').write(t)\nPY\n", ['/repo/out.md'], id="path-binding"),
    pytest.param("python3 - <<'PY'\nprint(open('in.md').read())\nPY\n", [], id="read-only"),
    pytest.param('python3 -c "open(\'gen.txt\',\'w\').write(\'x\')"', ['/repo/gen.txt'], id="dash-c"),
    pytest.param(".venv/bin/python - <<'PY'\np = 'backend/x.py'; t = open(p).read()\nopen(p, 'w').write(t)\nPY\n", ['/repo/backend/x.py'], id="interpreter-path"),
])
def test_python_write_targets(command, expected):
    assert scan(command, "/repo")[2] == expected


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
    pytest.param("cat 'C:/Users/Sample/f.py'", 'D:\\other', ('whole', ['C:\\Users\\Sample\\f.py'], []), id="whole-path-0"),
    pytest.param("cat 'C:\\Users\\Sample\\f.py'", 'D:\\other', ('whole', ['C:\\Users\\Sample\\f.py'], []), id="whole-path-1"),
    pytest.param("cat 'C:/Users/Sample/f.py'", '/work', ('whole', ['C:\\Users\\Sample\\f.py'], []), id="whole-path-2"),
    pytest.param("cat 'C:/Users/Sample/./pkg/../f.py'", '', ('whole', ['C:\\Users\\Sample\\f.py'], []), id="whole-path-3"),
    pytest.param("cat 'src/f.py'", 'C:/Users\\Sample/work', ('whole', ['C:\\Users\\Sample\\work\\src\\f.py'], []), id="whole-path-4"),
    pytest.param("cat 'src\\f.py'", 'C:\\work', ('whole', ['C:\\work\\src\\f.py'], []), id="whole-path-5"),
    pytest.param("cat '..\\f.py'", 'C:\\work\\pkg', ('whole', ['C:\\work\\f.py'], []), id="whole-path-6"),
    pytest.param("cat 'C:\\work\\README'", 'D:\\other', ('whole', ['C:\\work\\README'], []), id="whole-path-7"),
    pytest.param("cat 'src\\README'", 'C:\\work', ('whole', ['C:\\work\\src\\README'], []), id="whole-path-8"),
    pytest.param("cat 'C:f.py'", 'C:\\work', ('whole', ['C:\\work\\f.py'], []), id="whole-path-9"),
    pytest.param("cat 'D:f.py'", 'C:\\work', ('whole', ['D:f.py'], []), id="whole-path-10"),
    pytest.param('cat /etc/hosts', 'C:\\work', ('whole', ['/etc/hosts'], []), id="whole-path-11"),
    pytest.param('cat /c/Users/Sample/f.py', 'C:\\work', ('whole', ['/c/Users/Sample/f.py'], []), id="whole-path-12"),
    pytest.param('cat //server/share/f.py', '/work', ('whole', ['//server/share/f.py'], []), id="whole-path-13"),
    pytest.param('cat pkg/../f.py', '/work', ('whole', ['/work/f.py'], []), id="whole-path-14"),
    pytest.param("cat 'pkg\\f.py'", '/work', ('whole', ['/work/pkg\\f.py'], []), id="whole-path-15"),
    pytest.param("cat '\\\\server\\share\\pkg\\..\\f.py'", 'C:\\work', ('whole', ['\\\\server\\share\\f.py'], []), id="whole-path-16"),
    pytest.param('cat f.py', '\\\\server\\share\\work', ('whole', ['\\\\server\\share\\work\\f.py'], []), id="whole-path-17"),
    pytest.param("cat '\\\\?\\C:\\work\\.\\f.py'", 'D:\\work', ('whole', ['\\\\?\\C:\\work\\.\\f.py'], []), id="whole-path-18"),
    pytest.param("grep 'word\\b' 'src\\f.py'", 'C:\\work', ('slice', ['C:\\work\\src\\f.py'], []), id="pattern-and-operand-backslashes"),
    pytest.param("grep 'C:\\Users\\Sample' docs.txt", '/work', ('slice', ['/work/docs.txt'], []), id="windows-pattern-posix-cwd"),
    pytest.param("grep 'C:\\Users\\Sample' docs.txt", 'C:/work', ('slice', ['C:\\work\\docs.txt'], []), id="windows-pattern-windows-cwd"),
])
def test_target_resolution_uses_recorded_path_flavor(command, cwd, expected):
    assert scan(command, cwd) == expected


@pytest.mark.parametrize("program", ["cp", "mv", "install -m 644"])
@pytest.mark.parametrize("operands,cwd,expected", [
    pytest.param("'D:\\src\\f.py' out/", 'C:\\work', 'C:\\work\\out\\f.py', id="source-flavor-0"),
    pytest.param("'src\\f.py' 'out\\'", 'C:\\work', 'C:\\work\\out\\f.py', id="source-flavor-1"),
    pytest.param("'D:\\src\\f.py' 'C:\\out\\'", 'C:\\work', 'C:\\out\\f.py', id="source-flavor-2"),
    pytest.param("'src\\f.py' out/", '/work', '/work/out/src\\f.py', id="source-flavor-3"),
    pytest.param("f.py '\\\\server\\share'", 'C:\\work', '\\\\server\\share\\f.py', id="source-flavor-4"),
    pytest.param("file.py '\\\\?\\C:\\out\\'", 'C:\\work', '\\\\?\\C:\\out\\file.py', id="device-drive"),
    pytest.param("file.py '\\\\?\\UNC\\server\\share\\out\\'", 'C:\\work', '\\\\?\\UNC\\server\\share\\out\\file.py', id="device-unc"),
    pytest.param("file.py '\\\\?\\C:\\out\\.\\'", 'C:\\work', '\\\\?\\C:\\out\\.\\file.py', id="verbatim-dot"),
    pytest.param("file.py '\\\\server\\share\\out\\'", 'C:\\work', '\\\\server\\share\\out\\file.py', id="unc"),
    pytest.param("file.py '/work/out/'", 'C:\\work', '/work/out/file.py', id="posix-destination"),
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


@pytest.mark.parametrize("command,cwd,expected", [
    pytest.param('cd "$MISSING" && cat f.py', 'C:/work', (None, [], []), id="unknown-cd-relative-read"),
    pytest.param('cd "C:/work/$MISSING" && cat f.py', 'C:/work', (None, [], []), id="partial-cd-relative-read"),
    pytest.param('cd "$MISSING" && cp f.py out/', 'C:/work', (None, [], []), id="unknown-cd-relative-write"),
    pytest.param('cd "$MISSING" && cat "C:/known/f.py"', 'D:/work', ('whole', ['C:\\known\\f.py'], []), id="unknown-cd-absolute-read"),
])
def test_targets_after_unknown_cd(command, cwd, expected):
    assert scan(command, cwd) == expected


def test_short_chained_assignment_expansion_keeps_existing_targets():
    command = "A0=xy " + " ".join(
        f"A{i}=$A{i - 1}$A{i - 1}" for i in range(1, 4)
    ) + " cat $A3/x"
    assert scan(command, "/repo") == (
        "whole", ["/repo/xyxyxyxyxyxyxyxy/x"], []
    )


def test_40_assignment_chain_is_bounded_and_keeps_literal_read():
    command = (
        "A0=xy " + " ".join(
            f"A{i}=$A{i - 1}$A{i - 1}" for i in range(1, 41)
        ) + " cat /tmp/$A40 /tmp/f"
    )
    code = """
import json
import sys
import time
from backend.bash_reads import scan
command = sys.stdin.read()
started = time.process_time()
result = scan(command)
elapsed = time.process_time() - started
print(json.dumps([result, elapsed]))
"""
    completed = _run_limited_python(code, command)
    assert completed.returncode == 0, completed.stderr
    result, elapsed = json.loads(completed.stdout)
    # An earlier doubled assignment exceeds the per-expansion cap, leaving
    # A40 unknown; the separate /tmp/f operand remains a whole-file read.
    assert result == ["whole", ["/tmp/f"], []]
    assert elapsed < 5.0


def test_scan_cumulative_expansion_budget_refuses_later_operands():
    value = "x" * 900_000
    command = "A=" + value + " cat " + " ".join(
        f"$A/{letter}" for letter in "abcde"
    )
    kind, reads, writes = scan(command)
    # Four 900k expansions fit under the 4M per-scan budget; the fifth
    # would exceed it even though each individual result is under 1M.
    assert kind == "whole"
    assert [path[-2:] for path in reads] == ["/a", "/b", "/c", "/d"]
    assert not writes


def test_parse_file_keeps_literal_read_after_assignment_expansion_cap():
    fixture = (Path(__file__).resolve().parents[1] / "fixtures" / "parser" /
               "bash_chained_assignment_budget.jsonl")
    code = """
import json
import sys
import time
from pathlib import Path
from backend.parse import parse_file
path = Path(sys.argv[1])
data = path.read_bytes()
started = time.process_time()
parsed = parse_file(path.name, data)
elapsed = time.process_time() - started
tool = parsed["tool_uses"][0]
print(json.dumps([tool["read_targets"], tool["read_kind"],
                  elapsed]))
"""
    completed = _run_limited_python(code, args=(str(fixture),))
    assert completed.returncode == 0, completed.stderr
    targets, kind, elapsed = json.loads(completed.stdout)
    assert targets == ["/tmp/f"]
    assert kind == "whole"
    assert elapsed < 5.0


@pytest.mark.parametrize("command", [
    "$(" * 1_000_000,
    "true " + "x " * 1_000_000,
], ids=["unclosed-command-substitution", "many-benign-words"])
def test_scan_command_skips_over_limit_text_without_tokenizing(command):
    code = """
import json
import sys
import time
from backend.bash_churn import BashCommand
from backend.bash_reads import scan, scan_command
text = sys.stdin.read()
started = time.process_time()
result = scan(text)
elapsed = time.process_time() - started
command = BashCommand(text)
scan_command(command)
print(json.dumps([result, elapsed, "tokens" in command.__dict__]))
"""
    completed = _run_limited_python(code, command)
    assert completed.returncode == 0, completed.stderr
    result, elapsed, tokens_were_computed = json.loads(completed.stdout)
    assert result == [None, [], []]
    assert not tokens_were_computed
    assert elapsed < 5.0


def test_command_at_size_limit_is_still_scanned():
    prefix = "cat /tmp/f; "
    command = prefix + "x" * (MAX_COMMAND_CHARS - len(prefix))
    assert len(command) == MAX_COMMAND_CHARS
    assert scan(command) == ("whole", ["/tmp/f"], [])
