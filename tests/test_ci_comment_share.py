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
    ('shell_parameter_scope', 'sh', 'echo ${x:- # literal} # yes\n', (4, 22)),
    ('heredoc_continued', 'sh', 'cat <<EOF \\\n  <<BAR\n# first\nEOF\n# second\nBAR\n# yes\n', (4, 37)),
    ('py_prefixed', 'py', 'r"rawx"\nR"RAW"\nu"uni"\nU"UNI"\n', (25, 25)),
    ('js_hashbang', 'js', '#!/usr/bin/env node\nconst x=1; // yes\n', (23, 32)),
    ('jsx_text', 'jsx', 'const x=<div>hello // content</div>; // yes\n', (5, 38)),
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


@pytest.mark.parametrize("name,source", [
    ("bad.py", "def (\n"), ("bad.py", "x = '\n"),
    ("bad.js", "const = 1;"), ("bad.jsx", "<div>"),
    ("bad.sh", "cat <<EOF\n# body\n"),
])
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
