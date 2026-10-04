"""Unit tests for the gate trigger set.

The gate-freshness check goes red when master holds a commit an open
head lacks whose changed paths intersect the trigger set. These tests
pin the set: the directory families, the explicit members (the ratchet
data file included — a tighten raises what the gates demand of the
same source tree), the convention-read configs the extraction cannot
see, and the property that every file a workflow names is covered.
"""
from __future__ import annotations

import importlib.util
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


def _real_workflow_texts():
    texts = {}
    for path in sorted(REPO_ROOT.glob(".github/workflows/*.yml")):
        texts[path.name] = path.read_text(encoding="utf-8")
    return texts


def _tracked():
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True,
        check=True)
    return set(out.stdout.split())


gts = _load("gate_trigger_set")


# --- families, explicit members, hand-held configs -----------------------

def test_families_are_the_gate_shaped_directories():
    assert gts.FAMILIES == (
        ".github/workflows/",
        ".github/actions/",
        "scripts/ci/",
    )


def test_explicit_names_the_ratchet_data_and_the_scanner_config():
    assert ".github/ci-thresholds.json" in gts.EXPLICIT
    assert ".gitleaks.toml" in gts.EXPLICIT


def test_explicit_names_every_convention_read_config():
    # Tool-discovery configs: bare pylint/pycodestyle/pyright/eslint
    # read these without any workflow naming them, so the extraction
    # cannot see them; they are held by hand, and each must exist.
    assert set(gts.EXPLICIT) == {
        ".github/ci-thresholds.json",
        ".gitleaks.toml",
        "backend/requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "requirements-pip-audit.txt",
        "requirements-zizmor.txt",
        ".pylintrc",
        "setup.cfg",
        "pyrightconfig.json",
        "eslint.config.mjs",
    }


def test_hand_held_configs_exist_in_the_tree():
    for name in gts.EXPLICIT:
        assert (REPO_ROOT / name).is_file(), name


def test_is_triggered_on_each_family_and_explicit_member():
    assert gts.is_triggered(".github/workflows/ci-gate.yml")
    assert gts.is_triggered(".github/actions/suite-bench/action.yml")
    assert gts.is_triggered("scripts/ci/thresholds.py")
    for name in gts.EXPLICIT:
        assert gts.is_triggered(name)


def test_bare_family_name_itself_triggers():
    # `zizmor .github/workflows/` tokenizes as `.github/workflows`.
    assert gts.is_triggered(".github/workflows")


def test_exempt_beats_every_other_part_of_the_set():
    # The adjudicated command references are not gate parameters: no
    # leg's verdict depends on them, so they must never trigger. Pin
    # the precedence itself: exempt wins even when a family would
    # otherwise match.
    for name in ("src/pricing.json", "backend/constants.py", "VERSION",
                 "backend", "tests", "fixtures"):
        assert name in gts.EXEMPT, name
        assert not gts.is_triggered(name)
    assert not gts.is_triggered(
        "src/pricing.json", families=("src/",))
    assert gts.is_triggered("src/pricing.json", exempt={},
                            families=("src/",))


def test_plain_source_paths_are_not_triggers():
    assert not gts.is_triggered("backend/app.py")
    assert not gts.is_triggered("src/app.jsx")
    assert not gts.is_triggered("tests/test_parse.py")
    assert not gts.is_triggered("README.md")


# --- extraction -----------------------------------------------------------

def test_extraction_finds_named_files_and_local_actions():
    text = "\n".join([
        "run: python3 scripts/ci/foo.py",
        "uses: ./.github/actions/suite-bench",
        "run: pip install -r backend/requirements.txt",
        "key: pip-${{ runner.os }}-${{ hashFiles('backend/requirements.txt',"
        " 'requirements-test.txt') }}",
    ])
    refs = gts.references_from_workflows(
        {"w.yml": text},
        {"scripts/ci/foo.py", "backend/requirements.txt",
         "requirements-test.txt", ".github/actions/suite-bench/action.yml"},
    )
    assert refs == {
        "scripts/ci/foo.py",
        ".github/actions/suite-bench",
        "backend/requirements.txt",
        "requirements-test.txt",
    }


def test_extraction_ignores_untracked_and_foreign_shapes():
    text = "\n".join([
        "uses: actions/checkout@abc",
        "run: pip install requests",
        "run: echo https://example.com/x.yml",
        "run: echo ${{ runner.os }}",
    ])
    assert gts.references_from_workflows(
        {"w.yml": text}, set()) == set()


def test_extraction_over_the_real_workflows_is_live():
    # An absence assertion must prove its oracle is live: the extractor
    # runs over the real workflows and finds the requirements files.
    refs = gts.references_from_workflows(
        _real_workflow_texts(), _tracked())
    for name in ("backend/requirements.txt", "requirements-dev.txt",
                 "requirements-test.txt"):
        assert name in refs, name


def test_every_reference_the_real_workflows_name_is_covered():
    # The load-bearing property: any tracked file a workflow's command
    # lines name is inside the trigger set, so a new parameter file
    # cannot be silently missed. The extraction is deliberately
    # over-broad (prose comments are its only removal); over-broad
    # fails safe — an uncovered reference turns the test red and
    # forces an explicit decision.
    refs = gts.references_from_workflows(
        _real_workflow_texts(), _tracked())
    uncovered = gts.uncovered_references(refs)
    assert uncovered == [], (
        "workflows reference tracked files outside the trigger set: "
        f"{uncovered}")
