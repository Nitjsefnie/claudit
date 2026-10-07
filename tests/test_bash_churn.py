"""bash_churn.py — line churn recovered from Bash command text.

Most editing in a bypass-permissions session never touches Edit/Write:
it goes through a heredoc, an inline patch, or a python one-liner.
Payloads come from command text without execution. Recognized writes
with unknown addition sizes receive one estimated added line per call.
"""
import warnings

import pytest

from backend.bash_churn import bash_churn, python_write_paths
from backend.bash_churn_errors import _verbatim_targets, churn_survives_error


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
    (r"printf 'a\n' >| probe.txt", (1, 0)),
    (r"printf 'a\n' &> probe.txt", (1, 0)),
    (r"printf 'a\n' &>> probe.txt", (1, 0)),
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


@pytest.mark.parametrize("text", ["No space left on device", "Read-only file system"])
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


@pytest.mark.parametrize("redirect", [">", ">|", "&>", "&>>", ">&"])
def test_heredoc_cat_books_churn_through_every_stream_spelling(redirect):
    """Bash writes the heredoc body into the target whatever the
    redirect spelling: `>|` clobbers, `&>`/`&>>` and `>& file` take
    stdout+stderr, and each books the body like plain `>` (#803, #802)."""
    command = f"cat <<EOF {redirect} f.txt\nline1\nline2\nline3\nEOF\n"
    assert bash_churn(command) == (3, 0)


@pytest.mark.parametrize("redirect", [">&2", "2>&1", ">&-"])
def test_heredoc_cat_through_an_fd_dup_books_no_churn(redirect):
    """`>& digits` duplicates and `>&-` closes: the body goes to a
    stream, never a file, so no churn is booked (#803)."""
    command = f"cat <<EOF {redirect}\nline1\nline2\nline3\nEOF\n"
    assert bash_churn(command) == (0, 0)


def test_stream_redirect_into_a_pipe_target_counts_the_churn_line():
    """`2>| g.txt` clobbers g.txt exactly like `2>`: SV-BASH-CHURN
    counts one added line per call for a recognized write of unknown
    addition size, whatever the spelling (#805)."""
    assert bash_churn("grep x f.py 2>| g.txt") == (1, 0)


def test_echo_into_fd_dup_filename_books_its_payload():
    """`>& file` is a real stdout sink, so echo's payload books like it
    does behind `&>` (#802)."""
    assert bash_churn("echo hi >& both.txt") == (1, 0)


def test_fd_dup_target_is_not_an_error_line_path():
    """A bare digit `>&` target is filtered as a descriptor, so an error
    line naming a digit-leading path (`2.py`) cannot match it and zero
    counted tee churn; the real tee target beside it keeps the write
    alive: the filter limb in bash_churn_errors is load-bearing (#802)."""
    command = "cat <<E | tee >&2 real.txt\nbody\nE\npython3 2.py"
    text = "python3: cannot open file '2.py': No such file or directory"
    assert churn_survives_error(command, text) is True


def test_tee_into_stderr_alone_has_no_survivable_write():
    """With the operator token filtered (#820), `tee >&2` books no file
    target at all: the counted body churn has no verbatim write behind
    it, so an errored later stage zeroes it."""
    command = "cat <<E | tee >&2\nbody\nE\npython3 2.py"
    text = "python3: cannot open file '2.py': No such file or directory"
    assert churn_survives_error(command, text) is False


@pytest.mark.parametrize("redirect", ["1>", "1>>", "1>|", "1>&"])
def test_heredoc_cat_books_churn_through_fd_one_stdout_spellings(redirect):
    """fd 1 IS stdout: bash lands the heredoc body in the target through
    the fd-spelled stdout forms exactly like plain `>` (#819)."""
    command = f"cat <<EOF {redirect} f.txt\nline1\nline2\nline3\nEOF\n"
    assert bash_churn(command) == (3, 0)


def test_heredoc_cat_through_an_fd_dup_books_no_churn_case():
    """`1>&2` duplicates stdout into stderr: no file at all, no churn
    (#819)."""
    command = "cat <<EOF 1>&2\nline1\nline2\nline3\nEOF\n"
    assert bash_churn(command) == (0, 0)


def test_heredoc_cat_with_a_higher_fd_redirect_keeps_the_sink_line():
    """`21>` points another descriptor at f.txt — the body lands on
    stdout (no heredoc churn), but bash creates f.txt, and the redirect
    itself is a recognized write of unknown addition size: the one
    unknown-size line, `2>`'s parity (#805, #819)."""
    command = "cat <<EOF 21> f.txt\nline1\nline2\nline3\nEOF\n"
    assert bash_churn(command) == (1, 0)


@pytest.mark.parametrize("context,expected", [
    ("cat <<E | tee >&2", []),
    ("tee <<E >&2", []),
    ("cat <<E | tee f.txt >&2", ["f.txt"]),
    ("cat <<E | tee -a log.txt", ["log.txt"]),
])
def test_tee_arm_books_no_operator_tokens(context, expected):
    """The tee arm reads raw shlex tokens, so a redirect operator rides
    in looking like a path: `>&2` is not a file, and the operator shape
    is filtered while real targets book (#820)."""
    assert _verbatim_targets(context) == expected


def test_word_adjacent_digit_is_a_word_not_an_fd_prefix():
    """`f1>` is the word `f1` plus a plain stdout redirect in bash — the
    digit only reads as an fd when it STARTS the token — so the heredoc
    body lands nowhere the fd-1 branch may claim and master's
    unknown-size line stands (#819)."""
    command = "cat <<EOF f1> out.txt\nl1\nl2\nl3\nEOF\n"
    assert bash_churn(command) == (1, 0)


@pytest.mark.parametrize("fd", ["11>", "10>", "100>"])
def test_heredoc_sink_line_through_multi_digit_fds(fd):
    """An fd of two or more digits still opens the target: bash creates
    f.txt before the command runs, so the redirect is a recognized write
    of unknown addition size — the same one line a single-digit fd
    counts (#854)."""
    command = f"cat <<EOF {fd} f.txt\nline1\nline2\nline3\nEOF\n"
    assert bash_churn(command) == (1, 0)


@pytest.mark.parametrize("context,expected", [
    ("cat <<E | tee <f.txt", []),
    ("cat <<E | tee <f.txt out.txt", ["out.txt"]),
])
def test_tee_arm_books_no_stdin_redirect_target(context, expected):
    """A `<` token is tee's INPUT redirect, never a file the tee writes:
    bash opens f.txt for reading and the body lands on stdout, so the
    stdin-redirect shape filters with the other operators while real
    targets book (#855)."""
    assert _verbatim_targets(context) == expected
