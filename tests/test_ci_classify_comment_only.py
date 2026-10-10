"""The ci-gate comment-only class (issue #844).

A change whose changed paths are all source files whose comment-stripped
token streams are identical at base and head cannot move behaviour, so
the classifier narrows it: the behaviour legs run, the ones a comment
cannot affect skip. These tests drive `comment_only` and `classify`
through an injected `run` seam (the same seam the classifier uses in
production), pinning each over-run direction — a non-source path, an
oversized set, an unreadable blob, a byte-equal pair, a tokenization
failure — and the node helper's own contract against a fake espree.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    """Import scripts/ci/<name>.py by path (scripts/ci is not a package)."""
    path = REPO_ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


classify = _load("classify_changes")

REPO = "o/r"
BASE = "b" * 40
HEAD = "a" * 40
EVENT = {"name": "push", "repository": REPO, "before": BASE, "sha": HEAD}


def _run(contents, paths=(), calls=None, tokens=None, fail=None):
    """A stubbed `run` over the reads comment_only makes.

    `contents` maps (path, ref) -> blob text; `paths` is the changed set
    the compare read answers; `tokens` is the JSON a node call returns;
    `fail` is a url substring whose read raises.
    """
    def run(argv):
        if calls is not None:
            calls.append(argv)
        url = next((arg for arg in argv if arg.startswith("repos/")), "")
        if url:
            if fail and fail in url:
                raise OSError("gh failed")
            if "/runs?" in url:
                return f"{BASE} completed success 7\n"
            if "/jobs?" in url:
                return "tests success\n"
            if "/compare/" in url:
                return "".join(f"{path}\n" for path in paths)
            if "/contents/" in url:
                path = url.split("/contents/", 1)[1].split("?", 1)[0]
                ref = url.split("ref=", 1)[1]
                if (path, ref) not in contents:
                    raise OSError("no blob")
                return contents[(path, ref)]
        if argv and argv[0] == "node":
            if fail == "node":
                raise OSError("node failed")
            return tokens if tokens is not None else "[]"
        raise OSError(f"unexpected read: {argv}")
    return run


def _js_run(contents, base_tokens, head_tokens):
    """A run whose first node call is the base blob's tokens and second
    the head's, so a JS change can be made equal or unequal on demand."""
    node_calls = []

    def run(argv):
        url = next((arg for arg in argv if arg.startswith("repos/")), "")
        if url:
            if "/runs?" in url:
                return f"{BASE} completed success 7\n"
            if "/jobs?" in url:
                return "tests success\n"
            path = url.split("/contents/", 1)[1].split("?", 1)[0]
            ref = url.split("ref=", 1)[1]
            return contents[(path, ref)]
        node_calls.append(argv)
        return base_tokens if len(node_calls) == 1 else head_tokens
    return run


# --- comment_only: the over-run directions ----------------------------------


def test_py_comment_only_is_true():
    contents = {("m.py", BASE): "x = 1\n", ("m.py", HEAD): "x = 1  # c\n"}
    assert classify.comment_only(["m.py"], EVENT, _run(contents)) is True


def test_py_docstring_change_is_not_comment_only():
    # A docstring is a STRING token, kept in the stream, so it is a real
    # change however prose-shaped it looks.
    contents = {("m.py", BASE): 'def f():\n    """a"""\n',
                ("m.py", HEAD): 'def f():\n    """b"""\n'}
    assert classify.comment_only(["m.py"], EVENT, _run(contents)) is False


def test_py_real_code_change_is_not_comment_only():
    contents = {("m.py", BASE): "x = 1\n", ("m.py", HEAD): "x = 2\n"}
    assert classify.comment_only(["m.py"], EVENT, _run(contents)) is False


def test_a_non_source_path_over_runs_without_a_content_read():
    calls = []
    contents = {("m.py", BASE): "x = 1\n", ("m.py", HEAD): "x = 1  # c\n"}
    assert classify.comment_only(["m.py", "a.json"], EVENT,
                                 _run(contents, calls=calls)) is False
    assert not any("/contents/" in arg for call in calls for arg in call)


def test_more_than_the_cap_over_runs():
    paths = [f"m{i}.py" for i in range(classify.MAX_COMMENT_ONLY_FILES + 1)]
    assert classify.comment_only(paths, EVENT, _run({})) is False


def test_a_byte_equal_pair_is_not_comment_only():
    # A no-op or mode-only change: the blobs are identical, so there is
    # nothing to strip and it is not this class.
    contents = {("m.py", BASE): "x = 1\n", ("m.py", HEAD): "x = 1\n"}
    assert classify.comment_only(["m.py"], EVENT, _run(contents)) is False


def test_a_blob_read_failure_over_runs():
    contents = {("m.py", BASE): "x = 1\n"}  # the head blob is missing
    assert classify.comment_only(["m.py"], EVENT, _run(contents)) is False


def test_a_tokenization_failure_over_runs():
    contents = {("m.py", BASE): "x = 1\n", ("m.py", HEAD): "def (\n"}
    assert classify.comment_only(["m.py"], EVENT, _run(contents)) is False


def test_js_comment_only_and_real_change_via_canned_tokens():
    contents = {("m.js", BASE): "a\n", ("m.js", HEAD): "// c\na\n"}
    same = '[["Identifier", "a"]]'
    assert classify.comment_only(
        ["m.js"], EVENT, _js_run(contents, same, same)) is True
    assert classify.comment_only(
        ["m.js"], EVENT,
        _js_run(contents, same, '[["Identifier", "b"]]')) is False


# --- classify: the class through the whole decision --------------------------


def test_classify_reports_the_comment_only_class():
    contents = {("m.py", BASE): "x = 1\n", ("m.py", HEAD): "x = 1  # c\n"}
    docs, data, comment, reason = classify.classify(
        EVENT, _run(contents, paths=["m.py"]))
    assert (docs, data, comment) == (False, False, True)
    assert reason.startswith("comment-only change")


def test_write_outputs_emits_the_four_lines(tmp_path):
    out = tmp_path / "o.txt"
    classify.write_outputs(str(out), False, False, True,
                           "comment-only change: 2 source files")
    assert out.read_text(encoding="utf-8") == (
        "docs_only=false\n"
        "data_only=false\n"
        "comment_only=true\n"
        "reason=comment-only change: 2 source files\n"
    )


# --- the node helper, against a fake espree ---------------------------------


def test_the_node_helper_strips_comments_with_espree(tmp_path):
    # A fake espree that throws unless it is handed comment:false, so the
    # helper's own options are exercised, not merely its output shape.
    module_dir = tmp_path / "mods"
    module_dir.mkdir()
    (module_dir / "espree.js").write_text(
        "module.exports = { tokenize(src, opts) {\n"
        "  if (opts.comment !== false) { throw new Error('comment not false'); }\n"
        "  return [{type: 'Identifier', value: 'x'},\n"
        "          {type: 'Punctuator', value: ';'}];\n"
        "} };\n", encoding="utf-8")
    source = tmp_path / "s.js"
    source.write_text("x;  // c\n", encoding="utf-8")
    helper = REPO_ROOT / "scripts" / "ci" / "js_comment_tokens.mjs"
    completed = subprocess.run(
        ["node", str(helper), str(module_dir), str(source), "plain"],
        capture_output=True, text=True, check=True, timeout=60)
    assert json.loads(completed.stdout) == [["Identifier", "x"],
                                            ["Punctuator", ";"]]
