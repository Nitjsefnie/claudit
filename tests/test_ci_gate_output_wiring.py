"""The classify step's written outputs actually gate the legs (issue #540).

The workflow-text pins judge ci-gate.yml as TEXT: that each leg carries
the narrowing `if:`, that the classify job declares `docs_only` /
`data_only`, and that the classifier writes that exact byte shape. None
of them can see the layer between the two — the classifier RUNS, writes
the step-output file Actions reads, and each leg's condition is
evaluated against what the file actually carried. Every one of a
step-output rename, a key that stops being written, and an inverted
condition passes all of them and fails the gate: the decision prints and
does not bind, and the full matrix runs on the refresh bot's hourly push.

So this module closes that layer instead of adding more text pins. It
runs scripts/ci/classify_changes.py as a subprocess over a stub `gh`,
parses the GITHUB_OUTPUT file the way Actions does, and evaluates each
leg's own `if:` against those parsed outputs — the whole chain, so the
question each test asks is which legs would actually run.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CLASSIFIER = REPO_ROOT / "scripts" / "ci" / "classify_changes.py"
CI_GATE = REPO_ROOT / ".github" / "workflows" / "ci-gate.yml"

# These name CHANGED PATHS, not a rate: the classifier's own
# BOT_SIGNATURE is the refresh bot's push, and the file's rates are never
# read, priced or asserted on here (sv-test-data: allow).
BOT_PATHS = ["backend/constants.py", "src/pricing.json"]  # sv-test-data: allow
DOC_PATHS = ["README.md", "docs/guide.md"]
CODE_PATHS = ["backend/app.py", "src/pricing.json"]  # sv-test-data: allow

BEFORE = "4bf181aaaa2c2f7d550d10bcf47658df455317aa"
HEAD = "7459e46f0dd2ba1942d5ba4932b24b20a212e2d2"

# A `needs.classify.outputs.<name>` reference inside a leg's condition.
REFERENCE = re.compile(r"needs\.classify\.outputs\.([A-Za-z_][A-Za-z0-9_-]*)")
# The condition shapes this evaluator translates. Anything else that
# reads a narrowing output makes the evaluation below refuse rather than
# guess, so a rewritten condition is noticed here instead of being
# silently mis-read as the shape it happens to resemble.
CONJUNCT = (r"needs\.classify\.outputs\.([A-Za-z_][A-Za-z0-9_-]*)"
            r" != 'true'")
CONJUNCT_RE = re.compile(CONJUNCT)
EVALUABLE = re.compile(rf"^{CONJUNCT}(?: && {CONJUNCT})?$")

# The stub `gh`: the classifier shells out to it, so the reads it
# decides on are answered here. The push path reads three endpoints —
# the master runs list, one run's jobs, then the compare — and each is
# answered in the shape the jq filter would have produced.
STUB_GH = '''#!/usr/bin/env python3
"""A stand-in for `gh` over the reads classify_changes.py makes."""
import sys

url = next((arg for arg in sys.argv[1:] if arg.startswith("repos/")), "")
if "/runs?" in url:
    print("{head} completed success 4242")
elif "/jobs?" in url:
    print("tests success")
elif "/compare/" in url:
    for path in {paths!r}:
        print(path)
else:
    sys.exit("stub gh: unexpected read " + url)
'''


def _ci_gate():
    """The ci-gate workflow document."""
    return yaml.safe_load(CI_GATE.read_text(encoding="utf-8")) or {}


def _legs(gate):
    """Every job but the classifier and the always-running fold."""
    return {name: job for name, job in (gate.get("jobs") or {}).items()
            if name not in ("classify", "aggregate")}


def _stub_gh(directory, paths):
    """An executable stub `gh` on PATH that answers the push-path reads."""
    script = directory / "gh"
    script.write_text(STUB_GH.format(head="a" * 40, paths=list(paths)),
                      encoding="utf-8")
    script.chmod(0o755)
    return script


def _run_classifier(tmp_path, paths=None, gha_output=True):
    """Run the classifier on a push event; return the file it wrote.

    PR_NUMBER is empty and BEFORE_SHA is a real 40-hex commit, the shape
    of the refresh bot's own push (issue #540's run 37057914441).
    """
    paths = BOT_PATHS if paths is None else paths
    out = tmp_path / "step-outputs.txt"
    if gha_output:
        out.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "PATH": f"{tmp_path}:{env['PATH']}",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REPOSITORY": "Nitjsefnie/claudit",
        "GITHUB_SHA": HEAD,
        "PR_NUMBER": "",
        "BEFORE_SHA": BEFORE,
    })
    if gha_output:
        env["GITHUB_OUTPUT"] = str(out)
    else:
        env.pop("GITHUB_OUTPUT", None)
    _stub_gh(tmp_path, paths)
    subprocess.run([sys.executable, str(CLASSIFIER)], env=env,
                   capture_output=True, text=True, check=True, timeout=120)
    return out.read_text(encoding="utf-8") if gha_output else ""


def _parse_step_outputs(text):
    """The step outputs Actions reads out of a GITHUB_OUTPUT file."""
    outputs = {}
    for line in text.splitlines():
        if not line:
            continue
        name, separator, value = line.partition("=")
        assert separator, f"a step-output line without '=': {line!r}"
        outputs[name] = value
    return outputs


def _condition(job):
    """A leg's condition, unwrapped from `${{ }}`."""
    return str(job.get("if") or "").strip().removeprefix("${{").\
        removeprefix(" ").removesuffix("}}").removesuffix(" ").strip()


def _published(written, gate=None):
    """The step outputs projected through the classify job's own outputs.

    Actions publishes `steps.<id>.outputs.<name>` to the rest of the
    workflow under the job-output name the workflow declares for it, and
    a leg reads the PUBLISHED name. This projection is that hop, so a
    step-output name the workflow publishes under another name — or not
    at all — stops being visible to the legs exactly as it stops being
    visible on a runner.
    """
    gate = _ci_gate() if gate is None else gate
    classify = gate["jobs"]["classify"]
    step_ids = {step.get("id") for step in classify.get("steps") or []}
    published = {}
    for name, expression in (classify.get("outputs") or {}).items():
        step, _, rest = str(expression).partition('.outputs.')
        step_id = step.removeprefix('${{').strip().removeprefix(
            'steps.').strip()
        key = rest.removesuffix('}}').strip()
        # An expression naming a step id the job does not have resolves
        # to nothing on a runner, exactly as an unwritten output does —
        # and a text pin cannot see it, because both halves of the
        # mismatch are individually well-formed.
        published[name] = written.get(key, '') if step_id in step_ids else ''
    return published


def _published_names(gate=None):
    """The classify job's outputs paired with the step id each names."""
    gate = _ci_gate() if gate is None else gate
    return {name: str(expression).partition('.outputs.')[0].removeprefix(
        '${{').strip().removeprefix('steps.')
        for name, expression in (gate["jobs"]["classify"].get("outputs")
                                 or {}).items()}


def _output_name(conjunct):
    """The narrowing output one conjunct reads."""
    match = CONJUNCT_RE.fullmatch(conjunct.strip())
    assert match, f'not a narrowing conjunct: {conjunct!r}'
    return match.group(1)


def _runs(job, outputs):
    """Whether a leg's own condition would run it, on these outputs.

    Each conjunct is one `output != 'true'`, so the condition runs the
    leg exactly when none of the outputs it reads is the string `true`.
    """
    condition = _condition(job)
    if not condition:
        return True
    assert EVALUABLE.match(condition), \
        f'condition shape this evaluator does not translate: {condition!r}'
    names = [_output_name(part) for part in condition.split(' && ')]
    return all(outputs.get(name, '') != 'true' for name in names)


def _gating_legs(outputs, gate=None):
    """The legs whose conditions run, given the written step outputs."""
    return {name for name, job in _legs(_ci_gate()).items()
            if _runs(job, _published(outputs, gate))}


def _declared_outputs(gate):
    """The names the classify job publishes to the rest of the workflow."""
    declared = (gate["jobs"]["classify"].get("outputs") or {})
    return set(declared)


# ---------------------------------------------------------------------------
# the classifier's file, through the workflow's own names
# ---------------------------------------------------------------------------


def test_a_bot_push_runs_only_the_cheap_legs_end_to_end(tmp_path):
    # Issue #540's own run, with the two paths the refresh bot's commit
    # carries: the cheap class is lint and smoke, every other leg skips.
    outputs = _parse_step_outputs(_run_classifier(tmp_path))
    assert outputs.get("data_only") == "true", outputs
    assert _published(outputs).get("data_only") == "true", outputs
    assert _gating_legs(outputs) == {"lint", "smoke"}


def test_a_code_push_runs_every_leg_end_to_end(tmp_path):
    outputs = _parse_step_outputs(
        _run_classifier(tmp_path, paths=CODE_PATHS))
    assert outputs.get("docs_only") == "false", outputs
    assert outputs.get("data_only") == "false", outputs
    assert _gating_legs(outputs) == set(_legs(_ci_gate()))


def test_a_docs_push_skips_every_leg_end_to_end(tmp_path):
    outputs = _parse_step_outputs(
        _run_classifier(tmp_path, paths=DOC_PATHS))
    assert outputs.get("docs_only") == "true", outputs
    assert _gating_legs(outputs) == set()


def test_a_classifier_that_writes_nothing_runs_every_leg(tmp_path):
    # Fail closed, and the direction the #540 wiring failure took. A
    # GITHUB_OUTPUT the classifier never writes (an unset variable, a
    # step that dies before the write) leaves every output empty, every
    # `!= 'true'` true, and the full matrix running — never a narrowing
    # nobody decided.
    outputs = _parse_step_outputs(_run_classifier(tmp_path, gha_output=False))
    assert not outputs
    assert _gating_legs(outputs) == set(_legs(_ci_gate()))


def test_every_workflow_published_output_is_one_the_classifier_writes(tmp_path):
    # The step-output-name layer, in both directions: a workflow that
    # reads an output the step never writes narrows on nothing, and a
    # step that writes one the workflow never reads is a decision that
    # binds no job. The classifier's written file is the source of both
    # sets, so neither direction can drift from the other silently.
    written = set(_parse_step_outputs(_run_classifier(tmp_path)))
    published = _declared_outputs(_ci_gate())
    assert published <= written, published - written
    assert written == published, written - published


def test_every_narrowing_reference_names_a_written_output(tmp_path):
    # The job-output names every leg condition reads must exist in what
    # the step wrote: a typo in a condition is invisible to the text pin
    # that asserts the condition's own string, and reads as a permanent
    # full run.
    written = set(_parse_step_outputs(_run_classifier(tmp_path)))
    for name, job in _legs(_ci_gate()).items():
        for reference in REFERENCE.findall(_condition(job)):
            assert reference in written, (name, reference)


def test_every_published_output_names_a_step_the_classify_job_has():
    # The layer no text pin can reach, and the one #540's report turned
    # on. `steps.classifier.outputs.data_only` is a well-formed
    # reference with a well-formed output name, and every string pin in
    # the suite stays green on it — but no step carries the id
    # `classifier`, so the expression resolves to nothing at runtime,
    # every leg's `!= 'true'` is true, and the decision prints without
    # binding. The step id is bound to the step list here.
    step_ids = {step.get("id")
                for step in _ci_gate()["jobs"]["classify"].get("steps") or []}
    for name, step_id in _published_names().items():
        assert step_id in step_ids, (name, step_id)
