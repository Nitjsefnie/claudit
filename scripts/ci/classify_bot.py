from __future__ import annotations

from collections.abc import Callable

BOT_EMAIL = '41898282+github-actions[bot]@users.noreply.github.com'


def is_bot_push(event: dict, run: Callable[[list[str]], str]) -> bool:
    """Only the refresh bot's own push can carry pre-tested pricing data."""
    if event.get('name') != 'push':
        return False
    repository, sha = event.get('repository'), event.get('sha')
    if not repository or not sha:
        return False
    try:
        email = run([
            'gh', 'api', '-H', 'Cache-Control: no-cache',
            f'repos/{repository}/commits/{sha}',
            '--jq', '.commit.author.email'])
        return email.strip() == BOT_EMAIL
    except Exception:  # unreadable means full gates
        return False
