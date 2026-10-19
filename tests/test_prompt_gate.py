"""Prompt-gate fixtures preserve prompt counts and latency anchors."""
from pathlib import Path

import pytest

from backend import parse
from backend.prompt_gate import _is_prompt_text

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"


def _read(name):
    return (FIX / name).read_bytes()


@pytest.mark.parametrize("fixture,prompts,latency_count,turns", [
    ("prompt_xml_injection.jsonl", 2, 4, 2),
    ("prompt_pasted_content_keeps.jsonl", 2, 1, None),
    ("prompt_unknown_xml_tag.jsonl", 1, 1, None),
    ("prompt_xml_midtext.jsonl", 3, 3, None),
], ids=["xml-injection", "pasted-content", "unknown-xml", "midtext-xml"])
def test_prompt_count_and_latency_follow_the_gate(fixture, prompts, latency_count, turns):
    out = parse.parse_file("k/sess/sess.jsonl", _read(fixture))
    assert out["prompt_count"] == prompts
    if turns is not None:
        assert len(out["ctx_turns"]) == turns
    lats = [r["reply_latency_s"] for r in out["records"]]
    for index in range(latency_count):
        assert lats[index] == pytest.approx(1.0)


def test_is_prompt_text_direct_shapes():
    """The gate's defensive edges, directly: empty and
    whitespace-only text is not a prompt; the keep-list wrapper around a
    human paste is; a wrapped notification is not."""
    assert _is_prompt_text("") is False
    assert _is_prompt_text("   ") is False
    assert _is_prompt_text('<pasted_content id="3">x</pasted_content>') is True
    assert _is_prompt_text("<task-notification>x</task-notification>") is False


def test_codex_turns_ignore_injected_user_xml():
    """Codex prompt_count is event-keyed (one turn per task_started), so
    injected response_item/user messages carrying <environment_context> /
    <user_instructions> open no turn and count no prompt (R1:
    no lane code change — this fixture pins that the lane path stays
    immune so drift fails loudly)."""
    out = parse.parse_file("codex/codex_user_xml.jsonl",
                           _read("codex_user_xml.jsonl"))
    assert out["prompt_count"] == 1
    assert len(out["records"]) == 1
