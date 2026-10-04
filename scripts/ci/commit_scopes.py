#!/usr/bin/env python3
"""Refuse a commit subject whose scope names a workflow but whose type is
not `ci` (issue #556).

The claim and pr-gate action repositories run this check on every pull
request; this repository did not. A commit scope is prose no gate read,
so the convention drifted: two merged `fix(tests)` commits use the tests
workflow's name as a scope for suite fixes, which is exactly the
ambiguity the shared-name rule exists to settle. The rule the check
reads lives in CONTRIBUTING.md (Commit subjects): a workflow's `name:`
may be a scope only with type `ci` — `ci(tests)` changes the workflow,
`fix(tests)` fixes the suite.

The workflow-name set is DERIVED at HEAD from the tracked files under
.github/workflows, not kept in a list here. A remembered list is only as
current as the last time somebody remembered to add to it, and a
workflow renamed out of the list would take its scope out of the rule in
silence. The value that joins the set is the top-level `name:` in the
YAML — the thing the rule tells a contributor to use — never the
filename: a file naming itself differently still contributes its `name:`,
and a job- or step-level `name:` is indented and must not be collected.

The tree itself is enumerated NUL-separated (`ls-tree -z`) and every
path is decoded byte-exact, so the set reads the bytes HEAD tracks:
under the default core.quotePath a newline-separated enumeration takes
git's C-quoted display form for the path, the quoted entry misses the
workflow-file filter, and the workflow silently leaves the set — a false
green this gate exists not to give.

Only the OUTGOING range origin/master..HEAD is examined. Already-merged
history is what the check exists to stop repeating, not what it
re-judges: the two fix(tests) commits above predate the rule and are the
reason the `tests` scope is exempt by name rather than a violation to
convert.

WHAT IT CANNOT SEE, and says so rather than guessing:

  - A subject that does not parse — a merge commit, a non-conforming
    subject, anything without a lowercase type, a scope in parens and a
    colon. It is not a failure: it is counted and listed on stdout, so a
    green is never silent about what it skipped. Parsing it anyway would
    put a guessed scope under a gate it never agreed to.
  - A `name:` line this reader cannot read — quoted, anchored, a block
    scalar, a trailing comment, continued onto the next line, or missing
    entirely. Each is a refusal naming the file: an incomplete set is a
    green over a gate that was never checked.
  - The type table's other half. A type outside the convention is a
    defect under any scope, so no type is enumerated here: every type
    but `ci` violates when the scope names a workflow, and the scope
    rule is what fires.
  - A workflow `name:` containing whitespace can never be matched by a
    scope: the subject grammar's scope excludes spaces, so `ci gate` and
    the other multi-word names are structurally unreachable.
  - `tests`, which is both a workflow name and the suite's own
    directory, and is therefore exempt from the type rule in the
    direction CONTRIBUTING gives for it — `fix(tests)` fixes the suite.
    The exemption is one scope wide; a second shared name needs its own
    sentence in CONTRIBUTING before it gets its own clause here.

Runs on the standard library alone, and reads blobs with
`git cat-file blob HEAD:<path>` because in CI the working tree and HEAD
are the same commit, while at review time the tree somebody is editing
is not the tree any gate has agreed to judge.

    scripts/ci/commit_scopes.py [--root DIR]

--root points the check at a repository other than the one above
scripts/ci, which is how the suite rehearses it against real fixtures.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# The branch a pull request's head is compared against. Not configurable:
# a second branch here would be a second base, and this question has one.
BASE_BRANCH = "master"

WORKFLOW_DIR = ".github/workflows"

# The one scope that is both a workflow name and a subject of its own.
# Documented in CONTRIBUTING.md (Commit subjects); a second shared name
# needs its own sentence there before it gets a clause here.
SHARED_NAME_SCOPE = "tests"


class GateError(Exception):
    """This run could not establish the answer, and says so instead."""


def git(root: Path, *arguments: str, what: str) -> str:
    """Run one git command in `root` and return its stdout.

    Every call goes through here because a guard that reads its own
    error as a clean tree is the false green this exists to prevent: a
    non-zero status is a refusal naming what was being attempted, never
    an empty answer. stdout and stderr decode with surrogateescape so a
    path git prints raw survives as the string it is.
    """
    command = ("git", "-C", str(root)) + arguments
    done = subprocess.run(command, capture_output=True, text=True,
                          errors="surrogateescape", check=False)
    if done.returncode != 0:
        detail = done.stderr.strip() or "no output"
        raise GateError(
            f"cannot {what}: `{' '.join(command)}` exited "
            f"{done.returncode}: {detail}")
    return done.stdout


# --- reading the workflow names ----------------------------------------------
#
# The subset of YAML these workflows use for the one key this check
# reads: a plain block mapping entry at column 0. Every other shape is
# refused rather than guessed at, because a guessed name is either a
# scope the rule should have caught or a refusal nobody asked for.

MAPPING = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_.-]*):(?:[ \t]+(?P<value>.*))?$")

# The first characters that say a scalar is NOT plain: a quoted, anchored,
# aliased, tagged, block or flow value. A plain scalar starts with none of
# them, and reading one as if it were plain is how `name: "tests"` would
# enter the set carrying its quotes.
NOT_PLAIN = re.compile(r"""["'|>&*![\]{}]""")

# In a plain scalar a space before `#` starts a comment, so the value
# would end there — a fact this reader refuses to model rather than
# half-models by stripping, because a stripped comment and a name
# containing a space-hash are indistinguishable at this line's
# granularity.
TRAILING_COMMENT = re.compile(r"\s#")


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def skippable(line: str) -> bool:
    """A blank line or a whole-line comment, which carries no node."""
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def tracked_files(root: Path) -> tuple[str, ...]:
    # -z keeps the entries NUL-separated and raw, decoded byte-exact by
    # git() (see its docstring): the raw bytes ARE the path.
    return tuple(f for f in
                 git(root, "ls-tree", "-z", "-r", "--name-only",
                     "--full-tree", "HEAD",
                     what="list the tracked tree").split("\0") if f)


def workflow_files(files: tuple[str, ...]) -> list[str]:
    return sorted(f for f in files
                  if f.startswith(WORKFLOW_DIR + "/")
                  and (f.endswith(".yml") or f.endswith(".yaml")))


def refuse_name(workflow: str, reason: str) -> None:
    raise GateError(
        f"{workflow}: {reason} The workflow-name set would be incomplete, "
        f"and an incomplete set is a green over a gate that was never "
        f"checked: spell the name as a plain top-level `name:` line, or "
        f"teach this check the shape.")


def workflow_name(workflow: str, text: str) -> str:
    """The single top-level `name:` value of one workflow.

    Only column 0 is read: a job's or a step's `name:` is indented and
    is not a workflow name, and collecting it would put a job's display
    name under the ci-type rule where the rule never put it.
    """
    lines = text.splitlines()
    tops = [index for index, line in enumerate(lines)
            if not skippable(line) and indent_of(line) == 0
            and (header := MAPPING.match(line)) is not None
            and header["key"] == "name"]
    if len(tops) == 0:
        refuse_name(
            workflow,
            "has no top-level `name:` this reader can read, so the "
            "workflow it defines is unnamed")
    if len(tops) != 1:
        raise GateError(
            f"{workflow} has {len(tops)} top-level `name:` mappings this "
            f"reader can read, and which one names the workflow cannot "
            f"be established")
    at = tops[0]
    header = MAPPING.match(lines[at])
    value = ((header["value"] if header is not None else "") or "").strip()
    if not value:
        refuse_name(workflow,
                    f"the top-level `name:` at line {at + 1} carries no value")
    if NOT_PLAIN.match(value[0]):
        refuse_name(
            workflow,
            f"the top-level `name:` at line {at + 1} is {value!r}, which "
            f"is not a plain scalar")
    if TRAILING_COMMENT.search(value):
        refuse_name(
            workflow,
            f"the top-level `name:` at line {at + 1} carries a trailing "
            f"comment, and where the name ends cannot be read at this "
            f"line's granularity")
    for line in lines[at + 1:]:
        if skippable(line):
            continue
        if indent_of(line) > 0:
            refuse_name(
                workflow,
                f"the top-level `name:` at line {at + 1} continues onto "
                f"an indented line")
        break
    return value


def workflow_name_set(root: Path) -> set[str]:
    """Every workflow name, derived from HEAD's workflows and nothing else."""
    files = tracked_files(root)
    if not files:
        raise GateError("HEAD tracks no files, so there are no workflows to read")
    names = set()
    for workflow in workflow_files(files):
        names.add(workflow_name(
            workflow,
            git(root, "cat-file", "blob", f"HEAD:{workflow}",
                what=f"read {workflow}")))
    if not names:
        raise GateError(
            f"no workflow under {WORKFLOW_DIR}/ carries a readable "
            f"`name:`, so the workflow-name set is empty and no subject "
            f"can be judged")
    return names


# --- the subjects -------------------------------------------------------------


def fetch_base(root: Path) -> None:
    """Bring master in as it is NOW, not as the checkout left it.

    actions/checkout fetches one commit of one ref by default, so a
    master that moved after that run left nothing here to compare
    against. Anonymous on purpose: every checkout in this workflow sets
    persist-credentials: false and the repository is public.
    """
    arguments = ["fetch", "--no-tags", "--quiet"]
    shallow = git(root, "rev-parse", "--is-shallow-repository",
                  what="ask whether the checkout is shallow").strip()
    if shallow == "true":
        arguments.append("--unshallow")
    arguments += ["origin",
                  f"+refs/heads/{BASE_BRANCH}:refs/remotes/origin/{BASE_BRANCH}"]
    git(root, *arguments, what=f"fetch origin/{BASE_BRANCH}")


# A Conventional-Commits-shaped subject: a lowercase type, an optional
# breaking marker after the type and/or after the scope's closing paren,
# a scope of non-parenthesis non-whitespace inside parens, and the colon
# the rule requires. A type outside the convention is not this regex's
# problem — the rule below flags any non-ci type on a workflow-name
# scope, and a bad type is a defect under any scope.
SUBJECT = re.compile(
    r"^(?P<type>[a-z]+)!?\((?P<scope>[^()\s]+)\)!?:\s?(?P<summary>\S.*)?$")


def outgoing_commits(root: Path) -> list[tuple[str, str]]:
    """[(sha, subject)] for exactly the commits this head asks master to take."""
    listing = git(root, "log", "--format=%H%x09%s",
                  f"origin/{BASE_BRANCH}..HEAD",
                  what="list the outgoing commits")
    commits: list[tuple[str, str]] = []
    for line in listing.split("\n"):
        if not line.strip():
            continue
        sha, _, subject = line.partition("\t")
        commits.append((sha.strip(), subject.strip()))
    return commits


def check(root: Path, out=print) -> int:
    fetch_base(root)
    names = workflow_name_set(root)
    commits = outgoing_commits(root)

    examined = 0
    unexamined: list[tuple[str, str]] = []
    violations: list[tuple[str, str, str, str]] = []
    for sha, subject in commits:
        match = SUBJECT.match(subject)
        if match is None:
            # Not a failure — a merge commit or a non-conforming subject
            # has no scope to read, and inventing one would gate a
            # contributor on a parse they never wrote. Counted and
            # listed, so a green never rides silently over what it
            # skipped.
            unexamined.append((sha, subject))
            continue
        examined += 1
        scope, commit_type = match["scope"], match["type"]
        # The shared-name exemption: `tests` is both a workflow name and
        # the suite's own directory, and CONTRIBUTING gives its
        # direction — `fix(tests)` fixes the suite, `ci(tests)` changes
        # the workflow. No other scope is exempt.
        if (scope in names and commit_type != "ci"
                and scope != SHARED_NAME_SCOPE):
            violations.append((sha, subject, scope, commit_type))

    if violations:
        plural = "s" if len(violations) != 1 else ""
        verb = "pair" if len(violations) != 1 else "pairs"
        out(f"{len(violations)} commit{plural} in "
            f"origin/{BASE_BRANCH}..HEAD {verb} a workflow-name scope "
            f"with a type other than `ci`:")
        for sha, subject, scope, commit_type in violations:
            out(f"  {sha} {subject}")
            out(f"    scope `{scope}` is the name of a workflow under "
                f"{WORKFLOW_DIR}/; type `{commit_type}` is not `ci`.")
        out("A workflow's name is a scope only with the `ci` type: use "
            "the `ci` type on that scope, or take a scope that names "
            "what the commit is actually about.")
        return 1

    plural = "s" if examined != 1 else ""
    out(f"Examined {examined} commit subject{plural} in "
        f"origin/{BASE_BRANCH}..HEAD; {len(unexamined)} not examined.")
    # The green line states its own reach: only the outgoing range is
    # judged, and a subject without a readable scope is listed rather
    # than failed. A green line that claimed more than this would be the
    # same over-claim as a report over its evidence.
    out("  Only the OUTGOING range is examined: history already merged")
    out("  into master is never re-judged, and a subject that does not")
    out("  parse (a merge commit, a non-conforming subject) is listed")
    out("  below and is never a failure:")
    for sha, subject in unexamined:
        out(f"    {sha} {subject}")
    out("No commit pairs a workflow-name scope with a type other than `ci`.")
    return 0


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent.parent
    rest = list(argv[1:])
    while rest:
        argument = rest.pop(0)
        if argument == "--root":
            if not rest:
                print("usage: commit_scopes.py [--root DIR]", file=sys.stderr)
                return 2
            root = Path(rest.pop(0))
        else:
            print(f"unknown argument: {argument}", file=sys.stderr)
            print("usage: commit_scopes.py [--root DIR]", file=sys.stderr)
            return 2
    try:
        return check(root)
    except GateError as refusal:
        print(f"commit scopes: {refusal}", file=sys.stderr)
        return 1
    except OSError as failure:
        # git could not be run at all — absent from the runner image, or
        # an argument list past what the kernel will exec. Named rather
        # than traced: the exit status is the same either way, and a
        # refusal is what this run owes the reader.
        print(f"commit scopes: cannot run git: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
