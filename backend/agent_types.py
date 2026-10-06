"""Agent-role canonical names (issue #650): one role, one name across lanes.

files.agent_type and the dispatch columns store the role in the lane's own
spelling, so Cost by Agent Type showed the same role once per lane and
showed lane defaults as distinct roles. canonical_agent_type folds them:

- the Explore-equivalent role — Claude's ``Explore``, Kimi's ``explore``
  profile, Codex's ``explorer`` — resolves to Claude's spelling, the one
  name the Claude lane has always reported;
- a plugin-namespaced type (``superpowers:code-reviewer``) splits on the
  last colon and folds to the type itself, whatever lane named it
  namespaced;
- the lanes' default subagent roles — Kimi's ``coder``, Codex's
  ``worker`` — are "no real role was selected", the same DEFAULT_AGENT_TYPE
  bucket the lanes' default MAIN profiles (``agent``/``default``) already
  land in through parse_lanes.

One flat fold table: the function runs per named role on every reparse,
and the reparse CPU gate holds parse_body to a bytecode budget — the
lookup is the whole cost.
"""
from __future__ import annotations

from backend import branding
from backend.constants import DEFAULT_AGENT_TYPE

# The whole fold in one lookup: cross-lane spellings fold to one name (the
# canonical spellings are Claude's — the vocabulary the dashboard has
# always reported), and the lanes' default subagent roles (what a spawn
# runs as when the dispatch asked for no real role — Kimi's coder profile,
# Codex's worker role) fold to the unattributable bucket, not a role.
_FOLD = {
    "explore": "Explore",
    "explorer": "Explore",
    "coder": DEFAULT_AGENT_TYPE,
    "worker": DEFAULT_AGENT_TYPE,
}


def canonical_agent_type(role: str) -> str:
    """The canonical name for a role any lane named.

    A plugin-namespaced name takes the part after the LAST colon (a
    nested namespace folds to the type too); then one fold lookup.
    DEFAULT_AGENT_TYPE and a name no rule names pass through verbatim.
    """
    if ":" in role:
        role = role.rpartition(":")[2]
    return _FOLD.get(role, role)


def fold_table() -> dict[str, str]:
    """A copy of the fold table, for the served injection and its tests:
    callers never reach into _FOLD directly."""
    return dict(_FOLD)


def fold_js() -> str:
    """The script statement serving the fold table to the Inspector page
    (issue #691): the backend stays the table's only home, and the
    browser lookup (src/agent-types.js) holds the lookup logic only.
    branding.script_json keeps the payload from closing the tag it rides
    in, like window.BRAND beside it.
    """
    return f"window.AGENT_TYPE_FOLD = {branding.script_json(fold_table())};"
