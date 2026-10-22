"""The ci-gate comment-only class.

A change whose changed paths are all source files whose comment-stripped
token streams are identical at base and head cannot move behaviour, so
the classifier narrows it: the behaviour legs run, the ones a comment
cannot affect skip. These tests drive `comment_only` and `classify`
through an injected `run` seam (the same seam the classifier uses in
production), pinning each over-run direction — a non-source path, an
oversized set, an unreadable blob, a byte-equal pair, a tokenization
failure — the aggregate fold's negative space, and the node helper's own
contract against a fake espree and the real one.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml
from tests.node_run import run_node

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_GATE = REPO_ROOT / ".github" / "workflows" / "ci-gate.yml"
TESTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"


def _load(name):
    path = REPO_ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


classify = _load("classify_changes")
aggregate = _load("aggregate_gate")

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


# A pair whose only difference is a comment BELOW line 2 — the magic-comment
# window the rule keeps — so it is genuinely comment-only.
BELOW_WINDOW_BASE = "a = 1\nb = 2\nc = 3\n"
BELOW_WINDOW_HEAD = "a = 1\nb = 2\nc = 3  # c\n"


def test_py_comment_only_is_true():
    contents = {("m.py", BASE): BELOW_WINDOW_BASE,
                ("m.py", HEAD): BELOW_WINDOW_HEAD}
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


def test_a_python_magic_comment_is_not_comment_only():
    # A PEP 263 coding declaration (and a shebang) is a COMMENT token the
    # interpreter still reads: flipping utf-8 -> latin-1 decodes the same
    # bytes to "naïve" (5 chars) vs "naÃ¯ve" (6), a real behaviour change.
    # Both sit on line 1, which the rule keeps unconditionally.
    body = 's = "naïve"\nprint(len(s), s)\n'
    assert classify.comment_only(["m.py"], EVENT, _run({
        ("m.py", BASE): "# -*- coding: utf-8 -*-\n" + body,
        ("m.py", HEAD): "# -*- coding: latin-1 -*-\n" + body})) is False
    assert classify.comment_only(["m.py"], EVENT, _run({
        ("m.py", BASE): "#!/usr/bin/env python3\nx = 1\n",
        ("m.py", HEAD): "#!/usr/bin/python3\nx = 1\n"})) is False


def test_a_comment_change_after_line_two_still_narrows():
    # The control: the rule keeps only lines 1-2, so a comment edit below
    # them is still comment-only.
    contents = {("m.py", BASE): "x = 1\ny = 2\n# a\nz = 3\n",
                ("m.py", HEAD): "x = 1\ny = 2\n# b\nz = 3\n"}
    assert classify.comment_only(["m.py"], EVENT, _run(contents)) is True


def test_the_early_comment_boundary_is_line_two():
    # Both directions at the boundary, with a magic SPELLING the old
    # hand-rolled predicate missed (`# vim: fileencoding=...`): a comment
    # change on line 2 does not narrow, the same change on line 3 does.
    def pair(line):
        filler = ["x = 1"] * (line - 1)
        base = "\n".join(filler + ["# vim: fileencoding=utf-8", "y = 2"]) + "\n"
        head = "\n".join(filler + ["# vim: fileencoding=latin-1", "y = 2"]) + "\n"
        return {("m.py", BASE): base, ("m.py", HEAD): head}

    assert classify.comment_only(["m.py"], EVENT, _run(pair(2))) is False
    assert classify.comment_only(["m.py"], EVENT, _run(pair(3))) is True


def test_a_non_source_path_over_runs_without_a_content_read():
    calls = []
    contents = {("m.py", BASE): "x = 1\n", ("m.py", HEAD): "x = 1  # c\n"}
    assert classify.comment_only(["m.py", "a.json"], EVENT,
                                 _run(contents, calls=calls)) is False
    assert not any("/contents/" in arg for call in calls for arg in call)


def test_more_than_the_cap_over_runs():
    # Every path is seeded with byte-different, token-identical contents,
    # so the ONLY reason to refuse is MAX_COMMENT_ONLY_FILES — not a
    # missing blob. The cap-minus-one control proves the seed reaches the
    # tokenizer and would otherwise classify True.
    paths = [f"m{i}.py" for i in range(classify.MAX_COMMENT_ONLY_FILES + 1)]
    contents = {}
    for path in paths:
        contents[(path, BASE)] = BELOW_WINDOW_BASE
        contents[(path, HEAD)] = BELOW_WINDOW_HEAD
    assert classify.comment_only(paths, EVENT, _run(contents)) is False
    assert classify.comment_only(paths[:-1], EVENT, _run(contents)) is True


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
    same = '[["Identifier", "a", false]]'
    assert classify.comment_only(
        ["m.js"], EVENT, _js_run(contents, same, same)) is True
    assert classify.comment_only(
        ["m.js"], EVENT,
        _js_run(contents, same, '[["Identifier", "b", false]]')) is False


def test_classify_reports_the_comment_only_class():
    contents = {("m.py", BASE): BELOW_WINDOW_BASE,
                ("m.py", HEAD): BELOW_WINDOW_HEAD}
    docs, data, comment, _thresholds, _setup, reason = classify.classify(
        EVENT, _run(contents, paths=["m.py"]))
    assert (docs, data, comment) == (False, False, True)
    assert reason.startswith("comment-only change")


def test_write_outputs_emits_all_classifications(tmp_path):
    out = tmp_path / "o.txt"
    classify.write_outputs(str(out), False, False, True, False, False,
                           "comment-only change: 2 source files")
    assert out.read_text(encoding="utf-8") == (
        "docs_only=false\n"
        "data_only=false\n"
        "comment_only=true\n"
        "thresholds_only=false\n"
        "setup_only=false\n"
        "reason=comment-only change: 2 source files\n"
    )


def test_refs_reads_a_pull_request_base_and_head():
    calls = []

    def run(argv):
        calls.append(argv)
        return f"{BASE} {HEAD}\n"

    event = {"name": "pull_request", "repository": REPO, "pull_request": "17"}
    assert classify._refs(event, REPO, run) == (BASE, HEAD)  # pylint: disable=protected-access
    assert "repos/o/r/pulls/17" in calls[0]


def test_refs_over_runs_on_an_unreadable_pull_request():
    def run(argv):
        raise OSError("gh failed")

    event = {"name": "pull_request", "repository": REPO, "pull_request": "17"}
    assert classify._refs(event, REPO, run) is None  # pylint: disable=protected-access
    # And the class over-runs with it: unreadable refs are not
    # comment-only, and no blob read is attempted.
    assert classify.comment_only(["m.py"], event, run) is False


def test_comment_only_on_a_pull_request():
    contents = {("m.py", BASE): BELOW_WINDOW_BASE,
                ("m.py", HEAD): BELOW_WINDOW_HEAD}

    def run(argv):
        url = next((arg for arg in argv if arg.startswith("repos/")), "")
        if "/pulls/" in url:
            return f"{BASE} {HEAD}\n"
        if "/contents/" in url:
            path = url.split("/contents/", 1)[1].split("?", 1)[0]
            ref = url.split("ref=", 1)[1]
            return contents[(path, ref)]
        raise AssertionError(url)

    event = {"name": "pull_request", "repository": REPO, "pull_request": "17"}
    assert classify.comment_only(["m.py"], event, run) is True


LEGS = ("classify", "tests", "test-data", "lint", "types", "eslint",
        "panel-layout", "smoke", "docker-smoke", "audit", "actionlint",
        "codeql", "setup-docs")


def _needs(comment_only: str | None = "true", **results):
    """A needs document shaped for the comment-only fold pins.

    A LOCAL helper so the suite-bench-listed test_ci_gate_modules.py does
    not grow; `comment_only` is the classifier output, `results` override
    per-leg results by leg id.
    """
    doc = {"classify": {"result": "success",
                        "outputs": {"docs_only": "false",
                                    "data_only": "false",
                                    "comment_only": comment_only}}}
    for leg in LEGS[1:]:
        doc[leg] = {"result": results.get(leg, "success")}
    return doc


def test_comment_only_fold_passes_when_only_the_skippable_legs_skip():
    doc = _needs()
    for leg in aggregate.COMMENT_ONLY_SKIPPED:
        doc[leg] = {"result": "skipped"}
    verdict, message = aggregate.decide(doc)
    assert verdict == aggregate.PASSED
    assert "comment-only" in message


def test_comment_only_fold_fails_when_a_behaviour_leg_skips():
    # The negative space: a skip outside COMMENT_ONLY_SKIPPED is a
    # fold/workflow disagreement and must go red, not pass.
    for leg in ("tests", "lint", "types", "eslint", "smoke", "docker-smoke",
                "audit", "actionlint", "codeql"):
        doc = _needs()
        for skipped in aggregate.COMMENT_ONLY_SKIPPED:
            doc[skipped] = {"result": "skipped"}
        doc[leg] = {"result": "skipped"}
        verdict, message = aggregate.decide(doc)
        assert verdict == aggregate.FAILED, leg
        assert f"{leg}=skipped" in message


def test_comment_only_output_reads_fail_closed():
    # Missing, empty, wrong-case and non-string outputs cannot turn a
    # skipped leg into a pass.
    for value in (None, "", "True", "1"):
        doc = _needs(comment_only=value)
        doc["test-data"] = {"result": "skipped"}
        verdict, message = aggregate.decide(doc)
        assert verdict == aggregate.FAILED, value
        assert "test-data=skipped" in message


def test_comment_only_output_absent_fails():
    doc = _needs()
    del doc["classify"]["outputs"]["comment_only"]
    doc["test-data"] = {"result": "skipped"}
    assert aggregate.decide(doc)[0] == aggregate.FAILED


def _ci_gate():
    return yaml.safe_load(CI_GATE.read_text(encoding="utf-8")) or {}


def _tests_workflow():
    return yaml.safe_load(TESTS_WORKFLOW.read_text(encoding="utf-8")) or {}


def test_the_portable_matrix_reads_the_comment_only_input():
    portable = _tests_workflow()["jobs"]["pytest-portable"]
    assert portable["if"] == ("inputs.comment_only != 'true' "
                              "&& inputs.thresholds_only != 'true'")


def test_comment_only_keeps_the_parallel_canary_gates():
    jobs = _tests_workflow()["jobs"]
    assert set(jobs) == {
        "pytest-shards", "coverage-combine", "js-coverage", "reparse-gate",
        "suite-bench", "pytest-thresholds", "baselines", "ratchet", "pytest-portable"}
    for name, job in jobs.items():
        if name != "pytest-portable":
            assert "comment_only" not in job.get("if", ""), name


def test_the_espree_dir_env_name_matches_the_workflow():
    # A wrong name silently disables the whole class with a green gate:
    # the classifier reads one env key and the workflow sets another.
    step = next(s for s in _ci_gate()["jobs"]["classify"]["steps"]
                if s.get("id") == "classify")
    assert classify.ESPREE_DIR_ENV == "CLAUDIT_ESPREE_DIR"
    assert classify.ESPREE_DIR_ENV in (step.get("env") or {})


def test_the_node_helper_strips_comments_and_marks_line_breaks(tmp_path):
    # A fake espree that throws unless handed comment:false and loc:true,
    # returning two tokens with loc, so the helper's options AND its
    # three-field output (the third is the line-break flag) are both
    # exercised: the first token is at the start, the second follows a
    # line break.
    module_dir = tmp_path / "mods"
    module_dir.mkdir()
    (module_dir / "espree.js").write_text(
        "module.exports = { tokenize(src, opts) {\n"
        "  if (opts.comment !== false) { throw new Error('comment not false'); }\n"
        "  if (opts.loc !== true) { throw new Error('loc not true'); }\n"
        "  return [\n"
        "    {type: 'Identifier', value: 'x', loc: {start: {line: 1}, end: {line: 1}}},\n"
        "    {type: 'Identifier', value: 'y', loc: {start: {line: 2}, end: {line: 2}}},\n"
        "  ];\n"
        "} };\n", encoding="utf-8")
    source = tmp_path / "s.js"
    source.write_text("x\ny\n", encoding="utf-8")
    helper = REPO_ROOT / "scripts" / "ci" / "js_comment_tokens.mjs"
    completed = run_node(
        ["node", str(helper), str(module_dir), str(source), "plain"],
        capture_output=True, text=True, check=True, timeout=60)
    assert json.loads(completed.stdout) == [
        ["Identifier", "x", False], ["Identifier", "y", True]]


def test_the_node_helper_distinguishes_an_asi_line_break(tmp_path):
    # The regression this field exists for. A line terminator is semantic
    # in JS (the ASI restricted productions), and espree's tokens erase
    # whitespace, so a two-field key reads the ASI pair below as EQUAL
    # while the two functions return undefined vs 42. The real espree is
    # only present where `npm ci` has run (the JS-coverage step), so this
    # skips without it.
    espree_dir = REPO_ROOT / "node_modules"
    if not (espree_dir / "espree" / "package.json").is_file():
        pytest.skip("espree is not installed at the repository root")
    helper = REPO_ROOT / "scripts" / "ci" / "js_comment_tokens.mjs"

    def keys(text):
        source = tmp_path / "s.js"
        source.write_text(text, encoding="utf-8")
        completed = run_node(
            ["node", str(helper), str(espree_dir), str(source), "plain"],
            capture_output=True, text=True, check=True, timeout=60)
        return json.loads(completed.stdout)

    asi = keys("export function f(x) {\n  return\n  x * 2;\n}\n")
    no_asi = keys("export function f(x) {\n  return x * 2;\n}\n")
    assert asi != no_asi
    # A comment-only edit leaves the keys equal.
    assert keys("const a = 1; // c\n") == keys("const a = 1;\n")
