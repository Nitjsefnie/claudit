"""Tests moved from test_bash_churn.py to keep test modules under 700 lines."""
from __future__ import annotations

import pytest

from backend.bash_churn import replace_churn

from tests.test_bash_churn import (
    bash_churn,
    python_write_paths,
)


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
