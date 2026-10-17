"""Claude prompt-count and context-turn gate, independent of latency.

The gate text is the maintainer ruling verbatim. Its explicit whitespace class
must stay independent of interpreter Unicode tables and match the browser.
Module boundary: SV-CI-RATCHETS.
"""
from __future__ import annotations

import re

_PROMPT_WS_CLASS_BODY = (
    r"\x09-\x0d\x1c-\x1f\x20\x85\xa0\u1680"
    r"\u2000-\u200a\u2028\u2029\u202f\u205f\u3000"
)
_PROMPT_WS_RE = re.compile(rf"^[{_PROMPT_WS_CLASS_BODY}]+")

# A user text that OPENS with an XML tag is harness-injected data, not a
# prompt — deny-by-default, so an unknown future harness tag is excluded
# without a parser change. Wrappers that carry text a person provided are
# kept: <pasted_content> wraps a human paste, which IS a prompt.
_PROMPT_DATA_TAG_RE = re.compile(
    rf"<([A-Za-z][A-Za-z0-9._:-]*)(?:[{_PROMPT_WS_CLASS_BODY}][^<>]*)?>"
)
_PROMPT_HUMAN_TAGS = frozenset({"pasted_content"})


def lstrip_prompt_ws(text: str) -> str:
    return _PROMPT_WS_RE.sub("", text)


def _is_prompt_text(text: str) -> bool:
    """True when a user text counts as a prompt.

    Instrumentation is denied by SHAPE (opens with an XML tag), not by an
    ever-growing list; only wrappers around human text are kept. Empty
    text is not a prompt.
    """
    stripped = lstrip_prompt_ws(text)
    if not stripped:
        return False
    m = _PROMPT_DATA_TAG_RE.match(stripped)
    if m is None:
        return True
    return m.group(1) in _PROMPT_HUMAN_TAGS
