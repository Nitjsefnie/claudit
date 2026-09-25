"""One owner per pip requirements file (dependabot.yml regression pins).

The property pinned here: every pip requirements file tracked in this
repository has EXACTLY ONE owner — one entry in `.github/dependabot.yml`
whose coverage reaches it.

Dependabot's pip file fetcher does not stop at the configured directory.
In dependabot-core, `python/lib/dependabot/python/shared_file_fetcher.rb`,
`req_txt_and_in_files` (a) fetches every `*.txt`/`*.in` file in the
configured directory that passes `requirements_file?`, and (b) for EVERY
subdirectory listed in the configured directory's contents, fetches the
requirement files directly inside it (`req_files_for_dir`) — a one-level
walk, no deeper. From `directory: "/"` that walk reaches
`backend/requirements.txt`, so a root pip entry's coverage is a SUPERSET
of any subdirectory entry's:
https://github.com/dependabot/dependabot-core/blob/e3f36c58b9c9a9d8ea281cb58f3f9cfcaaa9e0d1/python/lib/dependabot/python/shared_file_fetcher.rb#L165-L200

Two pip entries whose coverage overlaps therefore double-own a file, and
each bump of it opens twice. That is exactly what happened with boto3:
PR #164 (`dependabot/pip/backend/boto3-1.43.99`, from the `/backend`
entry) and PR #165 (`dependabot/pip/boto3-1.43.99`, from the root entry),
opened 16 seconds apart, #165 closed as the duplicate (issue #176).

The parser below is a line-based reader for THIS file's pinned shape —
stdlib only, no PyYAML (not a dependency) — and fails loudly on any line
it does not recognise rather than silently returning no entries.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / ".github" / "dependabot.yml"

# The files issue #176 is about. Discovery is a `git ls-files` glob, so
# this equality also proves discovery did not silently come back empty.
EXPECTED_REQUIREMENTS_FILES = {
    "requirements-dev.txt",
    "requirements-test.txt",
    "backend/requirements.txt",
}

# An updates entry: `  - package-ecosystem: pip` — exactly two spaces, the
# dash, then the first inline key of the entry.
_ENTRY_RE = re.compile(r"^  - ([A-Za-z][A-Za-z0-9-]*): (.+)$")
# An entry-level inline key: exactly four spaces, `key: value`. Nested
# block content sits at six spaces and never matches.
_KEY_RE = re.compile(r"^    ([A-Za-z][A-Za-z0-9-]*): (.+)$")
# A pip `-r`/`-c` style include: `-r other.txt`, `--requirement=other.txt`,
# `-c constraints.txt`, `-rfile.txt`, ...
_INCLUDE_RE = re.compile(
    r"^\s*(?:--requirement|-r|--constraint|-c)[= ]\s*(\S+)"
    r"|^\s*(?:-r|-c)(\S+)"
)


def _unquote(value: str) -> str:
    """Strip one matching pair of YAML quotes from an inline scalar."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _header_event(stripped: str) -> str:
    """Classify a top-level line; raise when it is neither pinned key."""
    if stripped == "version: 2":
        return "version"
    if stripped == "updates:":
        return "updates"
    raise AssertionError(
        f"{CONFIG_PATH}: unexpected top-level line {stripped!r}; the "
        "parser pins the shape `version: 2` + `updates:`")


def _is_noise(raw: str, stripped: str) -> bool:
    """A line the pinned shape does not act on.

    Blank lines and comments anywhere; nested block content (six-space
    indent: `schedule:`, `labels:` lists, ...) and bare entry-level block
    keys (`    schedule:` with its content nested) inside an entry.
    """
    if not stripped or stripped.startswith("#"):
        return True
    if raw.startswith("      "):
        return True
    return bool(re.match(r"^    ([A-Za-z][A-Za-z0-9-]*):$", raw.rstrip()))


def _read_entries() -> list[dict[str, str]]:
    """(package-ecosystem, directory, ...) per updates entry, in file order.

    Fails loudly rather than returning nothing when the file's shape
    departs from what this reader understands.
    """
    text = CONFIG_PATH.read_text(encoding="utf-8")
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    saw_version = saw_updates = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if _is_noise(raw, stripped):
            continue
        if not saw_updates:
            # Top level: the two keys the file pins, nothing else.
            event = _header_event(stripped)
            saw_version = saw_version or event == "version"
            saw_updates = saw_updates or event == "updates"
            continue
        match = _ENTRY_RE.match(raw.rstrip())
        if match:
            current = {"package-ecosystem": _unquote(match.group(2))}
            entries.append(current)
            continue
        match = _KEY_RE.match(raw.rstrip())
        if match:
            if current is None:
                raise AssertionError(
                    f"{CONFIG_PATH}: entry-level key {match.group(1)!r} "
                    "outside any entry")
            current[match.group(1)] = _unquote(match.group(2))
            continue
        raise AssertionError(
            f"{CONFIG_PATH}: line outside the pinned shape: {raw!r}")
    _validate_shape(entries, saw_version, saw_updates)
    return entries


def _validate_shape(entries: list[dict[str, str]],
                    saw_version: bool, saw_updates: bool) -> None:
    """The reader must have seen the pinned header and complete entries."""
    if not (saw_version and saw_updates and entries):
        raise AssertionError(
            f"{CONFIG_PATH}: expected `version: 2`, `updates:` and at "
            f"least one entry; saw version={saw_version}, "
            f"updates={saw_updates}, entries={len(entries)}")
    missing = [e["package-ecosystem"] for e in entries
               if not e.get("directory")]
    if missing:
        raise AssertionError(
            f"{CONFIG_PATH}: entries without a directory: {missing}")


def _tracked_requirements() -> list[str]:
    """Tracked requirements files, the fetcher's own name-shaped targets."""
    result = subprocess.run(
        ["git", "ls-files", "*requirements*.txt", "*requirements*.in"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return sorted(result.stdout.split())


def _tracked_files() -> list[str]:
    """Every tracked path, the directory walk's raw material."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT, capture_output=True, check=True)
    return sorted(line for line in result.stdout.decode("utf-8").split("\0")
                  if line)


def _is_requirements_file(path: Path) -> bool:
    """The fetcher's `requirements_file?` predicate, loosely.

    A file whose NAME contains `requirements` is always one (the
    fetcher's always-true branch). Anything else must be all blank,
    comment, flag (`-r ...`, `--index-url ...`) or `name==version` pin
    lines — loose enough for constraints files, tight enough to keep
    unrelated `.txt` files out without emulating the real parser.
    """
    if "requirements" in path.name:
        return True
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        if not re.match(r"^[A-Za-z0-9._\[\]-]+\s*==\s*\S+$", line):
            return False
    return True


def _entry_dir(directory: str) -> str:
    """The entry's directory as a `/`-relative path (`"/"` -> `""`)."""
    trimmed = directory.strip()
    if not trimmed.startswith("/"):
        raise AssertionError(
            f"{CONFIG_PATH}: directory {directory!r} is not absolute; "
            "dependabot directories are repo-relative and start with `/`")
    return trimmed.strip("/")


def _walk_coverage(tracked: list[str], directory: str) -> set[str]:
    """Tracked requirement files at the entry's directory, one level down.

    Mirrors the fetcher: (a) requirement files directly in the configured
    directory, (b) requirement files directly inside each FIRST-LEVEL
    child directory — no deeper — over the tracked `*.txt`/`*.in` files,
    gated by the name/content predicate.
    """
    entry_dir = _entry_dir(directory)
    covered: set[str] = set()
    for path in tracked:
        if not path.endswith((".txt", ".in")):
            continue
        parts = path.split("/")
        parent = "/".join(parts[:-1])
        grandparent = "/".join(parts[:-2])
        if entry_dir not in (parent, grandparent):
            continue
        if _is_requirements_file(REPO_ROOT / path):
            covered.add(path)
    return covered


def _include_targets(including: str) -> list[str]:
    """Tracked files an `-r`/`-c` line names, relative to the includer."""
    try:
        text = (REPO_ROOT / including).read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return []
    targets: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _INCLUDE_RE.match(line)
        if not match:
            continue
        named = next(group for group in match.groups() if group)
        resolved = (REPO_ROOT / including).parent / named
        try:
            relative = resolved.resolve().relative_to(
                REPO_ROOT.resolve())
        except ValueError:
            continue  # an include escaping the repo names nothing here
        targets.append(relative.as_posix())
    return targets


def _entry_coverage(tracked: list[str], directory: str) -> set[str]:
    """Everything one pip entry would fetch, includes followed.

    (a)+(b) the directory walk, then (c) `-r`/`--requirement`/`-c`/
    `--constraint` includes inside any fetched file, resolved relative to
    the including file, recursively. Only tracked paths are reported.
    """
    covered = _walk_coverage(tracked, directory)
    queue = sorted(covered)
    seen: set[str] = set()
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        for target in _include_targets(path):
            if target in tracked and target not in covered:
                covered.add(target)
                queue.append(target)
    return covered


def _pip_coverage_owners() -> dict[str, list[str]]:
    """Requirements file -> owning pip entries, over every pip entry."""
    tracked = _tracked_files()
    owners: dict[str, list[str]] = {}
    for entry in _read_entries():
        if entry["package-ecosystem"] != "pip":
            continue
        label = f"pip \"{entry['directory']}\""
        for path in _entry_coverage(tracked, entry["directory"]):
            owners.setdefault(path, []).append(label)
    return owners


def test_no_requirements_file_has_two_owners():
    """THE regression test: no file may sit under two pip entries.

    Failed before the fix: the `/` and `/backend` entries both resolved
    backend/requirements.txt, which is how boto3's bump opened twice
    (#164 from /backend, #165 from the root, 16 seconds apart).
    """
    owners = _pip_coverage_owners()
    duplicates = {path: labels for path, labels in owners.items()
                  if len(labels) > 1}
    assert not duplicates, (
        "requirements files with more than one Dependabot owner — the "
        "second entry opens a duplicate PR for every bump: "
        + "; ".join(f"{path}: owned by {' AND '.join(labels)}"
                    for path, labels in sorted(duplicates.items())))


def test_every_requirements_file_is_covered():
    """The surviving entries together must still reach every file.

    Removing an overlapping entry must not orphan one: the union of the
    pip entries' coverage is a superset of the tracked requirements
    files. Also pins discovery against coming back empty by asserting
    the exact expected set.
    """
    requirements = _tracked_requirements()
    assert set(requirements) == EXPECTED_REQUIREMENTS_FILES, (
        f"git ls-files discovery returned {sorted(requirements)}, not the "
        "expected three files — the globs stopped matching (or a fourth "
        "requirements file arrived and this test needs its expectation "
        "extended)")
    covered: set[str] = set()
    for entry in _read_entries():
        if entry["package-ecosystem"] == "pip":
            covered |= _entry_coverage(_tracked_files(), entry["directory"])
    uncovered = sorted(set(requirements) - covered)
    assert not uncovered, (
        f"no Dependabot pip entry covers: {uncovered}")
