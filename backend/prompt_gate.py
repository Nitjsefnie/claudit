"""The Claude-path prompt gate (issue #213).

Decides whether a user text counts as a prompt for `prompt_count`,
ctx-turn boundaries and reply-latency anchoring. Split out of
parse.py, which had no room to grow under the module-size ratchet
(SV-CI-RATCHETS); the gate's own text is the maintainer ruling
verbatim.
"""
from __future__ import annotations

import re

# A user text that OPENS with an XML tag is harness-injected data, not a
# prompt — deny-by-default, so an unknown future harness tag is excluded
# without a parser change. Wrappers that carry text a person provided are
# kept: <pasted_content> wraps a human paste, which IS a prompt.
_PROMPT_DATA_TAG_RE = re.compile(r"<([A-Za-z][A-Za-z0-9._:-]*)(?:\s[^<>]*)?>")
_PROMPT_HUMAN_TAGS = frozenset({"pasted_content"})


def _is_prompt_text(text: str) -> bool:
    """True when a user text counts as a prompt.

    Instrumentation is denied by SHAPE (opens with an XML tag), not by an
    ever-growing list; only wrappers around human text are kept. Empty
    text is not a prompt.
    """
    stripped = text.lstrip()
    if not stripped:
        return False
    m = _PROMPT_DATA_TAG_RE.match(stripped)
    if m is None:
        return True
    return m.group(1) in _PROMPT_HUMAN_TAGS
