"""The ci-gate bot-data class: classifier and fold pins (issue #455).

The bot-data classification (src/pricing.json alone -> the cheap class)
and the aggregate fold's data-only branch are the issue-455 additions
to the ci-gate classifier and aggregate fold, so their pins live in
their own module rather than growing test_ci_gate_modules.py past the
test size ceiling. Loader shape matches that file: scripts/ci is not a
package and deliberately has no __init__.py — it holds standalone CI
entry points, so both modules load by path here too.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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


# ---------------------------------------------------------------------------
# classify_changes: the bot-data class
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


def test_write_outputs_records_the_data_only_narrowing(tmp_path):
    out = tmp_path / "output.txt"
    classify.write_outputs(str(out), False, True,
                           "bot-data-only change: 1 paths")
    assert out.read_text(encoding="utf-8") == (
        "docs_only=false\n"
        "data_only=true\n"
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
    doc["speed"] = {"result": "skipped"}
    verdict, message = aggregate.decide(doc)
    assert verdict == aggregate.FAILED
    assert "speed=skipped" in message


def test_aggregate_summary_names_the_data_only_narrowing(tmp_path):
    doc = _needs()
    doc["classify"]["outputs"]["reason"] = "bot-data-only change: 1 paths"
    summary = tmp_path / "summary.md"
    assert aggregate.main_with(doc, summary_path=str(summary)) == 0
    text = summary.read_text(encoding="utf-8")
    assert "data-only narrowing: true" in text
    assert "classification: bot-data-only change: 1 paths" in text
    assert "data-only" in text
