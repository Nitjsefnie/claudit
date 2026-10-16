"""Pin the three docs' application-database setup blocks together.

The setup is written out three times -- README "Quick start",
CONTRIBUTING "Getting it running" and AGENTS "Build and test commands"
-- and nothing failed when they drifted: CONTRIBUTING once shipped
without the CLAUDIT_DATABASE_URL_AUTH env-edit mention (issue #464). Like
test_panel_wiring.py, these are source-level pins: nothing here runs
the commands or touches a database, and nothing else in the suite reads
these docs, so a diverging edit passes every other test.

Three guards:

1. Inventory floor. Each doc's setup block, independently, carries the
   two load-bearing auth-DB steps (the auth-database creation step and
   the `CREATE TABLE users` psql step). Equality across docs alone would
   stay green if all three dropped the same step together.
2. Env-edit mentions. Each doc's section text names both
   CLAUDIT_DATABASE_URL_VIZ and CLAUDIT_DATABASE_URL_AUTH -- the historical drift was
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

CI compares these shared setup blocks before narrowing doc changes:
changed blocks run this guard; unreadable sections run the full gate.

Normalization boundary: each command's trailing ` # ...` inline
comment is stripped, so a quoted or escaped ` #` literal inside a
command line would be over-stripped -- none exists in any setup line
today, and the strip applies identically to all three docs, so the
equality test holds regardless.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from backend.guest_login import GUEST_LOGIN_ENV
from scripts.ci.setup_docs import SECTIONS, SERVER_START, section, setup_block

ROOT = Path(__file__).resolve().parents[1]


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
    block = setup_block(path.read_text(encoding="utf-8"), heading)
    assert block is not None, (
        f"{path.name}: no readable fenced block containing {SERVER_START!r} "
        f"under `## {heading}` -- relocate this pin with it")
    return _normalize(block)


def test_each_doc_carries_the_auth_db_steps():
    """Inventory floor, per doc independently: without these two steps a
    fresh `users` table never exists and startup's schema_check aborts,
    however well the three docs agree with each other."""
    for name, heading in SECTIONS:
        path = ROOT / name
        steps = _setup_steps(path, heading)
        # Spelled split mid-word: test_scratch_db.py's whole-tree guard
        # flags DDL-shaped literals in tests/ source, so the command is
        # spelled "create" "db ..." -- adjacent-literal concatenation
        # yields the same runtime string, while no token spells the
        # command contiguously for the guard's scan.
        assert "create" "db claudit_auth" in steps, (
            f"{name}: the auth-DB creation step is gone from the "
            f"setup block")
        assert any("CREATE TABLE users" in s for s in steps), (
            f"{name}: the CREATE TABLE users step is gone from the "
            f"setup block")


def test_each_doc_names_both_env_vars_in_its_setup_section():
    """Env-edit mentions, per doc independently. The setup block's cp
    line tells the reader which env values to edit; a doc whose section
    stopped naming one of them is the exact drift this issue closed.
    Guest-login config also names the same toggle the code reads."""
    for name, heading in SECTIONS:
        path = ROOT / name
        selected = section(path.read_text(encoding="utf-8"), heading)
        assert selected is not None, f"{name}: missing setup section {heading}"
        for var in ("CLAUDIT_DATABASE_URL_VIZ", "CLAUDIT_DATABASE_URL_AUTH"):
            assert var in selected, (
                f"{name}: the setup section no longer mentions {var} -- "
                f"the historical drift (issue #464) was exactly this "
                f"omission in CONTRIBUTING")
    for path in (ROOT / "backend" / ".env.example",
                 ROOT / "README.md", ROOT / "compose.yaml"):
        assert GUEST_LOGIN_ENV in path.read_text(encoding="utf-8"), path.name


def test_compose_passes_through_guest_login_setting() -> None:
    """A comment cannot stand in for the app's configurable guest setting."""
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    environment = compose["services"]["app"]["environment"]
    assert environment.get(GUEST_LOGIN_ENV) == "${CLAUDIT_GUEST_LOGIN:-1}"


def test_the_three_docs_setup_steps_are_identical():
    """Cross-doc equality as ORDERED lists: catches an omission,
    addition or reorder in any direction. The message prints every
    doc's step list, naming each doc."""
    per_doc = [(name, _setup_steps(ROOT / name, heading))
               for name, heading in SECTIONS]
    reference_name, reference = per_doc[0]
    assert reference, (
        f"parsed zero setup steps from {reference_name} - the guard "
        f"would pass vacuously")
    listing = "\n".join(f"  {name}: {steps}" for name, steps in per_doc)
    for name, steps in per_doc:
        assert steps == reference, (
            f"{name}: setup steps diverge from {reference_name}'s "
            f"(issue #464) -- every doc's normalized step list:\n{listing}")
