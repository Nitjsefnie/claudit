#!/usr/bin/env python3
"""Refuse a commit subject outside CONTRIBUTING.md's format, or one whose
scope names a workflow under a type other than `ci`.

Both rules live in CONTRIBUTING.md (Commit subjects). A subject reads
`type(scope)!: subject`: a type from TYPES, an optional scope (no
parenthesis, no whitespace), an optional `!` right before the colon, then
`: ` and a description. A workflow's `name:` may be a scope only with
type `ci` — `ci(tests)` changes the workflow, `fix(tests)` the suite.

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

Only the OUTGOING range origin/master..HEAD is examined. A subject git
or GitHub writes, which nobody authors, is listed and never examined; the
closed set (WRITTEN_BY_GIT): `Merge <40 hex> into <40 hex>` (GitHub's
pull-request merge), `Merge branch '<b>'` (git's optional ` of <url>`
and ` into <b>`), `Merge remote-tracking branch '<b>'` (optional
` into <b>`), `Merge pull request #<n> from <head>`, `Revert "<s>"` and
`Reapply "<s>"` (git reverting a revert). `fixup!`/`squash!`/`amend!`
and GitHub's editable defaults (`Apply suggestions from code review`)
are refused by choice: a rebase merge would land them on master.

WHAT IT CANNOT SEE, and says so rather than guessing:

  - A `name:` line this reader cannot read — quoted, anchored, a block
    scalar, a trailing comment, continued onto the next line, or missing
    entirely. Each is a refusal naming the file: an incomplete set is a
    green over a gate that was never checked.
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


# CONTRIBUTING.md's eleven types; thresholds_guard.py reads them too.
TYPES = frozenset(("build", "chore", "ci", "docs", "feat", "fix", "perf",
                   "refactor", "revert", "style", "test"))

# The type is any run of letters, so an unknown one is named; TYPES judges it.
SUBJECT = re.compile(r"(?P<type>[A-Za-z]+)(?:\((?P<scope>[^()\s]+)\))?"
                     r"!?: \S.*")

WRITTEN_BY_GIT = tuple(re.compile(shape) for shape in (
    r"Merge [0-9a-f]{40} into [0-9a-f]{40}",
    r"Merge branch '[^']+'(?: of \S+)?(?: into \S+)?",
    r"Merge remote-tracking branch '[^']+'(?: into \S+)?",
    r"Merge pull request #[0-9]+ from \S+",
    r'Revert ".+"',
    r'Reapply ".+"',
))


def written_by_git(subject: str) -> bool:
    return any(shape.fullmatch(subject) for shape in WRITTEN_BY_GIT)


def conforming(subject: str) -> re.Match[str] | None:
    """The subject's parts when it is in CONTRIBUTING's format, else None."""
    match = SUBJECT.fullmatch(subject)
    return match if match is not None and match["type"] in TYPES else None


def refusal(subject: str, names: set[str]) -> str | None:
    """What refuses an authored subject, or None when it passes."""
    match = conforming(subject)
    if match is None:
        shaped = SUBJECT.fullmatch(subject)
        if shaped is None:
            return "does not read as `type(scope)!: subject`."
        return (f"type `{shaped['type']}` is not one of "
                f"{', '.join(sorted(TYPES))}.")
    scope, commit_type = match["scope"], match["type"]
    # `tests` is exempt: CONTRIBUTING gives `fix(tests)` to the suite.
    if (scope in names and commit_type != "ci"
            and scope != SHARED_NAME_SCOPE):
        return (f"scope `{scope}` is the name of a workflow under "
                f"{WORKFLOW_DIR}/; type `{commit_type}` is not `ci`.")
    return None


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
        commits.append((sha.strip(), subject))
    return commits


def check(root: Path, out=print) -> int:
    fetch_base(root)
    names = workflow_name_set(root)
    commits = outgoing_commits(root)

    unexamined: list[tuple[str, str]] = []
    refused: list[tuple[str, str, str]] = []
    for sha, subject in commits:
        if written_by_git(subject):
            unexamined.append((sha, subject))
        elif (reason := refusal(subject, names)) is not None:
            refused.append((sha, subject, reason))
    examined = len(commits) - len(unexamined)

    if refused:
        plural = "s" if len(refused) != 1 else ""
        out(f"{len(refused)} commit subject{plural} in "
            f"origin/{BASE_BRANCH}..HEAD refused:")
        for sha, subject, reason in refused:
            out(f"  {sha} {subject}")
            out(f"    {reason}")
        out("Write each as `type(scope)!: subject` (CONTRIBUTING.md, Commit "
            f"subjects): the type one of {', '.join(sorted(TYPES))}; the "
            "scope and `!` optional; a workflow's name as the scope only "
            "with the `ci` type.")
        return 1

    plural = "s" if examined != 1 else ""
    out(f"Examined {examined} commit subject{plural} in "
        f"origin/{BASE_BRANCH}..HEAD; {len(unexamined)} written by git or "
        f"GitHub, not examined{':' if unexamined else '.'}")
    for sha, subject in unexamined:
        out(f"    {sha} {subject}")
    out("  History already merged into master is never re-judged.")
    out("Every examined subject reads as `type(scope)!: subject` with a "
        "listed type, and none pairs a workflow-name scope with a type "
        "other than `ci`.")
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
    except GateError as unanswered:
        print(f"commit scopes: {unanswered}", file=sys.stderr)
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
