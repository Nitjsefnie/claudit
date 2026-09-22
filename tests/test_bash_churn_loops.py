"""Heredoc churn inside shell loops.

A heredoc body inside `for x in a b c; do … done` is written once per
iteration, and when the word list is LITERAL the iteration count is in
the command text — no execution needed. Counting the body once was an
undercount by exactly that factor (measured on a real session: a
two-iteration loop writing a 26-line brief recorded 26 lines, not 52).

The count is taken only when it is certain. A word list containing a
runtime expansion (`$x`, `$(…)`, an unquoted glob, `"$@"`), a
`while`/`until` loop, or syntax the tokenizer does not support counts
as ONE iteration — the floor this estimator already keeps elsewhere.
"""
import pytest

from backend.bash_churn import bash_churn

BODY = "l1\nl2\nl3"


def _loop(header: str, body: str = BODY) -> str:
    return f"{header}; do\ncat > out_$f.txt <<EOF\n{body}\nEOF\ndone"


@pytest.mark.parametrize("header,expected", [
    ("for f in a b", (6, 0)),
    ("for f in a b c d", (12, 0)),
    # Quoted words are one word each, whatever they contain.
    ('for f in "a b" \'c d\'', (6, 0)),
    ('for f in "rollout-*.jsonl" x', (6, 0)),
    # An empty literal list never runs its body.
    ("for f in", (0, 0)),
])
def test_literal_word_list_multiplies_the_heredoc(header, expected):
    assert bash_churn(_loop(header)) == expected


@pytest.mark.parametrize("header", [
    "for f in $LIST",
    'for f in "$@"',
    "for f in *.py",
    "for f in a $(ls)",
    "for f",
    "while read f",
    "until false",
])
def test_an_unknowable_count_stays_one_iteration(header):
    assert bash_churn(_loop(header)) == (3, 0)


def test_nested_literal_loops_multiply():
    cmd = ("for a in 1 2; do\nfor b in x y z; do\n"
           "cat > f_$a$b <<EOF\nl1\nEOF\ndone\ndone")
    assert bash_churn(cmd) == (6, 0)


def test_only_the_heredoc_inside_the_loop_repeats():
    cmd = ("cat > before <<EOF\nb1\nEOF\n"
           "for f in a b c; do\ncat > $f <<EOF\nin1\nin2\nEOF\ndone\n"
           "cat > after <<EOF\na1\nEOF")
    assert bash_churn(cmd) == (1 + 3 * 2 + 1, 0)


def test_loop_on_one_line_with_the_opener():
    cmd = "for f in a b; do cat > $f <<EOF\nl1\nl2\nEOF\ndone"
    assert bash_churn(cmd) == (4, 0)


def test_a_do_that_is_an_argument_opens_no_loop():
    """`echo do` is a word, not the loop keyword."""
    cmd = "echo for x in a b do; cat > f <<EOF\nl1\nEOF"
    assert bash_churn(cmd) == (1, 0)


def test_the_session_loop_that_found_this():
    """Shape of the dispatch-brief loop: `${pair%%:*}` targets, a
    quoted word list whose words contain `*` and `/`, and a body that
    expands `$repo`. Two iterations, so twice the body."""
    body = "\n".join(f"line {i} $repo" for i in range(26))
    cmd = ("S=/tmp/s; mkdir -p $S/a $S/b\n"
           'for pair in "codex:codexmeter:rollout-*.jsonl (x/y)" "kimi:kimimeter:K"; do\n'
           "key=${pair%%:*}; rest=${pair#*:}; repo=${rest%%:*}\n"
           f"cat > $S/merge-$key/brief.md <<EOF\n{body}\nEOF\n"
           "done; ls $S/merge-*/")
    assert bash_churn(cmd) == (52, 0)
