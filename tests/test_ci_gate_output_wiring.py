"""The classify step's written outputs actually gate the legs (issue #540).

The workflow-text pins judge ci-gate.yml as TEXT: that each leg carries
the narrowing `if:`, that the classify job declares `docs_only` /
`data_only`, and that the classifier writes that exact byte shape. None
of them can see the layers between the two — the classifier RUNS, writes
the step-output file Actions reads, the job's `outputs:` block publishes
those under names the legs read, and `needs: classify` is what makes
them visible at all. Every one of a step-output rename, a step id the
job does not have, a key that stops being written, an inverted
condition, or a leg that drops `needs:` passes all of them and fails the
gate: the decision prints and does not bind, and the full matrix runs on
the refresh bot's hourly push.

So this module executes the chain instead of adding more text pins. It
runs scripts/ci/classify_changes.py as a subprocess over a stub `gh`,
parses the GITHUB_OUTPUT file the way Actions does, projects it through
the classify job's `outputs:` mapping, and evaluates each leg's own
`if:` against the result — the whole chain, so the question each test
asks is which legs would actually run.

The stub logs every read it answers. `classify_changes.py` wraps each
`gh` call in `except Exception: return None`, so a stub failure is
indistinguishable from a refusal unless the test looks at the log: an
unmodelled read the classifier swallowed would otherwise read as a
correct over-run, and every test below would pass on an answer the
harness never gave it.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest
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

# The classifier checks these are 40 hex digits and nothing more
# (`_hex40`), so they are synthetic: nothing repository-managed is
# pinned here for a shape that only needs the length.
BEFORE = "b" * 40
HEAD = "a" * 40

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
# answered in the shape the jq filter would have produced. Every read is
# appended to the log named by GITHUB_STUB_LOG before it is answered,
# and an unmodelled read exits nonzero naming its url: both so a test
# can tell a refusal it asked for from one it never modelled.
STUB_GH = '''#!/usr/bin/env python3
"""A stand-in for `gh` over the reads classify_changes.py makes."""
import os
import sys

url = next((arg for arg in sys.argv[1:] if arg.startswith("repos/")), "")
with open(os.environ["GITHUB_STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(url + "\\n")
if "/runs?" in url:
    # The base the walk returns. BEFORE, never HEAD: a self-compare
    # (base == sha) is a shape the real API answers with zero files and
    # the classifier has no branch for, so the harness would be agreeing
    # with the code it is testing instead of exercising it.
    print("{head} completed success 4242")
elif "/jobs?" in url:
    print("tests success")
elif "/compare/" in url:
    for path in {paths!r}:
        print(path)
else:
    sys.exit("stub gh: unexpected read " + url)
'''


class Classified(NamedTuple):
    """One classifier run: the file the step would read, the reads behind
    it, and the classifier's own exit status."""

    written: str
    reads: list[str]
    returncode: int


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
    script.write_text(STUB_GH.format(head=BEFORE, paths=list(paths)),
                      encoding="utf-8")
    script.chmod(0o755)
    return script


def _run_classifier(tmp_path, paths=None, writes=True):
    """Run the classifier on a push event over the stub `gh`.

    PR_NUMBER is empty and BEFORE_SHA is a 40-hex commit, the shape of
    the refresh bot's own push (issue #540's run 37057914441). Returns
    the step-output file's text, the reads the stub answered, and the
    classifier's own exit status — the third is what the fail-closed
    test rests on.
    """
    if os.name == "nt":
        pytest.skip(
            "the stub gh is a POSIX script; Windows resolves an "
            "extensionless command name to .exe alone, so the "
            "classifier's `gh` never reaches it and the read log stays "
            "empty. The two workflow-only pins still run there.")
    paths = BOT_PATHS if paths is None else paths
    out = tmp_path / "step-outputs.txt"
    out.write_text("", encoding="utf-8")
    # `writes=False` points GITHUB_OUTPUT where the classifier cannot
    # open it, so the step dies at `write_outputs` — the state a runner
    # reads when the script never reached its write, rather than a state
    # this harness invented.
    target = str(out) if writes else str(tmp_path / "absent" / "out.txt")
    log = tmp_path / "stub-reads.txt"
    log.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "PATH": os.pathsep.join((str(tmp_path), env["PATH"])),
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REPOSITORY": "Nitjsefnie/claudit",
        "GITHUB_SHA": HEAD,
        "GITHUB_STUB_LOG": str(log),
        "PR_NUMBER": "",
        "BEFORE_SHA": BEFORE,
        "GITHUB_OUTPUT": target,
    })
    _stub_gh(tmp_path, paths)
    completed = subprocess.run([sys.executable, str(CLASSIFIER)], env=env,
                               capture_output=True, text=True,
                               check=writes, timeout=120)
    return Classified(out.read_text(encoding="utf-8"),
                      log.read_text(encoding="utf-8").split(),
                      completed.returncode)


# The endpoints the stub models, and only these. A read outside them
# means the classifier asked something this harness never modelled, and
# the stub's nonzero exit is swallowed by `_read`'s `except Exception` —
# it would arrive as the same fail-closed over-run the narrowing decision
# makes, and every assertion below would pass on an answer the harness
# never gave.
MODELLED_READS = ("/runs?", "/jobs?", "/compare/")


def _only_modelled_reads(run):
    """The classifier asked the stub only reads the stub models."""
    assert run.reads, "the stub was never asked a read"
    unmodelled = [url for url in run.reads
                  if not any(part in url for part in MODELLED_READS)]
    assert not unmodelled, f'the stub was asked a read it does not model: {unmodelled}'


def _over_the_verified_base(run):
    """The run read its changed paths over the verified base.

    `_read` turns a failed `gh` call into `None`, and a `None` reads as
    the same fail-closed over-run the narrowing decision itself makes —
    so this is what separates "the classifier compared and found these
    paths" from "the classifier never got an answer".
    """
    assert any("/compare/" in url for url in run.reads), \
        f'the compare read never happened: {run.reads}'


def _parse_step_outputs(text):
    """The `name=value` step outputs Actions reads out of a GITHUB_OUTPUT
    file. Actions also accepts the `name<<DELIM` heredoc form, which this
    classifier cannot emit; a heredoc body line carries no `=` and trips
    the assert below rather than being read as an output.
    """
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


def _published(written, gate=None):
    """The step outputs projected through the classify job's own outputs.

    Actions publishes `steps.<id>.outputs.<name>` to the rest of the
    workflow under the name the job's `outputs:` block declares for it,
    and a leg reads the PUBLISHED name — so this projection is the hop
    between the two name spaces. Two references resolve to nothing,
    exactly as they do on a runner: an output name the step never wrote,
    and a step id the job does not have. Both halves of that second
    mismatch are individually well-formed, which is what makes it
    invisible to a text pin.
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
        published[name] = written.get(key, '') if step_id in step_ids else ''
    return published


def _published_names(gate=None):
    """The classify job's outputs paired with the step id each names."""
    gate = _ci_gate() if gate is None else gate
    return {name: str(expression).partition('.outputs.')[0].removeprefix(
        '${{').strip().removeprefix('steps.')
        for name, expression in (gate["jobs"]["classify"].get("outputs")
                                 or {}).items()}


def _gating_legs(outputs, gate=None):
    """The legs whose conditions run, given the written step outputs."""
    return {name for name, job in _legs(_ci_gate()).items()
            if _runs(job, _published(outputs, gate))}


# ---------------------------------------------------------------------------
# the legs can see the outputs at all
# ---------------------------------------------------------------------------


def test_every_leg_needs_the_classifier():
    # A leg that drops `needs: classify` does not narrow: `classify` is
    # no longer in its needs, so `needs.classify.outputs.docs_only`
    # resolves to empty, `'' != 'true'` is true, and the leg runs on
    # every push — #540's symptom, "the decision prints and does not
    # bind", with the condition string itself untouched and every
    # `if:`-shaped pin still green. The `needs:` hop is the limb sitting
    # one line below the `if:` every other pin reads.
    for name, job in _legs(_ci_gate()).items():
        needs = job.get("needs")
        # `needs: classify` parses as the scalar, where `in` would be a
        # SUBSTRING test and a job named `pre-classify` would pass.
        assert "classify" in ([needs] if isinstance(needs, str)
                              else (needs or [])), name


# ---------------------------------------------------------------------------
# the classifier's file, through the workflow's own names
# ---------------------------------------------------------------------------


def test_a_bot_push_runs_only_the_cheap_legs_end_to_end(tmp_path):
    # Issue #540's own run, with the two paths the refresh bot's commit
    # carries: the cheap class is lint and smoke, every other leg skips.
    run = _run_classifier(tmp_path)
    _only_modelled_reads(run)
    _over_the_verified_base(run)
    outputs = _parse_step_outputs(run.written)
    assert outputs.get("data_only") == "true", outputs
    assert _published(outputs).get("data_only") == "true", outputs
    assert _gating_legs(outputs) == {"lint", "smoke"}


def test_a_code_push_runs_every_leg_end_to_end(tmp_path):
    run = _run_classifier(tmp_path, paths=CODE_PATHS)
    _only_modelled_reads(run)
    outputs = _parse_step_outputs(run.written)
    assert outputs.get("docs_only") == "false", outputs
    assert outputs.get("data_only") == "false", outputs
    # The reason carries the path count, so this pins a real read of two
    # paths rather than the shape of an answer the harness never gave:
    # `_read` turns a failed `gh` call into the same `false, false` the
    # two assertions above accept.
    _over_the_verified_base(run)
    assert outputs.get("reason", "").startswith("2 paths changed, 2 outside")
    assert _gating_legs(outputs) == set(_legs(_ci_gate()))


def test_a_docs_push_skips_every_leg_end_to_end(tmp_path):
    run = _run_classifier(tmp_path, paths=DOC_PATHS)
    _only_modelled_reads(run)
    _over_the_verified_base(run)
    outputs = _parse_step_outputs(run.written)
    assert outputs.get("docs_only") == "true", outputs
    assert _gating_legs(outputs) == set()


def test_a_classifier_that_writes_nothing_runs_every_leg(tmp_path):
    # Fail closed, and the direction the #540 wiring failure took. The
    # step-output file is pre-created and the classifier dies before it
    # writes a byte, so the emptiness asserted here is the script's and
    # not the harness's — the nonzero exit proves the script ran and
    # died. Every output empty, every `!= 'true'` true, and the full
    # matrix running: never a narrowing nobody decided. A classify JOB
    # that fails outright is the other fail-closed shape and is not
    # modelled here; Actions skips everything needing a failed job, a
    # different outcome again.
    run = _run_classifier(tmp_path, writes=False)
    _only_modelled_reads(run)
    _over_the_verified_base(run)
    assert run.returncode != 0, "the classifier was supposed to die"
    assert run.written == ""
    assert _gating_legs({}) == set(_legs(_ci_gate()))


def test_every_workflow_published_output_is_one_the_classifier_writes(tmp_path):
    # The two name spaces in both directions, which is what keeps them
    # identical: a workflow reading an output the step never writes
    # narrows on nothing, and a step writing one the workflow never
    # reads is a decision that binds no job. Equality — not containment
    # in either direction — is the pin.
    run = _run_classifier(tmp_path)
    _only_modelled_reads(run)
    written = set(_parse_step_outputs(run.written))
    published = set(_ci_gate()["jobs"]["classify"].get("outputs") or {})
    assert published == written, published ^ written


def test_every_narrowing_reference_names_a_written_output(tmp_path):
    # The job-output names every leg condition reads must exist in what
    # the step wrote: a typo in a condition is invisible to the text pin
    # that asserts the condition's own string, and reads as a permanent
    # full run.
    run = _run_classifier(tmp_path)
    _only_modelled_reads(run)
    written = set(_parse_step_outputs(run.written))
    for name, job in _legs(_ci_gate()).items():
        references = REFERENCE.findall(_condition(job))
        # A condition naming no narrowing output narrows nothing at all,
        # which an absent reference must not read as agreement.
        assert references, name
        for reference in references:
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
