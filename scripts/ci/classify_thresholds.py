from __future__ import annotations


RATCHET_DATA_FILES = frozenset({
    '.github/ci-thresholds.json',
    '.github/comment-share.json',
    '.github/size-baseline.json',
    '.github/test-times.json',
})
# Trigger-ignored, so a records commit stays in the range; no ci-gate verdict
# reads these. Tests' checks read ci-thresholds.
BOT_DATA_RECORDS = RATCHET_DATA_FILES - {'.github/ci-thresholds.json'}


def bot_data_paths(event: dict, paths: list[str]) -> list[str]:
    if event.get('name') != 'push':
        return paths
    return [path for path in paths if path not in BOT_DATA_RECORDS]


def thresholds_only(paths: list[str]) -> bool:
    """True for non-empty changes confined to ratchet data files."""
    return bool(paths) and set(paths) <= RATCHET_DATA_FILES


def unreadable_or_thresholds(paths: list[str] | None):
    """The caller must exclude other path sets."""
    if paths is None:
        return (False, False, False, False, False,
                'could not read the changed paths; running the full gate set')
    return (False, False, False, True, False,
            f'ratchet data changed: {len(paths)} path(s)')


def write_outputs(path, documentation, data, comment, thresholds, setup, reason):
    if not path:
        return
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(f"docs_only={'true' if documentation else 'false'}\n")
        handle.write(f"data_only={'true' if data else 'false'}\n")
        handle.write(f"comment_only={'true' if comment else 'false'}\n")
        handle.write(f"thresholds_only={'true' if thresholds else 'false'}\n")
        handle.write(f"setup_only={'true' if setup else 'false'}\n")
        handle.write(f'reason={reason}\n')
