"""Pin the three docs' application-database setup blocks together.

The setup is written out three times -- README "Quick start",
CONTRIBUTING "Getting it running" and AGENTS "Build and test commands"
-- and nothing failed when they drifted: CONTRIBUTING once shipped
without the DATABASE_URL_AUTH env-edit mention (issue #464). Like
test_panel_wiring.py, these are source-level pins: nothing here runs
the commands or touches a database, and nothing else in the suite reads
these docs, so a diverging edit passes every other test.

Three guards:

1. Inventory floor. Each doc's setup block, independently, carries the
   two load-bearing auth-DB steps (`createdb claudit_auth` and the
   `CREATE TABLE users` psql step). Equality across docs alone would
   stay green if all three dropped the same step together.
2. Env-edit mentions. Each doc's section text names both
   DATABASE_URL_VIZ and DATABASE_URL_AUTH -- the historical drift was
   exactly this omission in one doc.
3. Cross-doc equality. The three docs' normalized steps are equal as
   ORDERED lists, so an omission, an addition or a reorder in any
   direction fails, and the message prints every doc's list so the
   drifted doc is named.

Section boundaries: each section runs from its `## <heading>` line to
the next `^## ` heading (a `###` subheading does not end it). Inside a
section, the setup block is the fenced block carrying the server-start
line -- starting the server is the sequence's last step -- so follow-on
blocks AFTER it are out of scope (CONTRIBUTING's file:// R2 fallback,
AGENTS's admin-ingest curl). Today no section has a command block
before the setup block.

Known CI blind spot, accepted for this pin: a docs-only push skips
every ci-gate leg (the tests leg included), so the drift push itself
stays green and the pin fires on the next code-touching run.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (doc name, path, exact heading as merged by PR #462/#465).
_DOCS: list[tuple[str, Path, str]] = [
    ("README.md", ROOT / "README.md", "Quick start"),
    ("CONTRIBUTING.md", ROOT / "CONTRIBUTING.md", "Getting it running"),
    ("AGENTS.md", ROOT / "AGENTS.md", "Build and test commands"),
]

# The server-start line: the setup sequence's last step. The setup block
# is the fenced block containing it; blocks after it are follow-on
# hints, not setup steps.
SERVER_START = "python3 -m uvicorn"


def _section(path: Path, heading: str) -> str:
    """The doc's text from `## <heading>` to the next same-level heading.

    `^## ` does not match a `###` subheading, so subsections stay inside
    their parent section.
    """
    text = path.read_text(encoding="utf-8")
    head = re.search(rf"^## {re.escape(heading)}\s*$", text, re.M)
    assert head, (
        f"{path.name}: no `## {heading}` heading -- the section moved or "
        f"was renamed; relocate this pin with it")
    start = head.end()
    nxt = re.search(r"^## ", text[start:], re.M)
    return text[start:start + nxt.start()] if nxt else text[start:]


def _fenced_blocks(section: str) -> list[str]:
    """The section's fenced code blocks, in document order."""
    blocks: list[str] = []
    body: list[str] | None = None
    for line in section.splitlines():
        if line.lstrip().startswith("```"):
            if body is not None:
                blocks.append("\n".join(body))
                body = None
            else:
                body = []
        elif body is not None:
            body.append(line)
    assert body is None, "the section ends inside an unterminated code fence"
    return blocks


def _normalize(block: str) -> list[str]:
    """A block's commands as comparable one-line strings.

    Joins backslash continuations, drops blank and pure-comment lines,
    strips a trailing `# ...` inline comment (per-doc commentary that
    legitimately differs: `# edit:` vs `# then edit:`, AGENTS's note on
    schema.sql), collapses whitespace runs to one space, and strips
    trailing whitespace.
    """
    joined: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if joined and joined[-1].endswith("\\"):
            joined[-1] = joined[-1][:-1].rstrip() + " " + line
        else:
            joined.append(line)
    steps: list[str] = []
    for line in joined:
        if not line or line.startswith("#"):
            continue
        command = re.sub(r"\s+", " ", line.split(" #", 1)[0]).strip()
        if command:
            steps.append(command)
    return steps


def _setup_steps(path: Path, heading: str) -> list[str]:
    """The doc's normalized setup steps (see SERVER_START for the block
    boundary)."""
    for block in _fenced_blocks(_section(path, heading)):
        if SERVER_START in block:
            return _normalize(block)
    raise AssertionError(
        f"{path.name}: no fenced block containing {SERVER_START!r} under "
        f"`## {heading}` -- the setup block moved or was rewritten; "
        f"relocate this pin with it")


def test_each_doc_carries_the_auth_db_steps():
    """Inventory floor, per doc independently: without these two steps a
    fresh `users` table never exists and startup's schema_check aborts,
    however well the three docs agree with each other."""
    for name, path, heading in _DOCS:
        steps = _setup_steps(path, heading)
        assert "createdb claudit_auth" in steps, (
            f"{name}: the createdb claudit_auth step is gone from the "
            f"setup block")
        assert any("CREATE TABLE users" in s for s in steps), (
            f"{name}: the CREATE TABLE users step is gone from the "
            f"setup block")


def test_each_doc_names_both_env_vars_in_its_setup_section():
    """Env-edit mentions, per doc independently. The setup block's cp
    line tells the reader which env values to edit; a doc whose section
    stopped naming one of them is the exact drift this issue closed."""
    for name, path, heading in _DOCS:
        section = _section(path, heading)
        for var in ("DATABASE_URL_VIZ", "DATABASE_URL_AUTH"):
            assert var in section, (
                f"{name}: the setup section no longer mentions {var} -- "
                f"the historical drift (issue #464) was exactly this "
                f"omission in CONTRIBUTING")


def test_the_three_docs_setup_steps_are_identical():
    """Cross-doc equality as ORDERED lists: catches an omission,
    addition or reorder in any direction. The message prints every
    doc's step list, naming each doc."""
    per_doc = [(name, _setup_steps(path, heading))
               for name, path, heading in _DOCS]
    reference_name, reference = per_doc[0]
    assert reference, (
        f"parsed zero setup steps from {reference_name} - the guard "
        f"would pass vacuously")
    listing = "\n".join(f"  {name}: {steps}" for name, steps in per_doc)
    for name, steps in per_doc:
        assert steps == reference, (
            f"{name}: setup steps diverge from {reference_name}'s "
            f"(issue #464) -- every doc's normalized step list:\n{listing}")
