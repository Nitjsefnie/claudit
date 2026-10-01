"""Unit tests for the ci-gate classifier and the aggregate fold.

The docs-only classification and the aggregate verdict are the two load-
bearing decisions of ci-gate.yml, so both live in tested modules here
rather than inline in the workflow. The tests pin the rule the issue
names: documentation-only runs narrow the expensive legs by
classification while every required check still reports, and the
aggregate folds every leg's result into one verdict where a leg skipped
BECAUSE docs-only passes and any other non-success fails.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    """Import scripts/ci/<name>.py by path.

    scripts/ci is not a package and deliberately has no __init__.py — it
    holds standalone CI entry points, not an importable library.
    """
    path = REPO_ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


classify = _load("classify_changes")
aggregate = _load("aggregate_gate")


# ---------------------------------------------------------------------------
# classify_changes: the pattern set
# ---------------------------------------------------------------------------


def test_pattern_set_is_the_re_homed_deny_list():
    # The exact set the gate workflows' paths-ignore deny-lists carried,
    # re-homed as classifier patterns. `.github/ci-thresholds.json` is
    # deliberately ABSENT: it is handled at ci-gate's push trigger, and a
    # change to it must run the gates rather than read as documentation.
    assert classify.DOC_PATTERNS == (
        "**/*.md", "PRESENTATION.txt", "examples/**", ".claude/**",
        "LICENSE", "NOTICE", ".gitignore",
    )


def test_doublestar_md_selects_readmes_at_every_depth():
    assert classify.matches("**/*.md", "README.md")
    assert classify.matches("**/*.md", "docs/guide.md")
    assert classify.matches("**/*.md", "a/b/c.md")


def test_doublestar_md_does_not_select_non_markdown():
    assert not classify.matches("**/*.md", "backend/app.py")
    assert not classify.matches("**/*.md", "PRESENTATION.txt")
    assert not classify.matches("**/*.md", "src/app.jsx")


def test_literal_patterns_are_rooted():
    assert classify.matches("LICENSE", "LICENSE")
    assert not classify.matches("LICENSE", "sub/LICENSE")
    assert not classify.matches("LICENSE", "LICENSE.txt")
    assert classify.matches("PRESENTATION.txt", "PRESENTATION.txt")
    assert not classify.matches("PRESENTATION.txt", "x/PRESENTATION.txt")
    assert classify.matches(".gitignore", ".gitignore")


def test_directory_doublestar_selects_only_inside_the_directory():
    assert classify.matches("examples/**", "examples/a.txt")
    assert classify.matches("examples/**", "examples/s/b.txt")
    assert not classify.matches("examples/**", "examplesx/a.txt")
    assert not classify.matches("examples/**", "src/examples/a.txt")
    assert classify.matches(".claude/**", ".claude/rules/x.md")


def test_unsupported_pattern_shapes_are_refused():
    for pattern in ("a*b/c", "prefix?.txt", "br[acket].md"):
        try:
            classify.matches(pattern, "anything")
        except ValueError:
            continue
        raise AssertionError(f"pattern not refused: {pattern!r}")


DOCS = [
    "README.md", "docs/guide.md", "PRESENTATION.txt", "examples/a.txt",
    "examples/s/b.txt", ".claude/rules/x.md", "LICENSE", "NOTICE",
    ".gitignore",
]
CODE = [
    "backend/app.py", "src/app.jsx", ".github/workflows/tests.yml",
    ".github/ci-thresholds.json", "VERSION", ".gitleaks.toml",
    ".github/dependabot.yml", "scripts/ci/classify_changes.py",
]


def test_is_documentation_partition():
    for path in DOCS:
        assert classify.is_documentation(path), path
    for path in CODE:
        assert not classify.is_documentation(path), path


def test_documentation_only_requires_a_nonempty_all_doc_set():
    assert not classify.documentation_only([])
    assert classify.documentation_only(["README.md"])
    assert classify.documentation_only(DOCS)
    for intruder in CODE:
        assert not classify.documentation_only(DOCS + [intruder]), intruder


# ---------------------------------------------------------------------------
# classify_changes: the bot-data class (issue #455)
# ---------------------------------------------------------------------------


def test_bot_data_pattern_set_is_the_refresh_bots_file():
    # The refresh bot's ONLY generated-data file, as a rooted filter
    # pattern. Deliberately NOT in DOC_PATTERNS: a rate-data change must
    # still get its boot-and-serve check (smoke) and lint, just not the
    # whole matrix — the refresh job ran the full suite against the new
    # rates BEFORE pushing, so re-running it here verifies nothing.
    assert classify.BOT_DATA_PATTERNS == ("src/pricing.json",)


def test_bot_data_pattern_is_rooted():
    assert classify.matches("src/pricing.json", "src/pricing.json")
    assert not classify.matches("src/pricing.json", "src/pricing.jsonx")
    assert not classify.matches("src/pricing.json", "x/src/pricing.json")
    assert not classify.matches("src/pricing.json", "backend/pricing.json")


def test_is_bot_data_partition():
    assert classify.is_bot_data("src/pricing.json")
    for path in (DOCS + CODE + ["src/parser.js", "src/pricing.py"]):
        assert not classify.is_bot_data(path), path


def test_data_only_requires_a_nonempty_all_bot_data_set():
    assert not classify.data_only([])
    assert classify.data_only(["src/pricing.json"])
    for intruder in DOCS + CODE:
        assert not classify.data_only(["src/pricing.json", intruder]), intruder


def test_classify_bot_data_only_gets_the_cheap_class():
    docs_only, data_only, reason = classify.classify(
        {"name": "pull_request", "repository": "o/r", "pull_request": "17"},
        lambda argv: "src/pricing.json\n",
    )
    assert (docs_only, data_only) == (False, True)
    assert "data-only" in reason


def test_classify_mixed_pricing_json_and_code_runs_everything():
    # The class is the SET of changed paths, never individual files: one
    # code file beside the data file is a full run.
    docs_only, data_only, reason = classify.classify(
        {"name": "pull_request", "repository": "o/r", "pull_request": "17"},
        lambda argv: "src/pricing.json\nbackend/app.py\n",
    )
    assert (docs_only, data_only) == (False, False)
    assert "outside documentation" in reason


def test_classify_pricing_json_beside_docs_runs_everything():
    docs_only, data_only, reason = classify.classify(
        {"name": "pull_request", "repository": "o/r", "pull_request": "17"},
        lambda argv: "src/pricing.json\nREADME.md\n",
    )
    assert (docs_only, data_only) == (False, False)
    assert "outside documentation" in reason


# ---------------------------------------------------------------------------
# classify_changes: reading the changed paths
# ---------------------------------------------------------------------------


def test_pr_event_reads_the_pull_request_files_api():
    calls = []

    def run(argv):
        calls.append(argv)
        return "README.md\ndocs/guide.md\n"

    paths = classify.changed_paths(
        {"name": "pull_request", "repository": "o/r", "pull_request": "17"},
        run,
    )
    assert paths == ["README.md", "docs/guide.md"]
    argv = calls[0]
    assert "repos/o/r/pulls/17/files" in argv
    assert "--paginate" in argv  # the list must be complete, not a first page


def test_pr_event_with_an_unusable_number_over_runs():
    for number in (None, "", "abc"):
        paths = classify.changed_paths(
            {"name": "pull_request", "repository": "o/r",
             "pull_request": number},
            lambda argv: "",
        )
        assert paths is None


# The runs-list URL segment the classifier reads on a push (issue #208);
# the walk itself has its own module, test_ci_classify_verified_base.py.
WORKFLOW_RUNS_URL = "actions/workflows/ci-gate.yml/runs"


def _url(argv):
    """The single repos/… URL of a stubbed gh api call."""
    return next(arg for arg in argv if arg.startswith("repos/"))


def test_push_event_diffs_against_the_previous_master_sha():
    # Normal behavior preserved: the newest completed master run before
    # this push executed its legs over `before` itself, so the verified
    # base IS `before` and the classified range is the push's own.
    calls = []

    def run(argv):
        calls.append(argv)
        url = _url(argv)
        if WORKFLOW_RUNS_URL in url:
            return f"{'a' * 40} completed success 21\n"
        if "/jobs" in url:
            return "classify success\naggregate success\ntests success\n"
        return "src/app.jsx\n"

    paths = classify.changed_paths(
        {"name": "push", "repository": "o/r",
         "before": "a" * 40, "sha": "b" * 40},
        run,
    )
    assert paths == ["src/app.jsx"]
    assert f"repos/o/r/compare/{'a' * 40}...{'b' * 40}" in calls[-1]
    runs_url = next(arg for arg in calls[0] if WORKFLOW_RUNS_URL in arg)
    assert "branch=master" in runs_url
    assert calls[0][calls[0].index("-H") + 1] == "Cache-Control: no-cache"


def test_push_event_with_a_new_branch_over_runs():
    # A new branch has no previous SHA (before is all zeros): there is no
    # changed set to read, so the fallback runs everything.
    assert classify.changed_paths(
        {"name": "push", "repository": "o/r",
         "before": "0" * 40, "sha": "b" * 40},
        lambda argv: "",
    ) is None


def test_push_event_with_a_missing_before_over_runs():
    assert classify.changed_paths(
        {"name": "push", "repository": "o/r", "before": None,
         "sha": "b" * 40},
        lambda argv: "",
    ) is None


def test_truncated_file_list_over_runs():
    # The compare endpoint caps its files collection at 300 and the pulls
    # files endpoint at 3000; a list at either cap may be truncated, and a
    # truncated list that read as documentation-only would skip gates over
    # code.
    def many(n):
        def run(argv):
            url = _url(argv)
            if WORKFLOW_RUNS_URL in url:
                return f"{'a' * 40} completed success 22\n"
            if "/jobs" in url:
                return "classify success\naggregate success\ntests success\n"
            return "\n".join(f"p{i}.md" for i in range(n))
        return run

    assert classify.changed_paths(
        {"name": "push", "repository": "o/r",
         "before": "a" * 40, "sha": "b" * 40},
        many(classify.COMPARE_FILES_CAP),
    ) is None
    assert classify.changed_paths(
        {"name": "pull_request", "repository": "o/r", "pull_request": "17"},
        many(classify.PULL_REQUEST_FILES_CAP),
    ) is None


def test_unreadable_file_list_over_runs():
    def run(argv):
        raise OSError("gh failed")

    assert classify.changed_paths(
        {"name": "pull_request", "repository": "o/r", "pull_request": "17"},
        run,
    ) is None


# The exact jq strings the classifier must read the two file lists
# through; the tests below assert them on the stubbed calls verbatim.
PR_FILES_JQ = ".[] | .filename, (.previous_filename // empty)"
COMPARE_FILES_JQ = ".files[] | .filename, (.previous_filename // empty)"


def _apply_files_jq(jq, rows):
    """The stdout a files-list jq yields for GitHub's `rows`.

    Emulates the projections in play over both payload shapes — the
    pulls files endpoint's ARRAY and the compare endpoint's `files`
    object: `.filename` always, plus `.previous_filename` where the jq
    names it and the row carries one (`// empty`). Any other selector
    shape fails the stub rather than reading as a clean list.
    """
    if not jq.endswith(".filename") and "previous_filename" not in jq:
        raise AssertionError(f"unexpected file-list jq: {jq!r}")
    selected = rows["files"] if ".files[]" in jq else rows
    lines = []
    for row in selected:
        lines.append(row["filename"])
        if "previous_filename" in jq and "previous_filename" in row:
            lines.append(row["previous_filename"])
    return "".join(line + "\n" for line in lines)


def test_pr_rename_row_yields_both_paths_and_classifies_full():
    # Issue #245. GitHub reports a rename as ONE row: `filename` is the
    # NEW path and the old path arrives as `previous_filename`. A
    # selector that keeps only `.filename` reads backend/auth.py renamed
    # to backend/auth.md as a docs-only change, skips every leg and
    # reports a green aggregate over broken code; the old path must be
    # projected alongside the new.
    rows = [{"status": "renamed", "filename": "backend/auth.md",
             "previous_filename": "backend/auth.py"}]
    calls = []

    def run(argv):
        calls.append(argv)
        return _apply_files_jq(argv[argv.index("--jq") + 1], rows)

    event = {"name": "pull_request", "repository": "o/r",
             "pull_request": "17"}
    assert classify.changed_paths(event, run) == [
        "backend/auth.md", "backend/auth.py"]
    assert calls[0][calls[0].index("--jq") + 1] == PR_FILES_JQ

    docs_only, data_only, reason = classify.classify(event, run)
    assert (docs_only, data_only) == (False, False)
    assert "outside documentation" in reason


def test_push_compare_rename_row_yields_both_paths():
    # Issue #245 on the push path: the compare read projects the old
    # path too, so a rename inside the verified-base range is not read
    # as its new docs-only name alone.
    rows = [{"status": "renamed", "filename": "backend/auth.md",
             "previous_filename": "backend/auth.py"}]
    calls = []

    def run(argv):
        calls.append(argv)
        url = _url(argv)
        if WORKFLOW_RUNS_URL in url:
            return f"{'a' * 40} completed success 21\n"
        if "/jobs" in url:
            return "classify success\naggregate success\ntests success\n"
        return _apply_files_jq(argv[argv.index("--jq") + 1],
                               {"files": rows})

    paths = classify.changed_paths(
        {"name": "push", "repository": "o/r",
         "before": "a" * 40, "sha": "b" * 40},
        run,
    )
    assert paths == ["backend/auth.md", "backend/auth.py"]
    compare = next(call for call in calls if "/compare/" in _url(call))
    assert compare[compare.index("--jq") + 1] == COMPARE_FILES_JQ


def test_reverse_rename_yields_both_paths():
    # A .md renamed INTO a code path carries the same exposure in
    # reverse: the old docs path and the new code path both count.
    rows = [{"status": "renamed", "filename": "backend/auth.py",
             "previous_filename": "docs/a.md"}]

    def run(argv):
        return _apply_files_jq(argv[argv.index("--jq") + 1], rows)

    assert classify.changed_paths(
        {"name": "pull_request", "repository": "o/r", "pull_request": "17"},
        run,
    ) == ["backend/auth.py", "docs/a.md"]


def test_docs_only_rename_yields_both_paths_and_stays_docs_only():
    # Both paths of a rename between two documentation paths are
    # projected, and the change still classifies docs-only — the fix
    # widens what is SEEN, never what counts as documentation.
    rows = [{"status": "renamed", "filename": "docs/b.md",
             "previous_filename": "docs/a.md"}]

    def run(argv):
        return _apply_files_jq(argv[argv.index("--jq") + 1], rows)

    event = {"name": "pull_request", "repository": "o/r",
             "pull_request": "17"}
    assert classify.changed_paths(event, run) == [
        "docs/b.md", "docs/a.md"]
    docs_only, data_only, reason = classify.classify(event, run)
    assert (docs_only, data_only) == (True, False)
    assert "documentation-only" in reason


def test_row_without_previous_filename_yields_one_path():
    # Control: a row that names no previous path projects exactly its
    # own filename, as before.
    rows = [{"status": "added", "filename": "backend/new.py"}]

    def run(argv):
        return _apply_files_jq(argv[argv.index("--jq") + 1], rows)

    assert classify.changed_paths(
        {"name": "pull_request", "repository": "o/r", "pull_request": "17"},
        run,
    ) == ["backend/new.py"]


# ---------------------------------------------------------------------------
# classify_changes: the classification
# ---------------------------------------------------------------------------


def test_classify_docs_only():
    docs_only, data_only, reason = classify.classify(
        {"name": "pull_request", "repository": "o/r", "pull_request": "17"},
        lambda argv: "README.md\nNOTICE\n",
    )
    assert (docs_only, data_only) == (True, False)
    assert "documentation-only" in reason


def test_classify_mixed_change_runs_everything():
    docs_only, data_only, reason = classify.classify(
        {"name": "pull_request", "repository": "o/r", "pull_request": "17"},
        lambda argv: "README.md\nbackend/app.py\n",
    )
    assert (docs_only, data_only) == (False, False)
    assert "outside documentation" in reason


def test_classify_unreadable_paths_run_everything():
    docs_only, data_only, reason = classify.classify(
        {"name": "push"}, lambda argv: "")
    assert (docs_only, data_only) == (False, False)
    assert "full" in reason


def test_event_from_environment():
    event = classify.event_from_environment({
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REPOSITORY": "o/r",
        "BEFORE_SHA": "a" * 40,
        "GITHUB_SHA": "b" * 40,
    })
    assert event == {"name": "push", "repository": "o/r",
                     "before": "a" * 40, "sha": "b" * 40,
                     "pull_request": None}


def test_write_outputs(tmp_path):
    out = tmp_path / "output.txt"
    classify.write_outputs(str(out), True, False,
                           "documentation-only change: 2 paths")
    assert out.read_text(encoding="utf-8") == (
        "docs_only=true\n"
        "data_only=false\n"
        "reason=documentation-only change: 2 paths\n"
    )


def test_write_outputs_records_the_data_only_narrowing(tmp_path):
    out = tmp_path / "output.txt"
    classify.write_outputs(str(out), False, True,
                           "bot-data-only change: 1 paths")
    assert out.read_text(encoding="utf-8") == (
        "docs_only=false\n"
        "data_only=true\n"
        "reason=bot-data-only change: 1 paths\n"
    )


def test_main_writes_full_run_over_an_unhandled_event(tmp_path, monkeypatch):
    # workflow_dispatch carries no PR number and no before SHA: the
    # fallback must run the full gate set, not read as documentation.
    out = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert classify.main() == 0
    text = out.read_text(encoding="utf-8")
    assert "docs_only=false" in text
    assert "data_only=false" in text


# ---------------------------------------------------------------------------
# aggregate_gate: the fold
# ---------------------------------------------------------------------------

LEGS = ("classify", "tests", "test-data", "lint", "types", "eslint",
        "smoke", "audit", "actionlint", "speed", "codeql")


def _needs(**overrides):
    """A green needs document, with per-leg result overrides.

    Key a leg as ``<name>__result`` to set its result, ``docs_only`` /
    ``data_only`` to flip the classifier outputs.
    """
    doc = {
        "classify": {"result": "success", "outputs": {"docs_only": "false",
                                                      "data_only": "false"}},
    }
    for leg in LEGS[1:]:
        doc[leg] = {"result": "success"}
    doc["classify"]["outputs"]["docs_only"] = overrides.pop(
        "docs_only", "false")
    doc["classify"]["outputs"]["data_only"] = overrides.pop(
        "data_only", "false")
    for key, value in overrides.items():
        name = key.removesuffix("__result")
        if name == "classify":
            doc["classify"]["result"] = value
        else:
            doc[name] = {"result": value}
    return doc


def test_aggregate_expected_legs_cover_the_ci_gate_jobs():
    assert aggregate.EXPECTED_LEGS == LEGS


def test_aggregate_all_green_passes():
    verdict, message = aggregate.decide(_needs())
    assert verdict == aggregate.PASSED
    assert "tests=success" in message


def test_aggregate_one_failed_fails_and_names_it():
    verdict, message = aggregate.decide(_needs(tests__result="failure"))
    assert verdict == aggregate.FAILED
    assert "tests=failure" in message


def test_aggregate_docs_only_skips_pass():
    doc = _needs(docs_only="true")
    for leg in LEGS[1:]:
        doc[leg] = {"result": "skipped"}
    verdict, message = aggregate.decide(doc)
    assert verdict == aggregate.PASSED
    assert "docs-only" in message


def test_aggregate_data_only_skips_pass_and_names_the_cheap_legs():
    doc = _needs(data_only="true")
    for leg in LEGS[1:]:
        doc[leg] = {
            "result": "skipped" if leg not in aggregate.CHEAP_LEGS
            else "success"}
    verdict, message = aggregate.decide(doc)
    assert verdict == aggregate.PASSED
    assert "data-only" in message
    for cheap in sorted(aggregate.CHEAP_LEGS):
        assert cheap in message


def test_aggregate_data_only_cannot_skip_the_cheap_legs():
    # Fail closed: a data-only run that skipped lint or smoke anyway is
    # a fold/workflow disagreement, and the aggregate must go red — the
    # boot-and-serve check is why the cheap class exists.
    doc = _needs(data_only="true")
    for leg in LEGS[1:]:
        doc[leg] = {"result": "skipped"}
    verdict, message = aggregate.decide(doc)
    assert verdict == aggregate.FAILED
    assert "lint=skipped" in message
    assert "smoke=skipped" in message


def test_aggregate_data_only_output_missing_reads_as_not_data_only():
    # Fail closed: a classify entry without the data_only output cannot
    # turn a skipped leg into a pass.
    doc = _needs()
    doc["classify"] = {"result": "success", "outputs": {
        "docs_only": "false"}}
    doc["tests"] = {"result": "skipped"}
    verdict, message = aggregate.decide(doc)
    assert verdict == aggregate.FAILED
    assert "tests=skipped" in message


def test_aggregate_non_data_only_skip_fails():
    # A skip beside a data_only=false output is still a disagreement,
    # the way a non-docs skip has always been.
    doc = _needs(data_only="false")
    doc["speed"] = {"result": "skipped"}
    verdict, message = aggregate.decide(doc)
    assert verdict == aggregate.FAILED
    assert "speed=skipped" in message


def test_aggregate_non_docs_skip_fails():
    doc = _needs(docs_only="false")
    doc["smoke"] = {"result": "skipped"}
    verdict, message = aggregate.decide(doc)
    assert verdict == aggregate.FAILED
    assert "smoke=skipped" in message


def test_aggregate_cancelled_fails():
    verdict, message = aggregate.decide(_needs(speed__result="cancelled"))
    assert verdict == aggregate.FAILED
    assert "speed=cancelled" in message


def test_aggregate_missing_fails():
    doc = _needs()
    del doc["audit"]
    verdict, message = aggregate.decide(doc)
    assert verdict == aggregate.FAILED
    assert "audit" in message


def test_aggregate_unknown_result_fails():
    verdict, message = aggregate.decide(_needs(lint__result="timed_out"))
    assert verdict == aggregate.FAILED
    assert "lint=timed_out" in message


def test_aggregate_extra_leg_is_gated_by_its_result():
    doc = _needs()
    doc["future"] = {"result": "success"}
    assert aggregate.decide(doc)[0] == aggregate.PASSED
    doc["future"] = {"result": "failure"}
    verdict, message = aggregate.decide(doc)
    assert verdict == aggregate.FAILED
    assert "future=failure" in message


def test_aggregate_docs_only_output_missing_reads_as_not_docs_only():
    # Fail closed: a classify entry without the docs_only output cannot
    # turn a skipped leg into a pass.
    doc = _needs()
    doc["classify"] = {"result": "success", "outputs": {}}
    doc["smoke"] = {"result": "skipped"}
    verdict, message = aggregate.decide(doc)
    assert verdict == aggregate.FAILED
    assert "smoke=skipped" in message


def test_aggregate_summary_is_markdown_naming_every_leg(tmp_path):
    doc = _needs(docs_only="true")
    doc["classify"]["outputs"]["reason"] = "documentation-only change: 3 paths"
    for leg in LEGS[1:]:
        doc[leg] = {"result": "skipped"}
    summary = tmp_path / "summary.md"
    code = aggregate.main_with(
        doc, summary_path=str(summary))
    assert code == 0
    text = summary.read_text(encoding="utf-8")
    assert "docs-only" in text
    assert "classification: documentation-only change: 3 paths" in text
    for leg in LEGS:
        assert leg in text


def test_aggregate_summary_names_the_data_only_narrowing(tmp_path):
    doc = _needs(data_only="true")
    doc["classify"]["outputs"]["reason"] = "bot-data-only change: 1 paths"
    for leg in LEGS[1:]:
        doc[leg] = {
            "result": "skipped" if leg not in aggregate.CHEAP_LEGS
            else "success"}
    summary = tmp_path / "summary.md"
    assert aggregate.main_with(doc, summary_path=str(summary)) == 0
    text = summary.read_text(encoding="utf-8")
    assert "data-only narrowing: true" in text
    assert "classification: bot-data-only change: 1 paths" in text
    assert "data-only" in text


def test_aggregate_summary_without_a_classify_reason_omits_the_line(tmp_path):
    summary = tmp_path / "summary.md"
    aggregate.main_with(_needs(), summary_path=str(summary))
    text = summary.read_text(encoding="utf-8")
    assert "classification:" not in text


def test_aggregate_main_green_exits_zero(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("NEEDS_JSON", json.dumps(_needs()))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert aggregate.main() == 0


def test_aggregate_main_red_exits_one(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("NEEDS_JSON", json.dumps(_needs(tests__result="failure")))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert aggregate.main() == 1
    assert summary.read_text(encoding="utf-8")
