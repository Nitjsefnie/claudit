"""Prompt-gate fixtures (issue #213): XML-wrapped harness injections are
not prompts.

One test per fixture, names 1:1; the Codex lane's R1 pin rides here too
(its event-keyed turns cannot open on injected XML — no lane code
change, the fixture pins that). These live in their own module because
tests/test_parse.py and tests/test_parse_codex.py sit under the
module-size baseline (SV-CI-RATCHETS), which entered files may not
grow.
"""
from pathlib import Path

import pytest

from backend import parse
from backend.prompt_gate import _is_prompt_text

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"


def _read(name):
    return (FIX / name).read_bytes()


def test_prompt_count_excludes_xml_wrapped_injections():
    """Harness-injected user lines that OPEN with an XML tag are data, not
    prompts (issue #213): they count neither toward prompt_count nor as
    ctx-turn boundaries, and they never re-anchor the latency window —
    the second reply measures from the real prompt two lines above it,
    not from the reminder sitting directly before it. The replies with no
    anchored prompt at all (only injections before them) have NULL
    latency, including the reply after the U+001C-prefixed notification:
    Python lstrip strips the C0 separators, so the backend denies it, and
    the JS strip carries the same explicit ranges (SV-PARSER-SPEC)."""
    out = parse.parse_file("k/sess-x/sess-x.jsonl", _read("prompt_xml_injection.jsonl"))
    assert out["prompt_count"] == 2
    assert len(out["ctx_turns"]) == 2
    lats = [r["reply_latency_s"] for r in out["records"]]
    assert lats[0] == pytest.approx(1.0)
    assert lats[1] is None
    assert lats[2] is None
    assert lats[3] == pytest.approx(2.0)


def test_prompt_count_keeps_pasted_content():
    """<pasted_content> wraps text a person pasted, so it STAYS a prompt
    (issue #213 keep-list): it counts and re-anchors the latency window —
    the reply answers the paste, not the line before it."""
    out = parse.parse_file("k/sess-pc/sess-pc.jsonl",
                           _read("prompt_pasted_content_keeps.jsonl"))
    assert out["prompt_count"] == 2
    assert out["records"][0]["reply_latency_s"] == pytest.approx(1.0)


def test_prompt_count_excludes_unknown_xml_tags_by_default():
    """Deny-by-default (issue #213): an injected wrapper we have never
    heard of (<environment_context>, <user_instructions>) is excluded
    without a parser change; only the real prompt counts and anchors."""
    out = parse.parse_file("k/sess-u/sess-u.jsonl",
                           _read("prompt_unknown_xml_tag.jsonl"))
    assert out["prompt_count"] == 1
    assert out["records"][0]["reply_latency_s"] == pytest.approx(1.0)


def test_prompt_count_counts_xml_only_when_it_opens():
    """Only the OPENING matters (issue #213): a tag mid-text is the
    person's own words around harness vocabulary, a leading CLOSING tag
    is not an opening tag, and a URL in angle brackets fails the tag
    shape (the // is outside the tag-name alphabet) — all three texts
    count as prompts and anchor their replies."""
    out = parse.parse_file("k/sess-m/sess-m.jsonl", _read("prompt_xml_midtext.jsonl"))
    assert out["prompt_count"] == 3
    lats = [r["reply_latency_s"] for r in out["records"]]
    assert lats[0] == pytest.approx(1.0)
    assert lats[1] == pytest.approx(1.0)
    assert lats[2] == pytest.approx(1.0)


def test_is_prompt_text_direct_shapes():
    """The gate's defensive edges, directly (issue #213): empty and
    whitespace-only text is not a prompt; the keep-list wrapper around a
    human paste is; a wrapped notification is not."""
    assert _is_prompt_text("") is False
    assert _is_prompt_text("   ") is False
    assert _is_prompt_text('<pasted_content id="3">x</pasted_content>') is True
    assert _is_prompt_text("<task-notification>x</task-notification>") is False


def test_codex_turns_ignore_injected_user_xml():
    """Codex prompt_count is event-keyed (one turn per task_started), so
    injected response_item/user messages carrying <environment_context> /
    <user_instructions> open no turn and count no prompt (issue #213, R1:
    no lane code change — this fixture pins that the lane path stays
    immune so drift fails loudly)."""
    out = parse.parse_file("codex/codex_user_xml.jsonl",
                           _read("codex_user_xml.jsonl"))
    assert out["prompt_count"] == 1
    assert len(out["records"]) == 1
