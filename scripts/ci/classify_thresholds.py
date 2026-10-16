"""Emit classifier outputs."""
from __future__ import annotations


def thresholds_only(paths: list[str]) -> bool:
    """Rename source paths count too."""
    return paths == ['.github/ci-thresholds.json']


def unreadable_or_thresholds(paths: list[str] | None):
    """The caller must exclude other path sets."""
    if paths is None:
        return (False, False, False, False, False,
                'could not read the changed paths; running the full gate set')
    return (False, False, False, True, False, 'thresholds-only change: 1 path')


def write_outputs(path, documentation, data, comment, thresholds, setup, reason):
    """Append flags and reason."""
    if not path:
        return
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(f"docs_only={'true' if documentation else 'false'}\n")
        handle.write(f"data_only={'true' if data else 'false'}\n")
        handle.write(f"comment_only={'true' if comment else 'false'}\n")
        handle.write(f"thresholds_only={'true' if thresholds else 'false'}\n")
        handle.write(f"setup_only={'true' if setup else 'false'}\n")
        handle.write(f'reason={reason}\n')
