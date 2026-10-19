"""Canonical Codex roles and Kimi's coder profile through parse_file."""
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


def test_kimi_code_coder_profile_is_the_implementer_role():
    """Kimi's `coder` profile is the implementer role: it
    takes Claude's spelling, not the default bucket."""
    out = parse.parse_file("sessions/p/s/wire.jsonl",
                           _kc_profile('"coder"'))
    assert out["agent_type"] == "implementer"
