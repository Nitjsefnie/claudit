from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci import comment_share as counter

ROOT = Path(__file__).resolve().parents[1]
ESPREE = Path(os.environ.get("CLAUDIT_ESPREE_DIR") or ROOT / "node_modules")

CASES = [
    ('py_hash', 'py', 'x = "# code" # hi\n', (3, 12)),
    ('py_bare', 'py', '"mod"\nclass C:\n "cls"\n def f(self):\n  "fun"\n  "bare"\n  return 1\n', (21, 46)),
    ('py_nonstring', 'py', 'f"# {1}"\nb"#"\nx = "#"\n# yes\n', (4, 20)),
    ('py_utf8', 'py', 'é = 1; "dé" # ø\n', (8, 13)),
    ('py_line_separator', 'py', 'x = "\u2028"; "doc" # é\n"next"\n', (14, 19)),
    ('py_concat', 'py', '("a"\n # x\n "b")\n', (8, 10)),
    ('yaml_literal', 'yaml', 'a: |\n  text # data\n', (0, 12)),
    ('yaml_folded', 'yaml', 'a: >\n  text # data\n', (0, 12)),
    ('yaml_block_reset', 'yaml', 'a: |\n  "text\nb: 1 # real\n', (5, 16)),
    ('yaml_run', 'yaml', 'run: |\n  # shell\n  cat <<EOF\n  # body\n  EOF\n  echo "multi\n    # string"\nb: 1 # real\n', (11, 53)),
    ('yaml_spaced_quote', 'yaml', "a: this is 'plain # real\n", (5, 19)),
    ('shell_command_scope', 'sh', 'echo "$(echo x # real\n)"\n', (5, 19)),
    ('shell_backtick_scope', 'sh', 'echo "`echo x # real\n`"\n', (5, 18)),
    ('shell_parameter_command', 'sh', 'echo ${x:-$(echo x # literal\n)} # yes\n', (4, 30)),
    ('shell_parameter_brace', 'sh', 'echo ${x:-foo{} # yes\n', (4, 18)),
    ('shell_parameter_scope', 'sh', 'echo ${x:- # literal} # yes\n', (4, 22)),
    ('heredoc_continued', 'sh', 'cat <<EOF \\\n  <<BAR\n# first\nEOF\n# second\nBAR\n# yes\n', (4, 37)),
    ('py_prefixed', 'py', 'r"rawx"\nR"RAW"\nu"uni"\nU"UNI"\n', (25, 25)),
    ('js_hashbang', 'js', '#!/usr/bin/env node\nconst x=1; // yes\n', (23, 32)),
    ('jsx_text', 'jsx', 'const x=<div>hello // content</div>; // yes\n', (5, 38)),
    ('yaml_unicode_break', 'yaml', 'a: |\u2028  # data\n', (0, 8)),
    ('yaml_cr_comments', 'yaml', '# first\ra: 1 # yes\r', (10, 13)),
    ('yaml_key_colon', 'yaml', '"a:b": |\n  # data\n', (0, 12)),
    ('yaml_key_hash', 'yaml', "'a#b': >-\n  # data\n", (0, 13)),
    ('yaml_escaped_run', 'yaml', '"ru\\u006e": |\n  # yes\n', (0, 16)),
    ('yaml_alias_key', 'yaml', '&k run: x\n*k: |\n  # yes\n', (0, 15)),
    ('yaml_anchor', 'yaml', 'a: &v "x # data" # yes\n', (4, 16)),
    ('yaml_tag', 'yaml', 'a: !!str "x # data" # yes\n', (4, 19)),
    ('yaml_custom_tag', 'yaml', 'a: !tag "x # data" # yes\n', (4, 18)),
    ('yaml_verbatim_tag', 'yaml', 'a: !<tag:example.org,2026:x> "x # data" # yes\n', (4, 39)),
    ('yaml_flow_properties', 'yaml', '{a: &v "x # data", b: *v} # yes\n', (4, 23)),
    ('yaml_block_properties', 'yaml', 'a: &v !!str |\n  # data\n', (0, 15)),
    ('yaml_run_properties', 'yaml', 'run: &v !!str |\n  cat <<EOF\n  # body\n  EOF\n  # yes\n', (0, 32)),
    ('yaml_plain_continuation', 'yaml', "a: text\n  'plain # real\nb: 1 # yes\n", (9, 24)),
    ('shell_quoted_shift', 'sh', 'echo "$((1 << true))"\n# real\ntrue\n# yes\n', (9, 31)),
    ('shell_arithmetic', 'sh', 'echo "$((1 << 2))"\n# yes\n', (4, 19)),
    ('shell_arithmetic_statement', 'sh', '((1 << 2))\n# yes\n', (4, 12)),
    ('shell_nested_arithmetic', 'sh', 'echo "$(( (1 + 1) << 2))"\n# yes\n', (4, 23)),
    ('yaml_run_arithmetic', 'yaml', 'run: |\n  echo "$((1 << 2))"\n  # yes\n', (4, 24)),
    ('shell_ansi_quote', 'sh', "echo $'it\\'s # data' # yes\n", (4, 21)),
    ('yaml_sequence_block', 'yaml', 'items:\n  - |\n    # data\n  - x # yes\n', (4, 19)),
    ('yaml_block_overlap', 'yaml', 'run:\n  - |\n    # a\n  - run: |\n      # bbbbbbbbbbbbbbbbbbbb\n', (21, 35)),
    ('shell_literal_keywords', 'sh', 'echo if case # yes\n', (4, 14)),
    ('shell_quoted_ansi_word', 'sh', 'cat <<"$\'EOF\'"\n# data\nEOF\n# more\n$\'EOF\'\n# yes\n', (4, 36)),
    ('empty', 'py', ' \t\n', (0, 0)),
    ('js_literals', 'js', 'const a = "//"; const b = `// ${1}`; const c = /\\/\\//; // a\n/* b c */\n', (9, 51)),
    ('mjs', 'mjs', 'export const x = 1; // hi\n', (4, 19)),
    ('cjs', 'cjs', 'module.exports = 1; // hi\n', (4, 21)),
    ('jsx', 'jsx', 'const el = <div title="//">{/* hi */}text</div>; // é\n', (10, 46)),
    ('yaml', 'yaml', 'a: "prefix # x"\nb: \'prefix # y\'\nc: a#b # yes\n# ok\n', (7, 36)),
    ('yaml_quotes', 'yml', 'a: "escaped \\" # still quoted" # é\nb: \'it\'\'s # quoted\' # z\n', (5, 46)),
    ('yaml_plain_quote', 'yaml', "name: it's plain # yes\n", (4, 18)),
    ('shell', 'sh', 'echo "# inside" \' # quote\' $# ${#x} a#b \\# # yes\n# ok\n', (7, 40)),
    ('heredoc', 'sh', 'cat <<\'EOF\' <<-"END" # hi\n# body\nEOF\n\t# body\n\tEND\n# ok\n', (6, 40)),
    ('shell_multiline', 'sh', 'echo "first\n# inside" # out\n# end\n', (8, 26)),
]


@pytest.mark.parametrize("case,suffix,source,want", CASES, ids=[c[0] for c in CASES])
def test_counts(case: str, suffix: str, source: str, want: tuple[int, int],
                tmp_path: Path) -> None:
    if suffix in ("js", "jsx", "mjs", "cjs") and not (ESPREE / "espree" / "package.json").is_file():
        pytest.skip("espree is not installed")
    path = tmp_path / f"sample.{suffix}"
    path.write_text(source, encoding="utf-8")
    language = counter.language_for(path)
    assert language is not None
    assert counter.measure_files(tmp_path, {path.name: language}) == {path.name: want}


def test_tracked_universe_and_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    sources = {"a.py": "x = 1 # hi\n", "run": "#!/usr/bin/env -S bash -eu\n# hi\n",
               "a.yaml": "# hi\n", "no": "#!/usr/bin/env python3\n",
               "no.txt": "#!/bin/sh\n"}
    for name, source in sources.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", *sources], check=True)
    (tmp_path / "untracked.py").write_text("# hi\n", encoding="utf-8")
    assert counter.tracked_sources(tmp_path) == {"a.py": "python", "a.yaml": "yaml", "run": "shell"}
    assert counter.main(["--report", "--root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "a.py\t3\t6\t50.000000%" in output
    assert "overall\t3\t32\t35\t91.428571%" in output
    assert "python\t1\t3\t6\t50.000000%" in output


REFUSED_SHELL = [
    'echo "$(( $(cat <<EOF\n"2"\nEOF\n) + 1 ))"\n',
    'echo "$(( $(cat <<\'EOF\'\n2\nEOF\n) + 1 ))"\n',
    'cat <<"E OF"\n# data\nE OF\n',
    'echo "$(case x in\nx) echo x ;; # real\nesac\n)" # yes\n',
    'echo "$(! case x in\nx) echo x ;; # real\nesac\n)" # yes\n',
    'echo "$(case x in\n*/esac) echo x ;; # real\nesac\n)" # yes\n',
    "cat <<\\$'EOF'\n# data\n$EOF\n# yes\n",
    "cat <<$'EOF'\n# data\nEOF\n# yes\n",
    'echo "$(( \'x ))"\n',
    'echo "$(( "x ))"\n',
    '((1 + "2"))\n',
    "cat <<$'EOF'\n# data\nEOF\n",
    "cat <<$'it\\'s'\n# data\nit\\s\n# more\nit's\n# yes\n",
    "cat <<$'\\303\\251'\n# data\nÃ©\n# more\né\n# yes\n",
    "cat <<$'\\xC3\\xA9'\n# data\nÃ©\n# more\né\n# yes\n",
    "cat <<E'OF'\n# data\nEOF\n",
    'echo "$(case x in\nx) echo x ;;\nesac\n)"\n',
    'echo "$(time case x in\nx) echo x ;;\nesac\n)"\n',
    'echo "`case x in\nx) echo x ;;\nesac\n`"\n',
]
REFUSED_SOURCES = [("refused.sh", source) for source in REFUSED_SHELL] + [
    ("refused.yaml", "run: |\n" + "".join("  " + line for line in source.splitlines(keepends=True)))
    for source in REFUSED_SHELL if '$((' in source or source.startswith('((')
]


OPEN_SCOPES = [
    'echo "$(\n',
    'echo ${\n',
    'echo ${x:-$(}\n',
    'echo ${x:-$((}\n',
    'echo ${x:-`}\n',
    'echo "`\n',
    'echo "$((\n',
    'echo "x\n',
    "echo 'x\n",
    "echo $'it\\'s\n",
    '((1 + 2)\n',
    '(echo x\n',
    'case x in @(x esac\n',
]
OPEN_SOURCES = [("open.sh", source) for source in OPEN_SCOPES] + [
    ("open.yaml", "run: |\n" + "".join("  " + line for line in source.splitlines(keepends=True))
     + "next: 1 # real\n") for source in OPEN_SCOPES
]


@pytest.mark.parametrize("name,source", [
    ("bad.py", "def (\n"), ("bad.py", "x = '\n"),
    ("bad.js", "const = 1;"), ("bad.jsx", "<div>"),
    ("bad.sh", "cat <<EOF\n# body\n"),
] + OPEN_SOURCES + REFUSED_SOURCES)
def test_unmeasurable_is_an_error(name: str, source: str, tmp_path: Path) -> None:
    if name.endswith((".js", ".jsx")) and not (ESPREE / "espree" / "package.json").is_file():
        pytest.skip("espree is not installed")
    (tmp_path / name).write_text(source, encoding="utf-8")
    language = counter.language_for(tmp_path / name)
    assert language is not None
    with pytest.raises(counter.MeasurementError, match=name):
        counter.measure_files(tmp_path, {name: language})


@pytest.mark.parametrize("dependency", ["node", "espree"])
def test_missing_dependency_fails_cli(dependency: str, tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.js").write_text("// hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.js"], check=True)
    env = dict(os.environ)
    if dependency == "node":
        git = shutil.which("git")
        assert git
        (tmp_path / "git").symlink_to(git)
    env["PATH" if dependency == "node" else "CLAUDIT_ESPREE_DIR"] = str(tmp_path)
    result = subprocess.run([sys.executable, str(ROOT / "scripts/ci/comment_share.py"),
                             "--report", "--root", str(tmp_path)], env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert dependency in result.stderr.lower()
    assert not result.stdout


@pytest.mark.parametrize("name,source", OPEN_SOURCES + REFUSED_SOURCES)
def test_unfinished_scopes_cli(name: str, source: str, tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / name).write_text(source, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", name], check=True)
    result = subprocess.run([sys.executable, str(ROOT / "scripts/ci/comment_share.py"),
                             "--report", "--root", str(tmp_path)],
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert name in result.stderr
    assert not result.stdout
