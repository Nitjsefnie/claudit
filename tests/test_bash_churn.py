"""bash_churn.py — line churn recovered from Bash command text.

Most editing in a bypass-permissions session never touches Edit/Write:
it goes through a heredoc, an inline patch, or a python one-liner.
Payloads come from command text without execution. Recognized writes
with unknown addition sizes receive one estimated added line per call.
"""
import warnings

import pytest

from backend.bash_churn import bash_churn, churn_survives_error, python_write_paths, replace_churn


@pytest.mark.parametrize("command,expected", [
    ('echo "EXIT=$?" > out.txt', (1, 0)),
    ('echo "exit=$?" >> out.txt', (1, 0)),
    ('timeout 500 python3 -u scripts/run.py > out.txt 2>&1; echo "EXIT=$?" >> out.txt', (1, 0)),
    ('{ python3 scripts/run.py; echo "EXIT=$?"; } > out.txt', (1, 0)),
    ('echo "EXIT=$?" | tee out.txt copy.txt', (1, 0)),
    ('echo "EXIT=$?" < input.txt | tee out.txt', (1, 0)),
    ('echo "EXIT=$?" | tee out.txt 3< input.txt', (1, 0)),
    ('echo "EXIT=$?"', (0, 0)),
    ('echo "EXIT=$?" > /dev/null', (0, 0)),
    ('echo "EXIT=$?" | tee /dev/null', (0, 0)),
    ('echo "EXIT=$?" > /dev/null | tee out.txt', (0, 0)),
    ('echo "EXIT=$?" 2> out.txt', (0, 0)),
    ('echo "EXIT=$?" | sed s/EXIT/exit/ > out.txt', (1, 0)),
    ("echo 'echo \"EXIT=$?\" > out.txt'", (0, 0)),
    ('echo "$UNKNOWN" > out.txt', (1, 0)),
    ('echo "EXIT=$?$UNKNOWN" > out.txt', (1, 0)),
    ('echo "EXIT=$?" "$UNKNOWN" > out.txt', (1, 0)),
    ('echo -e "EXIT=$?" > out.txt', (1, 0)),
])
def test_exit_marker_payloads(command, expected):
    assert bash_churn(command) == expected


@pytest.mark.parametrize("redirect", [
    "< /dev/null", "< input.txt", "0<&3", "<&-", "0< input.txt",
])
def test_receiving_stdin_redirect_severs_exit_marker(redirect):
    assert bash_churn('echo "EXIT=$?" | tee out.txt ' + redirect) == (0 if redirect in ('< /dev/null', '<&-') else 1, 0)


def test_receiving_heredoc_replaces_exit_marker():
    command = 'echo "EXIT=$?" | tee out.txt <<\'EOF\'\na\nb\nEOF'
    assert bash_churn(command) == (2, 0)


@pytest.mark.parametrize("command,expected", [
    ("printf '%s' {text} > out.txt", (1, 0)),
    ("printf '%s\\n' a{b,c} > out.txt", (0, 0)),
    ("printf '%s\\n' '{a,b}' > out.txt", (1, 0)),
    ("{ printf '%s\\n' {text}; } > out.txt", (1, 0)),
])
def test_brace_words_preserve_literal_payload_and_group_boundaries(command, expected):
    assert bash_churn(command) == expected


@pytest.mark.parametrize("command,expected", [
    ("printf 'a\\nb\\n' | tee out.txt <<'EOF'\nx\nEOF", (1, 0)),
    ("printf 'a\\nb\\n' | tee out.txt 0<<'EOF'\nx\nEOF", (1, 0)),
    ("printf 'a\\nb\\n' | tee out.txt <<-'EOF'\nx\nEOF", (1, 0)),
    ("printf 'a\\nb\\n' | tee out.txt", (2, 0)),
    ("tee out.txt <<'EOF'\nx\nEOF", (1, 0)),
    ("printf 'a\\nb\\n' | tee first.txt\ntee out.txt <<'EOF'\nx\nEOF", (3, 0)),
])
def test_heredoc_override_preserves_receiving_stdin_provenance(command, expected):
    assert bash_churn(command) == expected


@pytest.mark.parametrize("redirect", ["< /dev/null", "< input.txt", "0<&3", "<&-", "0< input.txt"])
def test_receiving_stdin_redirect_severs_printf_payload(redirect):
    assert bash_churn(r"printf 'a\nb\n' | tee out.txt " + redirect) == (0 if redirect in ("< /dev/null", "<&-") else 1, 0)


@pytest.mark.parametrize("command", [
    r"printf 'a\nb\n' < input.txt | tee out.txt",
    r"printf 'a\nb\n' | tee out.txt 3< input.txt",
    r"printf 'a\nb\n' | tee first.txt | tee second.txt < input.txt",
])
def test_stdin_redirect_does_not_erase_independent_file_payload(command):
    assert bash_churn(command) == (2, 0)


def test_loop_carried_path_bindings_do_not_multiply_churn():
    command = ("python3 - <<'PY'\np='old.md'\n"
               "for item in ['a.md','b.md']:\n"
               "    open(p,'w').write('literal\\npayload')\n"
               "    p=item\nPY")
    assert python_write_paths(command) == ["old.md", "a.md"]
    assert bash_churn(command) == (1, 0)


@pytest.mark.parametrize("command,expected", [
    (r"printf 'a\nb\nc\n' > probe.txt", (3, 0)),
    (r"printf '%s\n' 'a' 'b' > probe.txt", (2, 0)),
    (r"printf '%% %s %s\n' a b c d > probe.txt", (2, 0)),
    (r"printf '%s' a b > probe.txt", (1, 0)),
    (r"printf '%s' 2 > probe.txt", (1, 0)),
    (r"printf 'a\n' 2> probe.txt", (0, 0)),
    (r"printf 'a\n' >&2", (0, 0)),
    (r"printf 'a\n' > probe.txt 2>&1", (1, 0)),
    (r"printf 'a\n' | tee --unknown probe.txt", (0, 0)),
    (r"printf 'a\n' | tee /dev/null > probe.txt", (1, 0)),
    (r"printf 'a\n' > /dev/null | tee probe.txt", (0, 0)),
    (r"printf '%s\n' '$LITERAL' > probe.txt", (1, 0)),
    (r"printf 'a\n' | tee a.txt b.txt", (1, 0)),
    (r"printf 'a\n' | tee /dev/null", (0, 0)),
    (r"printf 'a\n' > /dev/null", (0, 0)),
    (r"printf 'a\n'", (0, 0)),
    (r'printf "%s\n" "$UNKNOWN" > probe.txt', (1, 0)),
    ("printf '%s\\n' \"'$UNKNOWN'\" > probe.txt", (1, 0)),
    ("sed -i '2a text\ns/old/new/' README", (1, 0)),
    (r"sed -i '2a one\\ntwo' README", (1, 0)),
    (r"printf '%d\n' 42 > probe.txt", (1, 0)),
    (r"printf 'a\n' | sed s/a/b/ > probe.txt", (1, 0)),
    (r"printf 'a\n' | tee a.txt | sed s/a/b/ > b.txt", (1, 0)),
    (r"{ printf 'a\n'; sed -n '1,2p' old.txt; printf 'b\n'; } > probe.txt", (2, 0)),
    ("{\n printf 'a\\n';\n} > README.new.md\nmv README.new.md README.md\n", (1, 0)),
    (r"cd /work && { printf 'a\n'; sed -n '1,3p' README.md; } > README.new.md && mv README.new.md README.md", (1, 0)),
    (r"{ printf 'a\n'; } | sed s/a/b/ > probe.txt", (1, 0)),
    (r"{ printf 'a\n' > /dev/null; } > probe.txt", (0, 0)),
    # Escaped quotes are literal bytes, so this > is real shell syntax.
    (r"echo \"printf 'a\\n' > probe.txt\"", (1, 0)),
    (r"printf 'a\n' > \"$OUT\"", (1, 0)),
    ("printf 'unterminated", (0, 0)),
    (r"sed -i '311a !tests/docs/\ntests/docs/*\n!tests/docs/*.ts' .gitignore", (3, 0)),
    (r"sed '2a one\ntwo' README > out.txt", (2, 0)),
    (r"sed '2a one\ntwo' README | head -1 > out.txt", (1, 0)),
    (r"sed -i '2a text' README > out.txt", (1, 0)),
    (r"sed '2a one\ntwo' README", (0, 0)),
    (r"sed -i 's/a/b/' README", (1, 1)),
    (r"sed -i '/regex/a text' README", (1, 0)),
    (r"sed -i -f dynamic.sed -e '2a text' README", (1, 0)),
    (r"sed -i '2a text'", (0, 0)),
    ("sed -i '2a\\\none\\\ntwo' README", (2, 0)),
    ("cp src.txt dst.txt; mv dst.txt final.txt", (1, 0)),
    (r"perl -pi -e 's/(a)/$1$1/g' file.ts", (1, 0)),
])
def test_literal_shell_payloads(command, expected):
    assert bash_churn(command) == expected


@pytest.mark.parametrize("body,expected", [
    ("marker='end\\n'; entry='new\\n'; p=Path('README.md')\n"
     "p.write_text(p.read_text().replace(marker, entry + marker))", (1, 0)),
    ("old='a'; new=old + '\\nb'; new=new + '\\nc'\n"
     "open('f','w').write(text.replace(old, new))", (3, 1)),
    ("new='a'; new=unknown + new\nopen('f','w').write(text.replace('b',new))", (1, 0)),
    ("new=1 + 'a'\nopen('f','w').write(text.replace('b',new))", (1, 0)),
    ("new=Path('a') + 'b'\nopen('f','w').write(text.replace('b',new))", (1, 0)),
    ("p=Path('a'); new=p + 'b'\nopen('f','w').write(text.replace('b',new))", (1, 0)),
    ("for p in ['a.md', 'b.md']:\n    if Path(p).exists():\n"
     "        Path(p).write_text(text.replace('old','new\\nnew'))", (1, 0)),
])
def test_python_bounded_literal_expressions(body, expected):
    assert bash_churn("python3 - <<'PY'\n" + body + "\nPY") == expected


def test_python_concatenation_payload_growth_is_bounded():
    body = "x='a'\n" + "x=x+x\n" * 30 + "open('f','w').write(x)"
    assert bash_churn("python3 - <<'PY'\n" + body + "\nPY") == (1, 0)


def test_python_concatenation_depth_is_bounded():
    body = "new=" + "+".join(["'a'"] * 100) + "\nopen('f','w').write(new)"
    assert bash_churn("python3 - <<'PY'\n" + body + "\nPY") == (1, 0)


# --- heredoc bodies redirected into a file --------------------------------

def test_cat_heredoc_into_file_counts_body_lines():
    assert bash_churn("cat > /tmp/f.txt <<'EOF'\nalpha\nbeta\nEOF\n") == (2, 0)


def test_cat_append_heredoc_counts_body_lines():
    """An append adds lines and deletes none — the same shape as Write."""
    assert bash_churn("cat >> notes.md <<'EOF'\na\nb\nc\nEOF\n") == (3, 0)


def test_overwrite_heredoc_reports_zero_deletions():
    """`cat > F` on an existing file destroys its old content, but the
    call does not carry it — the same limitation Write has, counted the
    same way (0 deletions) rather than guessed."""
    assert bash_churn("cat > existing.py <<'EOF'\nnew\nEOF\n") == (1, 0)


def test_tee_heredoc_counts_body_lines():
    assert bash_churn("tee -a /etc/hosts <<'EOF'\n1.2.3.4 host\nEOF\n") == (1, 0)


def test_redirect_written_after_the_opener_still_counts():
    assert bash_churn("cat <<'EOF' > out.txt\nx\ny\nEOF\n") == (2, 0)


def test_empty_heredoc_body_counts_zero():
    assert bash_churn("cat > f <<'EOF'\nEOF\n") == (0, 0)


def test_unquoted_heredoc_tag_is_recognised():
    assert bash_churn("cat > f <<EOF\none\ntwo\nEOF\n") == (2, 0)


def test_dash_suppressed_heredoc_terminator_is_recognised():
    """<<- allows a tab-indented terminator; the body still counts."""
    assert bash_churn("cat > f <<-'EOF'\n\ta\n\tb\n\tEOF\n") == (2, 0)


def test_multiple_file_heredocs_in_one_command_sum():
    cmd = ("cat > a <<'A'\n1\n2\nA\n"
           "cat >> b <<'B'\n3\nB\n")
    assert bash_churn(cmd) == (3, 0)


# --- heredocs that are NOT file content ------------------------------------

def test_commit_message_heredoc_is_not_churn():
    cmd = ("git add x && git commit -q -F - <<'EOF'\n"
           "fix: a thing\n\nbody line\nEOF\n")
    assert bash_churn(cmd) == (0, 0)


def test_commit_message_via_command_substitution_is_not_churn():
    cmd = "git commit -m \"$(cat <<'EOF'\nsubject\n\nbody\nEOF\n)\"\n"
    assert bash_churn(cmd) == (0, 0)


def test_psql_heredoc_is_not_churn():
    assert bash_churn("psql -d db <<'SQL'\nSELECT 1;\nSQL\n") == (0, 0)


def test_output_capture_redirect_uses_write_estimate():
    """Redirecting a command's OUTPUT to a file is not enumerable churn:
    the fallback estimates one addition without executing it."""
    assert bash_churn("python3 -m pytest tests/ -q > /tmp/out.txt") == (1, 0)


def test_plain_command_yields_zero():
    assert bash_churn("ls -la /root && git status --porcelain") == (0, 0)


def test_empty_command_yields_zero():
    assert bash_churn("") == (0, 0)


# --- inline patches ---------------------------------------------------------

def test_inline_git_apply_counts_hunk_lines():
    """+/- hunk lines are exact churn; the +++/--- file headers are not."""
    cmd = ("git apply <<'PATCH'\n"
           "--- a/x.py\n"
           "+++ b/x.py\n"
           "@@ -1,3 +1,4 @@\n"
           " ctx\n"
           "-old one\n"
           "-old two\n"
           "+new one\n"
           "+new two\n"
           "+new three\n"
           "PATCH\n")
    assert bash_churn(cmd) == (3, 2)


def test_inline_patch_command_counts_hunk_lines():
    cmd = ("patch -p1 <<'DIFF'\n"
           "--- a/y\n"
           "+++ b/y\n"
           "@@ -1 +1 @@\n"
           "-a\n"
           "+b\n"
           "DIFF\n")
    assert bash_churn(cmd) == (1, 1)


# --- python bodies ----------------------------------------------------------

def test_python_heredoc_literal_replace_counts_both_sides():
    cmd = ("python3 - <<'PY'\n"
           "import pathlib\n"
           "p = pathlib.Path('backend/db.py')\n"
           "s = p.read_text()\n"
           "s = s.replace('a\\nb', 'x\\ny\\nz')\n"
           "p.write_text(s)\n"
           "PY\n")
    assert bash_churn(cmd) == (3, 2)


def test_python_heredoc_re_sub_literals_counted():
    cmd = ("python3 - <<'PY'\n"
           "import re, pathlib\n"
           "p = pathlib.Path('f')\n"
           "p.write_text(re.sub('old', 'new\\nnew2', p.read_text()))\n"
           "PY\n")
    assert bash_churn(cmd) == (2, 1)


def test_python_heredoc_that_never_writes_is_not_churn():
    """A read-and-print script mutates nothing on disk, however many
    replace() calls it makes on the way to stdout."""
    cmd = ("python3 - <<'PY'\n"
           "s = open('f').read()\n"
           "print(s.replace('a', 'b\\nc'))\n"
           "PY\n")
    assert bash_churn(cmd) == (0, 0)


def test_python_replace_with_non_literal_arguments_uses_write_estimate():
    """Unknown replacement sizes receive the per-call write estimate."""
    cmd = ("python3 - <<'PY'\n"
           "import pathlib, sys\n"
           "old, new = sys.argv[1], sys.argv[2]\n"
           "p = pathlib.Path('f')\n"
           "p.write_text(p.read_text().replace(old, new))\n"
           "PY\n")
    assert bash_churn(cmd) == (1, 0)


def test_python_write_text_of_a_string_literal_counts_as_added():
    cmd = ("python3 - <<'PY'\n"
           "import pathlib\n"
           "pathlib.Path('f').write_text('one\\ntwo\\nthree\\n')\n"
           "PY\n")
    assert bash_churn(cmd) == (3, 0)


def test_python_dash_c_literal_replace_is_counted():
    cmd = ("python3 -c 'import pathlib\np=pathlib.Path(\"f\")\n"
           "p.write_text(p.read_text().replace(\"a\", \"b\\nc\"))'")
    assert bash_churn(cmd) == (2, 1)


def test_syntactically_invalid_python_yields_zero():
    cmd = ("python3 - <<'PY'\n"
           "def broken(:\n"
           "PY\n")
    assert bash_churn(cmd) == (0, 0)


def test_non_python_interpreter_heredoc_yields_zero():
    cmd = ("node - <<'JS'\n"
           "require('fs').writeFileSync('f', 'a\\nb')\n"
           "JS\n")
    assert bash_churn(cmd) == (0, 0)


def test_python_replace_through_single_assignment_literals_is_counted():
    """The shape most edits actually take: bind old/new to literals,
    assert the match is unique, then write back. The literals are right
    there in the script — binding them to a name does not make them
    unknowable."""
    cmd = ("python3 - <<'PY'\n"
           "import pathlib\n"
           "p = pathlib.Path('enroll.go')\n"
           "old = 'a\\nb\\n'\n"
           "new = 'x\\n'\n"
           "s = p.read_text()\n"
           "assert s.count(old) == 1\n"
           "p.write_text(s.replace(old, new))\n"
           "PY\n")
    assert bash_churn(cmd) == (1, 2)


def test_python_replace_through_a_rebound_name_uses_write_estimate():
    """A name assigned more than once holds whichever value the run
    produced — not enumerable from the text."""
    cmd = ("python3 - <<'PY'\n"
           "import pathlib\n"
           "p = pathlib.Path('f')\n"
           "new = 'one'\n"
           "new = new + open('other').read()\n"
           "p.write_text(p.read_text().replace('old', new))\n"
           "PY\n")
    assert bash_churn(cmd) == (1, 0)


def test_python_replace_through_a_loop_variable_uses_write_estimate():
    cmd = ("python3 - <<'PY'\n"
           "import pathlib\n"
           "p = pathlib.Path('f')\n"
           "s = p.read_text()\n"
           "for new in ('a', 'b'):\n"
           "    s = s.replace('old', new)\n"
           "p.write_text(s)\n"
           "PY\n")
    assert bash_churn(cmd) == (1, 0)


def test_parsing_a_body_with_a_bad_escape_emits_no_warning():
    """Transcript scripts are arbitrary third-party text — `\\$` in a
    shell-ish literal is legal enough to run and warned about by the
    compiler. Ingest parses ~200k of them, so a leaked SyntaxWarning is
    thousands of journald lines per run, from files nobody will edit."""
    cmd = ("python3 - <<'PY'\n"
           "import pathlib\n"
           "pathlib.Path('f').write_text('cost: \\$5')\n"
           "PY\n")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        bash_churn(cmd)
    assert [str(w.message) for w in caught] == []


def test_python_replace_through_sequentially_rebound_names_is_counted():
    """Two edits in one script, each rebinding `old`/`new` before its
    own replace: at every replace the names hold a literal, so both
    are enumerable — the binding in force is the one just above."""
    cmd = ("python3 - <<'PY'\n"
           "p = 'a.md'; t = open(p).read()\n"
           "old = 'x\\ny'\n"
           "new = 'z'\n"
           "open(p, 'w').write(t.replace(old, new))\n"
           "p = 'b.md'; t = open(p).read()\n"
           "old = 'q'\n"
           "new = 'r\\ns\\nt'\n"
           "open(p, 'w').write(t.replace(old, new))\n"
           "PY\n")
    assert bash_churn(cmd) == (4, 3)


def test_python_replace_through_a_local_helper_is_counted():
    """`sub(path, old, new)` defined in the same script and called with
    literals is the literal replace with one indirection."""
    cmd = ("python3 - <<'PY'\n"
           "import re\n"
           "def sub(path, old, new):\n"
           "    t = open(path, encoding='utf-8').read()\n"
           "    assert old in t, (path, old)\n"
           "    open(path, 'w', encoding='utf-8').write(t.replace(old, new))\n"
           "sub('a.md', 'one\\ntwo', 'three')\n"
           "sub('b.md', 'four', 'five\\nsix')\n"
           "PY\n")
    assert bash_churn(cmd) == (3, 3)


def test_python_helper_called_with_variable_strings_uses_write_estimate():
    cmd = ("python3 - <<'PY'\n"
           "import sys\n"
           "def sub(path, old, new):\n"
           "    open(path, 'w').write(open(path).read().replace(old, new))\n"
           "sub('a.md', sys.argv[1], sys.argv[2])\n"
           "PY\n")
    assert bash_churn(cmd) == (1, 0)


def test_python_helper_body_is_not_counted_on_its_own():
    """The replace inside the helper runs once PER CALL; with no call
    it never runs."""
    cmd = ("python3 - <<'PY'\n"
           "def sub(path, old, new):\n"
           "    open(path, 'w').write(open(path).read().replace(old, new))\n"
           "PY\n")
    assert bash_churn(cmd) == (0, 0)


# --- does an errored result mean the write did not happen? ------------

def test_heredoc_write_followed_by_other_stages_survives_an_error():
    """`cat > f <<EOF … EOF && python3 f` failing in python3 wrote f
    all the same; the exit status belongs to the last stage."""
    cmd = "cat > f.py <<'EOF'\nraise SystemExit(1)\nEOF\npython3 f.py"
    assert churn_survives_error(cmd, "Exit code 1") is True


def test_lone_heredoc_write_does_not_survive_an_error():
    cmd = "cat > missing/f.py <<'EOF'\nx\nEOF\n"
    assert churn_survives_error(cmd, "bash: missing/f.py: No such file or directory") is False


def test_heredoc_write_whose_target_failed_does_not_survive():
    cmd = "mkdir -p d && cat > d/f.py <<'EOF'\nx\nEOF\npython3 d/f.py"
    text = "bash: d/f.py: Permission denied"
    assert churn_survives_error(cmd, text) is False


@pytest.mark.parametrize("text", [
    "No space left on device", "Read-only file system",
])
def test_disk_failure_never_survives(text):
    cmd = "cat > f.py <<'EOF'\nx\nEOF\npython3 f.py"
    assert churn_survives_error(cmd, text) is False


def test_python_edit_body_does_not_survive_an_error():
    """A python body that raised may have raised BEFORE its write —
    `assert old in t` is put there to do exactly that."""
    cmd = ("python3 - <<'PY'\n"
           "t = open('f').read(); assert 'a' in t\n"
           "open('f', 'w').write(t.replace('a', 'b'))\n"
           "PY\n"
           "git diff --stat")
    assert churn_survives_error(cmd, "AssertionError") is False


@pytest.mark.parametrize("interp", [
    ".venv/bin/python", "/usr/bin/python3", "./venv/bin/python3.13",
])
def test_interpreter_given_by_path_is_still_python(interp):
    """A venv's python is the common spelling in a repo with one;
    `.venv/bin/python -` edits exactly like `python3 -` does."""
    cmd = (f"{interp} - <<'PY'\n"
           "p = 'f.md'\n"
           "t = open(p).read()\n"
           "open(p, 'w').write(t.replace('a\\nb', 'c'))\n"
           "PY\n")
    assert bash_churn(cmd) == (1, 2)


def test_interpreter_given_by_path_dash_c_is_still_python():
    cmd = ".venv/bin/python -c \"open('f','w').write('x\\ny')\""
    assert bash_churn(cmd) == (2, 0)


@pytest.mark.parametrize("command,expected", [
    ("generate > out.txt", (1, 0)),
    ("generate > out.txt; cp a.txt b.txt; cp c.txt d.txt", (1, 0)),
    ("cp source.txt dest.txt", (1, 0)),
    ("cp /dev/null dest.txt", (0, 0)),
    ("echo marker > out.txt", (1, 0)),
    ("echo -n '' > out.txt", (0, 0)),
    ("echo '' > out.txt", (1, 0)),
    (r"echo -e 'one\ntwo' > out.txt", (2, 0)),
    (r"echo -e '\cignored' > out.txt", (0, 0)),
    ('echo "$UNKNOWN" > out.txt', (1, 0)),
    (r"printf 'one\ntwo\n' > out.txt; generate > other.txt", (2, 0)),
    ("printf '' > out.txt; generate > other.txt", (1, 0)),
    ("printf '' > out.txt", (0, 0)),
    ("cat file.txt", (0, 0)),
    ("generate > /dev/null", (0, 0)),
    ("echo 'generate > out.txt'", (0, 0)),
    ("python3 mysterious.py --output out.txt", (0, 0)),
    ("sed -i 's/old/new/' out.txt", (1, 1)),
    ("sed -i 's/old/new/3g' out.txt other.txt", (1, 1)),
    (r"sed -i 's|old\|text|new\nline|' out.txt", (2, 1)),
    (r"sed -i 's/old\.text/new\&text/' out.txt", (1, 1)),
    (r"sed -i 's/old//g' out.txt", (0, 1)),
    ("sed -i '2d' out.txt", (0, 0)),
    ("sed -i 's/old//'; generate > out.txt", (1, 0)),
    (r"sed -i 's/(old)/\1/g' out.txt", (1, 0)),
    ("sed -i 's/.*old/new/' out.txt", (1, 0)),
    ("sed -i 's/old/new/' /dev/null", (0, 0)),
])
def test_write_estimates_and_declared_payloads(command, expected):
    assert bash_churn(command) == expected


@pytest.mark.parametrize("body,expected", [
    ("open(path, 'w').write(computed)", (1, 0)),
    ("Path(path).write_text(computed)", (1, 0)),
    ("p=Path(path); p.write_text(computed)", (1, 0)),
    ("open(path, 'w').write('')", (0, 0)),
    ("Path(path).write_text('')", (0, 0)),
    ("Path(path).write_bytes(b'')", (0, 0)),
    ("Path('/dev/null').write_text(computed)", (0, 0)),
    ("open('/dev/null', 'w').write(computed)", (0, 0)),
    ("obj.write(computed)", (0, 0)),
    ("obj.write_text(computed)", (0, 0)),
    ("obj.open(path, 'w')", (0, 0)),
    ("open(path).read()", (0, 0)),
    # Removing text inside a line modifies that line, as git counts it.
    ("open(path, 'w').write(text.replace('old', ''))", (1, 1)),
    ("open(path, 'w').write(text.replace('old', '')); open(other, 'w').write(computed)", (1, 1)),
])
def test_python_write_estimate_boundaries(body, expected):
    assert bash_churn("python3 - <<'PY'\n" + body + "\nPY") == expected


@pytest.mark.parametrize("command,expected", [
    ('cp "$SOURCE" "$DEST"', (1, 0)),
    ("sed -i 's/.*//' out.txt", (0, 0)),
    (r"echo -e '\\c' > out.txt", (1, 0)),
    ("generate 2> out.txt", (1, 0)),
    ("sed -i 's/old/new/' out.txt; sed -i 's/old/new/' other.txt", (2, 2)),
    ("python3 -c \"obj.write('one\\ntwo')\"", (0, 0)),
    ("python3 -c \"Path('/dev/null').write_text('one\\ntwo')\"", (0, 0)),
    ("python3 -c \"Path(path).write_bytes(b'one\\ntwo')\"", (2, 0)),
    ("python3 - <<'PY'\nfor p in paths:\n    Path(p).write_text(computed)\nPY", (1, 0)),
])
def test_additional_write_boundaries(command, expected):
    assert bash_churn(command) == expected


@pytest.mark.parametrize("body,expected", [
    ("open(path, 'w').write(text.replace(old, 'new'))", (1, 0)),
    ("open(path, 'w').write(text.replace(old, ''))", (0, 0)),
    ("with open(path, 'w') as f:\n    f.write(computed)", (1, 0)),
    ("with open(path, 'w') as f:\n    f.write('')", (0, 0)),
])
def test_python_replacement_and_handle_sizes(body, expected):
    assert bash_churn("python3 - <<'PY'\n" + body + "\nPY") == expected


def test_python_helper_does_not_infer_file_write_from_arbitrary_method():
    command = "python3 - <<'PY'\ndef send(obj):\n    obj.write(data)\nsend(client)\nPY"
    assert bash_churn(command) == (0, 0)


@pytest.mark.parametrize("body", [
    "def truncate(path):\n    open(path, 'w').close()\ntruncate('out.txt')",
    "def empty(path):\n    open(path, 'w').write('')\nempty('out.txt')",
    "for p in paths:\n    Path(p).write_bytes(b'')",
    "for p in paths:\n    Path(p).write_text(text.replace('old', ''))",
])
def test_known_no_additions_in_helpers_and_loops(body):
    assert bash_churn("python3 - <<'PY'\n" + body + "\nPY") == (0, 0)


@pytest.mark.parametrize("command,expected", [
    ("python3 - <<'PY'\ndef emit(path, text):\n    open(path, 'w').write(text)\nemit('out.txt', '')\nPY", (0, 0)),
    ("python3 - <<'PY'\ndef outer(path):\n    def inner():\n        open(path, 'w').write('x')\nouter('out.txt')\nPY", (0, 0)),
    ("cp -f /dev/null out.txt", (0, 0)),
    ("perl -pi -e 's/a/b/' \"$OUT\"", (1, 0)),
])
def test_review_write_estimate_boundaries(command, expected):
    assert bash_churn(command) == expected


@pytest.mark.parametrize("command,expected", [
    ("python3 - <<'PY'\ndef emit(path, text):\n    open(path, 'wb').write(text)\nemit('out.txt', b'')\nPY", (0, 0)),
    ("python3 - <<'PY'\ndef emit(path, text):\n    open(path, 'w').write(text)\nempty=''\nemit('out.txt', empty)\nPY", (0, 0)),
    ("python3 - <<'PY'\ndef emit(path, text):\n    open(path, 'w').write(text)\nemit('out.txt', computed)\nPY", (1, 0)),
    ("python3 - <<'PY'\ndef outer(path):\n    def inner():\n        open(path, 'w').write(computed)\nouter('out.txt')\nPY", (0, 0)),
    ("python3 - <<'PY'\ndef direct(path):\n    open(path, 'w').write(computed)\ndirect('out.txt')\nPY", (1, 0)),
    ("cp -f -t outdir /dev/null", (0, 0)),
    ("cp --target-directory=outdir /dev/null", (0, 0)),
    ("cp -- /dev/null out.txt", (0, 0)),
    ("cp -f /dev/null \"$OUT\"", (0, 0)),
    ("cp -S /dev/null source.txt out.txt", (1, 0)),
    ("cp -t outdir /dev/null source.txt", (1, 0)),
    ("perl -pi -e 's/a/b/' /dev/null", (0, 0)),
    ("perl -p -e 's/a/b/' \"$OUT\"", (0, 0)),
])
def test_review_write_estimate_neighbors(command, expected):
    assert bash_churn(command) == expected


@pytest.mark.parametrize("body,argument,expected", [
    ("text = computed\n    open(path, 'w').write(text)", "''", (1, 0)),
    ("text = 'generated\\nsecond'\n    open(path, 'w').write(text)", "''", (2, 0)),
    ("text = ''\n    open(path, 'w').write(text)", "'incoming'", (0, 0)),
    ("text = b''\n    open(path, 'wb').write(text)", "b'incoming'", (0, 0)),
    ("open(path, 'w').write(text)\n    text = computed", "''", (0, 0)),
    ("local = computed\n    open(path, 'w').write(text)", "''", (0, 0)),
    ("local = text\n    text = computed\n    open(path, 'w').write(local)", "''", (0, 0)),
    ("local = 'new\\nlines'\n    text = local\n    open(path, 'w').write(text)", "''", (2, 0)),
    ("text = 'new'\n    open(path, 'w').write(text)\n    text = ''", "''", (1, 0)),
    ("text = text + '\\nnew'\n    open(path, 'w').write(text)", "'old'", (2, 0)),
    ("def inner():\n        text = computed\n    open(path, 'w').write(text)", "''", (0, 0)),
])
def test_helper_payload_binding_order(body, argument, expected):
    command = "python3 - <<'PY'\ndef emit(path, text):\n    " + body + "\nemit('out.txt', " + argument + ")\nPY"
    assert bash_churn(command) == expected


@pytest.mark.parametrize("body,expected", [
    ("ignored = 'old'.replace('old', 'new\\nline')\n    open(path, 'w').write('')", (0, 0)),
    ("text = 'old'.replace('old', 'new\\nline')\n    text = ''\n    open(path, 'w').write(text)", (0, 0)),
    ("text = 'old'.replace('old', 'new\\nline')\n    open(path, 'w').write(text)", (2, 1)),
    ("text = 'old'.replace('old', 'new\\nline')\n    alias = text\n    text = ''\n    open(path, 'w').write(alias)", (2, 1)),
    ("text = 'old'.replace('old', 'new\\nline')\n    alias = text\n    alias = ''\n    open(path, 'w').write(alias)", (0, 0)),
    ("open(path, 'w').write('')\n    ignored = 'old'.replace('old', 'new\\nline')", (0, 0)),
    ("text = 'old'.replace('old', 'new\\nline')\n    open(path, 'w').write(text)\n    text = ''", (2, 1)),
    ("text = content.replace('old', 'new\\nline')\n    text = text.replace('line', 'last')\n    open(path, 'w').write(text)", (3, 2)),
    ("text = content.replace('old', 'new\\nline')\n    text = re.sub('line', 'last', text)\n    open(path, 'w').write(text)", (3, 2)),
    ("text = content.replace('old', 'new\\nline')\n    with open(path, 'w') as handle:\n        handle.write(text)", (2, 1)),
    ("text = content.replace('old', 'new\\nline')\n    client.write(text)\n    open(path, 'w').write('')", (0, 0)),
    ("text = content.replace('old', 'new\\nline')\n    sys.stdout.write(text)\n    open(path, 'w').write('')", (0, 0)),
    ("text = content.replace('old', 'new\\nline')\n    open(path, 'w').write(text)\n    open(path, 'a').write(text)", (2, 1)),
])
def test_helper_edits_only_count_when_written(body, expected):
    command = "python3 - <<'PY'\ndef emit(path):\n    " + body + "\nemit('out.txt')\nPY"
    assert bash_churn(command) == expected


@pytest.mark.parametrize("writer", ["open(computed_path, 'w').write(text)", "p = Path(computed_path)\n    p.write_text(text)"])
def test_written_helper_edit_does_not_require_a_parameter_path(writer):
    command = "python3 - <<'PY'\ndef emit():\n    text = 'old'.replace('old', 'new\\nline')\n    " + writer + "\nemit()\nPY"
    assert bash_churn(command) == (2, 1)
    assert not python_write_paths(command)


@pytest.mark.parametrize("body,argument,expected,paths", [
    ("path = Path('/dev/null')\n    open(path, 'w').write(text)", "computed", (0, 0), []),
    ("path = '/dev/null'\n    open(Path(path), 'w').write(text)", "'one\\ntwo'", (0, 0), []),
    ("open(Path(path), 'w').write(text)", "'one\\ntwo'", (2, 0), ['out.txt']),
    ("handle = open(path, 'w')\n    alias = handle\n    path = '/dev/null'\n    alias.write(text)", "'one\\ntwo'", (2, 0), ['out.txt']),
    ("first = second = open(path, 'w')\n    path = '/dev/null'\n    second.write(text)", "'one\\ntwo'", (2, 0), ['out.txt']),
    ("open(path, 'w').write('one')\n    path = 'second.txt'\n    open(path, 'w').write('two')", "computed", (2, 0), ['out.txt', 'second.txt']),
    ("path = '/dev/null'\n    open(path, 'w').write(text)", "'one\\ntwo'", (0, 0), []),
    ("path = '/dev/null'\n    open(path, 'w').write(text)", 'computed', (0, 0), []),
    ("path = 'actual.txt'\n    open(path, 'w').write(text)", "'one\\ntwo'", (2, 0), ['actual.txt']),
    ("path = computed_path\n    open(path, 'w').write(text)", 'computed', (1, 0), []),
    ("alias = path\n    path = '/dev/null'\n    open(alias, 'w').write(text)", "'one\\ntwo'", (2, 0), ['out.txt']),
    ("alias = path\n    alias = '/dev/null'\n    open(alias, 'w').write(text)", 'computed', (0, 0), []),
    ("open(path, 'w').write(text)\n    path = '/dev/null'", "'one\\ntwo'", (2, 0), ['out.txt']),
    ("open(path, 'w').write('one')\n    path = '/dev/null'\n    open(path, 'w').write(text)", 'computed', (1, 0), ['out.txt']),
    ("path = '/dev/null'\n    open(path, 'w').write(text)\n    path = 'actual.txt'\n    open(path, 'w').write('one')", 'computed', (1, 0), ['actual.txt']),
    ("handle = open(path, 'w')\n    path = '/dev/null'\n    handle.write(text)", "'one\\ntwo'", (2, 0), ['out.txt']),
    ("path = '/dev/null'\n    with open(path, 'w') as handle:\n        handle.write(text)", 'computed', (0, 0), []),
    ("p = Path(path)\n    path = '/dev/null'\n    p.write_text(text)", "'one\\ntwo'", (2, 0), ['out.txt']),
    ("path = '/dev/null'\n    p = Path(path)\n    p.write_text(text)", 'computed', (0, 0), []),
])
def test_helper_destination_binding_at_each_write(body, argument, expected, paths):
    command = "python3 - <<'PY'\ndef emit(path, text):\n    " + body + "\nemit('out.txt', " + argument + ")\nPY"
    assert bash_churn(command) == expected
    assert python_write_paths(command) == paths


@pytest.mark.parametrize("command,expected", [
    ('cat /dev/null > out.txt', (0, 0)),
    ('cat /dev/null /dev/null > out.txt', (0, 0)),
    ('cat < /dev/null > out.txt', (0, 0)),
    ('cat source.txt > out.txt', (1, 0)),
    ('cat /dev/null source.txt > out.txt', (1, 0)),
    ('cat /dev/null - > out.txt', (1, 0)),
    ('cat < source.txt > out.txt', (1, 0)),
    ('cat "$SOURCE" > out.txt', (1, 0)),
])
def test_cat_empty_source_and_unknown_controls(command, expected):
    assert bash_churn(command) == expected


@pytest.mark.parametrize("old,new,expected", [
    # Anchor re-emitted after an insertion: only the inserted lines count.
    ("def f():", "x = 1\n\ndef f():", (2, 0)),
    ("a\n", "a\nb\n", (1, 0)),
    # Identical payloads changed nothing.
    ("a\nb\n", "a\nb\n", (0, 0)),
    # Whole-line deletion.
    ("a\nb\n", "", (0, 2)),
    # Mid-line: the text after the match is part of the same line, so
    # splitting it changes that line even though "foo(" survives.
    ("foo(", "foo(\n  bar,", (2, 1)),
    ("(a)", "(b)", (1, 1)),
    # No old text (Edit creating a file): all of new is added.
    ("", "x\ny\n", (2, 0)),
])
def test_replace_churn_counts_like_git(old, new, expected):
    """One occurrence of old → new, diffed as the file would show it."""
    assert replace_churn(old, new) == expected


def test_python_replace_anchor_is_not_counted_as_churn():
    """Inserting before an anchor that is re-emitted is pure addition."""
    body = ("s = s.replace('def f():', 'x = 1\\n\\ndef f():')\n"
            "open('f.py', 'w').write(s)")
    assert bash_churn("python3 - <<'PY'\n" + body + "\nPY") == (2, 0)
