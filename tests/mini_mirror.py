"""What the committed mini mirror holds, derived rather than written down.

`fixtures/r2_mini` is the corpus a dozen tests ingest, the reparse bench
measures and the smoke job boots against, and it grows: issue #503 added
the lane-layout wires and their meta.json sidecar so the Codex and both
Kimi formats are exercised end to end. A count written into a test is a
count that has to be edited on every addition and is silently wrong the
day one is missed, so the inventory is read from the tree here and the
assertions stay about what the ingest DID with it.
"""
from __future__ import annotations

from pathlib import Path

MIRROR = Path(__file__).resolve().parents[1] / "fixtures" / "r2_mini"
BUCKET = "claude"


def object_keys() -> list[str]:
    """Every transcript key the mirror's bucket holds, bucket-qualified.

    The mirror's first level is the bucket directory the file-mode client
    scans from, so the object key is the path below it — which is what a
    stored `files.file_key` carries.
    """
    root = MIRROR / BUCKET
    return sorted(f"{BUCKET}/{path.relative_to(root).as_posix()}"
                  for path in root.rglob("*.jsonl"))


def project_ids() -> list[str]:
    """Every project id the mirror's transcripts classify into."""
    return sorted(file_counts())


def file_counts() -> dict:
    """{project id: transcripts} for the mirror, under the layout rules."""
    # pylint: disable-next=import-outside-toplevel
    from backend import key_layout

    found: dict = {}
    for key in object_keys():
        info = key_layout.classify(key.split("/", 1)[1])
        if info is not None:
            found[info.project_id] = found.get(info.project_id, 0) + 1
    return found


def folded_owners(source: str, target: str) -> dict:
    """The mirror's file counts with `source` folded into `target`.

    What an end-to-end alias fold must leave behind: the source's files
    counted at the target, every other project untouched.
    """
    owners: dict = {}
    for project_id, count in file_counts().items():
        owners[target if project_id == source else project_id] = (
            owners.get(target if project_id == source else project_id, 0)
            + count)
    return owners


def counts() -> dict:
    """The mirror's shape, as the ingest's own layout rules see it."""
    # pylint: disable-next=import-outside-toplevel
    from backend import key_layout

    mains = 0
    sessions = set()
    for key in object_keys():
        info = key_layout.classify(key.split("/", 1)[1])
        if info is None:
            continue
        mains += int(info.is_main)
        sessions.add(info.session_id)
    return {"transcripts": len(object_keys()), "main": mains,
            "sessions": len(sessions), "projects": len(project_ids())}
