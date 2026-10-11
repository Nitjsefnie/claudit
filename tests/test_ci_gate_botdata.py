"""The ci-gate bot-data class: classifier and fold pins (issues #455, #488).

The bot-data classification (the refresh bot's exact push signature ->
the cheap class) and the aggregate fold's data-only branch are the
issue-455/-488 additions to the ci-gate classifier and aggregate fold,
so their pins live in their own module rather than growing
test_ci_gate_modules.py past the test size ceiling. Loader shape
matches that file: scripts/ci is not a package and deliberately has no
__init__.py — it holds standalone CI entry points, so both modules load
by path here too.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    """Import scripts/ci/<name>.py by path."""
    path = REPO_ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


classify = _load("classify_changes")
aggregate = _load("aggregate_gate")


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


# ---------------------------------------------------------------------------
# classify_changes: the bot-data class
# ---------------------------------------------------------------------------


def test_bot_signature_is_the_refresh_bots_staged_paths():
    # The push stages exactly these three paths (refresh-pricing.yml's
    # `git add`): rate entries in src/pricing.json, the PRICING_VERSION
    # bump in backend/constants.py (SV-RATE-REFRESH), and the notice
    # baseline src/pricing_notices.json (#888). A signature, not a
    # filter: the cheap class exists for generated data the refresh
    # job ran the full suite against BEFORE pushing, and only the bot's
    # exact commit shapes carry that pre-test.
    assert classify.BOT_SIGNATURE == frozenset(
        {"src/pricing.json", "backend/constants.py",
         "src/pricing_notices.json"})


def test_bot_signature_spans_the_refresh_push_workflow():
    # The contract issue #932 broke on: refresh-pricing.yml's push job
    # stages the bot's paths and BOT_SIGNATURE names them, but each side
    # was pinned only inside its own suite — a fourth staged path
    # (#888) left both green. Parse the push job's git add and compare
    # the SET the workflow stages against the classifier's signature.
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "refresh-pricing.yml")
        .read_text(encoding="utf-8"))
    runs = [step.get("run", "") for step in workflow["jobs"]["push"]["steps"]
            if isinstance(step, dict)]
    staged = re.findall(r'push-repo" add ([^\n]+)', "\n".join(runs))
    assert staged, "the push job no longer stages paths with git add"
    paths = {path for line in staged for path in line.split()}
    assert paths == classify.BOT_SIGNATURE


def test_data_only_requires_the_exact_signature():
    assert not classify.data_only([])
    assert classify.data_only(["src/pricing.json", "backend/constants.py"])
    # Listing order and duplicates are set noise, not a third path.
    assert classify.data_only(
        ["backend/constants.py", "src/pricing.json", "src/pricing.json"])


def test_data_only_accepts_the_three_path_bot_commit():
    # Every refresh commit since pricing_notices.json joined the push
    # (#888, ef8b131c) carries this shape: the notices baseline moved
    # with the data. Under the pair-only signature it classified full
    # and ran the whole matrix on a pure bot push (issue #932).
    assert classify.data_only(
        ["src/pricing.json", "backend/constants.py",
         "src/pricing_notices.json"])


def test_data_only_refuses_a_third_path():
    for intruder in (DOCS + CODE + ["src/parser.js", "src/pricing.py"]):
        assert not classify.data_only(
            ["src/pricing.json", "backend/constants.py", intruder]), intruder
        # The notices baseline does not widen the class: the triple
        # beside anything else is not the bot either.
        assert not classify.data_only(
            ["src/pricing.json", "backend/constants.py",
             "src/pricing_notices.json", intruder]), intruder


def test_data_only_refuses_either_half_alone():
    # constants.py without pricing.json is ordinary code. pricing.json
    # without constants.py narrows the issue-455 class on purpose: the
    # bot never produces it (SV-RATE-REFRESH bumps in the same commit),
    # so a lone hand-edited rate file has no pre-test behind it and runs
    # the full matrix. The notices baseline alone — or beside only one
    # half — the bot never commits either (it is written only inside the
    # moves branch, together with both data files).
    assert not classify.data_only(["backend/constants.py"])
    assert not classify.data_only(["src/pricing.json"])
    assert not classify.data_only(["src/pricing_notices.json"])
    assert not classify.data_only(
        ["src/pricing.json", "src/pricing_notices.json"])
    assert not classify.data_only(
        ["backend/constants.py", "src/pricing_notices.json"])


def test_classify_the_bots_signature_gets_the_cheap_class():
    docs_only, data_only, _comment, reason = classify.classify(
        {"name": "pull_request", "repository": "o/r", "pull_request": "17"},
        lambda argv: "src/pricing.json\nbackend/constants.py\n",
    )
    assert (docs_only, data_only) == (False, True)
    assert "data-only" in reason


def test_classify_the_bots_three_path_commit_gets_the_cheap_class():
    docs_only, data_only, _comment, reason = classify.classify(
        {"name": "pull_request", "repository": "o/r", "pull_request": "17"},
        lambda argv: ("src/pricing.json\nbackend/constants.py\n"
                      "src/pricing_notices.json\n"),
    )
    assert (docs_only, data_only) == (False, True)
    assert "data-only" in reason


def test_classify_an_ordinary_push_stays_full():
    # Direction two of the proof (issue #932): a non-bot change must
    # keep data_only=false — ci-gate's expensive legs and the tests
    # canary gate on data_only != 'true', so a misclassification here
    # never errors, it silently stops gating real pushes.
    docs_only, data_only, _comment, reason = classify.classify(
        {"name": "pull_request", "repository": "o/r", "pull_request": "17"},
        lambda argv: "backend/app.py\nVERSION\n",
    )
    assert (docs_only, data_only) == (False, False)
    assert "outside documentation" in reason


def test_classify_mixed_pricing_json_and_code_runs_everything():
    # The class is the SET of changed paths, never individual files: one
    # code file beside the data file is a full run.
    docs_only, data_only, _comment, reason = classify.classify(
        {"name": "pull_request", "repository": "o/r", "pull_request": "17"},
        lambda argv: "src/pricing.json\nbackend/app.py\n",
    )
    assert (docs_only, data_only) == (False, False)
    assert "outside documentation" in reason


def test_classify_pricing_json_beside_docs_runs_everything():
    docs_only, data_only, _comment, reason = classify.classify(
        {"name": "pull_request", "repository": "o/r", "pull_request": "17"},
        lambda argv: "src/pricing.json\nREADME.md\n",
    )
    assert (docs_only, data_only) == (False, False)
    assert "outside documentation" in reason


def test_write_outputs_records_the_data_only_narrowing(tmp_path):
    out = tmp_path / "output.txt"
    classify.write_outputs(str(out), False, True, False,
                           "bot-data-only change: 1 paths")
    assert out.read_text(encoding="utf-8") == (
        "docs_only=false\n"
        "data_only=true\n" "comment_only=false\n"
        "reason=bot-data-only change: 1 paths\n"
    )


# ---------------------------------------------------------------------------
# aggregate_gate: the data-only fold
# ---------------------------------------------------------------------------

# Lockstep with the fold's own leg tuple, not a copy of the modules
# file's: the pins here judge exactly the document aggregate_gate
# expects, and a leg added there moves this document with it.
LEGS = aggregate.EXPECTED_LEGS


def _needs(cheap="success", expensive="skipped", data_only="true",
           docs_only="false"):
    """A needs document shaped for the data-only fold pins.

    Cheap legs sit at ``cheap``, every other leg at ``expensive``, so
    the helper states the class split once instead of each test looping
    over legs by hand.
    """
    doc = {"classify": {"result": "success",
                        "outputs": {"docs_only": docs_only,
                                    "data_only": data_only}}}
    for leg in LEGS[1:]:
        doc[leg] = {"result": cheap if leg in aggregate.CHEAP_LEGS
                    else expensive}
    return doc


def test_aggregate_data_only_skips_pass_and_names_the_cheap_legs():
    verdict, message = aggregate.decide(_needs())
    assert verdict == aggregate.PASSED
    assert "data-only" in message
    for cheap in sorted(aggregate.CHEAP_LEGS):
        assert cheap in message


def test_aggregate_data_only_cannot_skip_the_cheap_legs():
    # Fail closed: a data-only run that skipped lint or smoke anyway is
    # a fold/workflow disagreement, and the aggregate must go red — the
    # boot-and-serve check is why the class exists.
    verdict, message = aggregate.decide(_needs(cheap="skipped"))
    assert verdict == aggregate.FAILED
    assert "lint=skipped" in message
    assert "smoke=skipped" in message


def test_aggregate_data_only_output_missing_reads_as_not_data_only():
    # Fail closed: a classify entry without the data_only output cannot
    # turn a skipped leg into a pass.
    doc = _needs()
    doc["classify"]["outputs"] = {"docs_only": "false"}
    doc["tests"] = {"result": "skipped"}
    verdict, message = aggregate.decide(doc)
    assert verdict == aggregate.FAILED
    assert "tests=skipped" in message


def test_aggregate_non_data_only_skip_fails():
    # A skip beside a data_only=false output is still a disagreement,
    # the way a non-docs skip has always been.
    doc = _needs(data_only="false", expensive="success")
    doc["test-data"] = {"result": "skipped"}
    verdict, message = aggregate.decide(doc)
    assert verdict == aggregate.FAILED
    assert "test-data=skipped" in message


def test_aggregate_summary_names_the_data_only_narrowing(tmp_path):
    doc = _needs()
    doc["classify"]["outputs"]["reason"] = "bot-data-only change: 1 paths"
    summary = tmp_path / "summary.md"
    assert aggregate.main_with(doc, summary_path=str(summary)) == 0
    text = summary.read_text(encoding="utf-8")
    assert "data-only narrowing: true" in text
    assert "classification: bot-data-only change: 1 paths" in text
    assert "data-only" in text
