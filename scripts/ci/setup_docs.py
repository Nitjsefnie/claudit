"""Pinned setup sections shared by the documentation guard and CI classifier."""
from __future__ import annotations

import re

SECTIONS = (
    ('README.md', 'Quick start'),
    ('CONTRIBUTING.md', 'Getting it running'),
    ('AGENTS.md', 'Build and test commands'),
)
SERVER_START = 'python3 -m uvicorn'
GUEST_LOGIN_ENV_LITERAL = 'CLAUDIT_GUEST_LOGIN'


def section(text: str, heading: str) -> str | None:
    """Keep the heading and its section, including any level-three headings."""
    head = re.search(rf'^## {re.escape(heading)}[^\S\n]*$', text, re.M)
    if head is None:
        return None
    nxt = re.search(r'^## ', text[head.end():], re.M)
    end = head.end() + nxt.start() if nxt else len(text)
    return text[head.start():end]


def setup_block(text: str, heading: str) -> str | None:
    """Return the server-start fence's body verbatim, or None if unreadable.

    An unterminated fence anywhere in the section invalidates extraction,
    matching the setup guard's boundary check. Preserve whitespace and line
    endings so the classifier compares bytes without normalizing commands.
    """
    selected = section(text, heading)
    if selected is None:
        return None
    body: list[str] | None = None
    found = None
    for line in selected.splitlines(keepends=True):
        if line.lstrip().startswith('```'):
            if body is None:
                body = []
            else:
                block = ''.join(body)
                if found is None and SERVER_START in block:
                    found = block
                body = None
        elif body is not None:
            body.append(line)
    return found if body is None else None


def pin_facts(text: str, path: str, heading: str) -> tuple[str | bool, ...] | None:
    """The guard's complete section and any file-wide presence facts.

    Unreadable setup sections or fences retain the full-gate fallback.
    README's guest-setting presence is pinned beyond its setup section.
    """
    selected = section(text, heading)
    if selected is None or setup_block(text, heading) is None:
        return None
    return (selected, GUEST_LOGIN_ENV_LITERAL in text) if path == 'README.md' else (selected,)


def classify(paths, event, run, refs, blob):
    """Classify a documentation-shaped set after checking its pinned facts.

    Every changed setup doc must extract on both sides, even after an earlier
    doc changed. Read and extraction failures always run the full gate set.
    """
    selected = [(path, heading) for path, heading in SECTIONS if path in paths]
    changed = []
    if selected:
        pair = refs(event, event['repository'], run) if event.get('repository') else None
        if pair is None:
            return (False, False, False, False, False,
                    'could not read setup doc refs; running the full gate set')
        for path, heading in selected:
            observed = []
            for side, ref in zip(('base', 'head'), pair):
                raw = blob(event['repository'], path, ref, run)
                try:
                    facts = pin_facts(raw.decode('utf-8'), path, heading) if raw is not None else None
                except (UnicodeError, AttributeError):
                    facts = None
                if facts is None:
                    return (False, False, False, False, False,
                            f"could not {'read' if raw is None else 'extract'} "
                            f'{side} setup facts in {path}; '
                            'running the full gate set')
                observed.append(facts)
            if observed[0] != observed[1]:
                changed.append(path)
    if changed:
        return (False, False, False, False, True,
                f'setup-only change: {"; ".join(changed)}')
    return (True, False, False, False, False,
            f'documentation-only change: {len(paths)} paths')
