"""Canonical agent roles across lanes.

Explore/explore/explorer share Claude's spelling; plugin namespaces fold on the
last colon. Kimi coder is implementer; Codex worker is no recorded
role and folds to DEFAULT_AGENT_TYPE. Keep the per-role hot path one flat lookup
for the reparse CPU budget.
"""
from __future__ import annotations

from backend import branding
from backend.constants import DEFAULT_AGENT_TYPE

# Cross-lane spellings fold to Claude's canonical names in one lookup.
# Kimi coder is implementer; Codex worker, the default with no named role,
# folds to the unattributable bucket.
_FOLD = {
    "explore": "Explore",
    "explorer": "Explore",
    "coder": "implementer",
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
    """Serve the Inspector's fold table from its only home, the backend.

    The browser holds lookup logic only. branding.script_json prevents the
    payload from closing its script tag."""
    return f"window.AGENT_TYPE_FOLD = {branding.script_json(fold_table())};"
