"""The canonical winner prefers an attributed-model copy (issue #529).

A forked Codex rollout REPLAYS its parent's history in its leading lines,
and a multi-model fork cannot attribute that prefix (no model declaration
in front of it — sole_model is only for single-model files). The fork's
`subagents/…` file key sorts before its parent's `wire.jsonl`, so under a
bare file_key ordering the unattributed copies won the dedup and the
Models panel grew an `unknown` row for usage every copy of which is
attributed elsewhere.

The winner rule is the spec (SV-CANONICAL-FLAG): the copy whose model is
attributed beats an unattributed one, then file_key, then line_num.
`unknown` survives only where NO copy of the uuid names a model.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from backend import db, ingest
from tests import scratch_db

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "codex"
FORK_FIXTURE = FIX / "rollout_fork_model_switch.jsonl"

# The parent's session id — a fork REUSES it, which is what puts the two
# files' records in the same dedup partition (parse_codex._codex_record_uuid).
S = "00000000-0000-4000-8000-000000000001"
REPLAY_UUIDS = (
    f"{S}:100500",
    f"{S}:110920",
)


def _token_count(ts, cumulative, last):
    """One token_count event. (input, cached, output, reasoning) order."""
    return {
        "timestamp": ts, "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": cumulative[0],
                    "cached_input_tokens": cumulative[1],
                    "cache_write_input_tokens": 0,
                    "output_tokens": cumulative[2],
                    "reasoning_output_tokens": cumulative[3],
                    "total_tokens": cumulative[0] + cumulative[2],
                },
                "last_token_usage": {
                    "input_tokens": last[0],
                    "cached_input_tokens": last[1],
                    "cache_write_input_tokens": 0,
                    "output_tokens": last[2],
                    "reasoning_output_tokens": last[3],
                    "total_tokens": last[0] + last[2],
                },
            },
        },
    }


def _parent_rollout() -> bytes:
    """The parent the fork's replayed prefix replays: two requests on
    gpt-5.6-sol, at exactly the cumulative totals the fork fixture's
    leading token_count events reproduce."""
    lines = [
        {"timestamp": "2026-06-14T11:00:01.000Z", "type": "session_meta",
         "payload": {"session_id": S, "id": S,
                     "cwd": "/workspace/toy-project",
                     "originator": "codex-tui", "cli_version": "1.0.0"}},
        {"timestamp": "2026-06-14T11:00:02.000Z", "type": "turn_context",
         "payload": {"model": "gpt-5.6-sol"}},
        # request 1: the counter opens at zero, so cumulative == last
        _token_count("2026-06-14T11:00:03.000Z",
                     (100000, 98000, 500, 300), (100000, 98000, 500, 300)),
        # request 2
        _token_count("2026-06-14T11:00:04.000Z",
                     (109800, 107604, 1120, 520), (9800, 9604, 620, 220)),
    ]
    return ("".join(json.dumps(lne, separators=(",", ":")) + "\n"
                    for lne in lines)).encode()


@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    yield from scratch_db.scratch_viz_database(monkeypatch, "canon_attr")


@pytest.fixture(name="fork_mirror")
def _fork_mirror_fixture(monkeypatch, tmp_path):
    """A lane mirror holding the fork fixture beside its (absent or
    present) parent. Yields the mirror root."""
    root = tmp_path / "mirror"
    fork_dir = root / "mini/sessions/toyproj/sessA/subagents/forkthread"
    fork_dir.mkdir(parents=True)
    shutil.copyfile(FORK_FIXTURE, fork_dir / "wire.jsonl")
    monkeypatch.setenv("R2_ENDPOINT", root.as_uri() + "/")
    monkeypatch.setenv("R2_BUCKET", "mini")
    return root


def _replay_rows():
    """Both copies of every replayed uuid, ordered by file_key."""
    with db.viz_conn() as c:
        return c.execute(
            """
            SELECT uuid, file_key, model, is_canonical
              FROM records
             WHERE uuid = ANY(%s)
             ORDER BY uuid, file_key
            """, (list(REPLAY_UUIDS),),
        ).fetchall()


def test_an_attributed_copy_beats_an_unattributed_replay(
        fresh_db, fork_mirror):
    """The parent's attributed copies win the replayed uuids; the fork's
    unknown replays lose the dedup instead of masking the model."""
    parent = fork_mirror / "mini/sessions/toyproj/sessA/wire.jsonl"
    parent.write_bytes(_parent_rollout())

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None

    rows = _replay_rows()
    # The competition must exist: both copies of each replayed uuid.
    assert len(rows) == 4, rows
    by_file = {(u, fk.split("toyproj/")[1]): (m, canon)
               for u, fk, m, canon in rows}
    for uuid in REPLAY_UUIDS:
        fork_model, fork_canon = by_file[(uuid, "sessA/subagents/forkthread/wire.jsonl")]
        parent_model, parent_canon = by_file[(uuid, "sessA/wire.jsonl")]
        assert fork_model == "unknown"
        assert parent_model == "gpt-5.6-sol"
        assert parent_canon is True, f"attributed copy of {uuid} must win"
        assert fork_canon is False, f"unknown replay of {uuid} must lose"


def test_unknown_survives_when_no_copy_attributes_the_usage(
        fresh_db, fork_mirror):
    """With the parent file absent, the fork's replayed prefix has no
    competitor: it stays canonical and unknown — `unknown` is for usage no
    copy attributes, not a state to be engineered away."""
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None

    rows = _replay_rows()
    assert len(rows) == 2, rows
    assert all(model == "unknown" and canon is True
               for _, _, model, canon in rows)
