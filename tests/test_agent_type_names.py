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


def test_kimi_dispatch_asking_for_coder_is_the_implementer_role():
    """A dispatch asking for `coder` stores `implementer` (issue #749)."""
    out = parse.parse_file(
        "sessions/p/s/wire.jsonl",
        (b'{"type":"metadata","protocol_version":"1.4",'
         b'"created_at":1782740973430}\n'
         b'{"type":"context.append_message","message":{"role":"assistant",'
         b'"content":[],"toolCalls":[{"type":"function","id":"tool_A2",'
         b'"function":{"name":"Agent","arguments":"{\\"subagent_type\\":'
         b'\\"coder\\",\\"model\\":\\"k3\\",\\"prompt\\":\\"do it\\"}"}}]},'
         b'"time":1782740973431}\n'))
    assert out["tool_uses"][0]["agent_type"] == "implementer"


def test_kimi_code_explore_profile_folds_to_the_canonical_name():
    """Kimi's `explore` profile is the Explore-equivalent role: it takes
    Claude's spelling (issue #650)."""
    out = parse.parse_file("sessions/p/s/wire.jsonl",
                           _kc_profile('"explore"'))
    assert out["agent_type"] == "Explore"


def test_kimi_code_coder_profile_is_the_implementer_role():
    """Kimi's `coder` profile is the implementer role (issue #749): it
    takes Claude's spelling, not the default bucket."""
    out = parse.parse_file("sessions/p/s/wire.jsonl",
                           _kc_profile('"coder"'))
    assert out["agent_type"] == "implementer"
