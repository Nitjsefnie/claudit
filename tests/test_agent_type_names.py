"""Agent-type canonical names (issue #650): one role, one name across lanes.

The fold itself lives in backend/agent_types.py; these drive it through
parse_file on the lane formats, beside the verbatim-role pins that stay
in tests/test_parse_lanes.py.
"""
import pytest

from backend import constants, parse

from tests.test_parse_lanes import _codex_meta, _kc_profile


@pytest.mark.parametrize("role,expected", [
    (',"agent_role":"explorer"', "Explore"),     # Codex's Explore-equivalent
    (',"agent_role":"worker"', constants.DEFAULT_AGENT_TYPE),  # Codex's default subagent role
    (',"agent_role":"superpowers:code-reviewer"',
     "code-reviewer"),                            # plugin namespace folds
    (',"agent_role":"task-reviewer"', "task-reviewer"),  # verbatim passes through
])
def test_codex_roles_take_their_canonical_name(role, expected):
    out = parse.parse_file("sessions/p/s/wire.jsonl", _codex_meta(role))
    assert out["agent_type"] == expected


def test_kimi_code_explore_profile_folds_to_the_canonical_name():
    """Kimi's `explore` profile is the Explore-equivalent role: it takes
    Claude's spelling (issue #650)."""
    out = parse.parse_file("sessions/p/s/wire.jsonl",
                           _kc_profile('"explore"'))
    assert out["agent_type"] == "Explore"
