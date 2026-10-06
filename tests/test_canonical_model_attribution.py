"""A replayed copy loses to its original, whatever the key order (issues #529, #687).

A forked Codex rollout REPLAYS its parent's history in its leading lines.
Under issue #653 the replayed prefix takes the file's first declared
model, so both copies of a replayed uuid are attributed and — before
#687 — the attribution rank tied and `file_key` decided: the fork's
`subagents/…` key sorts before its parent's `wire.jsonl`, and the fork's
copy won. #687 adds a replay rank ahead of attribution: a copy parsed
from a fork's replayed prefix (marked `is_replay` at parse time, from
the first session_meta's `forked_from_id` and the replay's position
before the fork's own first model declaration) loses to an original of
the same uuid, whatever the key order, so the parent's copy — carrying
the model the parent had in force — stays canonical and replayed history
counts once, as the main session's.

With the parent absent, the fork's replay is the only copy and stays
canonical, attributed to the fork's first declared model — the #653
fallback, never an `unknown` placeholder. With the parent present, a
losing copy also ADOPTS the original's model (issue #713): the fork's
first declaration is the model at the fork point, and only the parent's
copy carries the model the parent had in force for that record.

The winner rule is the spec (SV-CANONICAL-FLAG): an original beats a
replay, an attributed copy beats an unattributed one, then file_key,
then line_num. Under #653 and #688 no parser emits a placeholder, so
the unattributed members of the rank are reachable only as stored
historical rows (the lockstep shapes test_ingest pins) — a live
model-less file is refused at parse instead.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from backend import constants, db, ingest
from backend.ingest_scope import Scope
from tests import scratch_db

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "codex"
FORK_FIXTURE = FIX / "rollout_fork_model_switch.jsonl"
DIFFERENT_MODEL_FORK_FIXTURE = FIX / "rollout_fork_different_model.jsonl"

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
    gpt-5.6-sol, at exactly the cumulative totals the fork fixtures'
    leading token_count events reproduce."""
    lines = [
        {"timestamp": "2026-06-14T11:00:01.000Z", "type": "session_meta",
         "payload": {"session_id": S, "id": "00000000-0000-4000-8000-000000000003",
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


def _switching_parent_rollout() -> bytes:
    """The parent of the different-model fork fixture, switching models
    mid-history: request 1 runs on gpt-5.6-sol, request 2 on
    gpt-5.6-terra. The fork (cut after the switch) declares terra — the
    model at the fork point — so its replayed copy of request 1 is wrong
    at parse time, and only the parent's copy knows sol."""
    lines = [
        {"timestamp": "2026-06-14T11:00:01.000Z", "type": "session_meta",
         "payload": {"session_id": S, "id": "00000000-0000-4000-8000-000000000003",
                     "cwd": "/workspace/toy-project",
                     "originator": "codex-tui", "cli_version": "1.0.0"}},
        {"timestamp": "2026-06-14T11:00:02.000Z", "type": "turn_context",
         "payload": {"model": "gpt-5.6-sol"}},
        # request 1: the counter opens at zero, so cumulative == last
        _token_count("2026-06-14T11:00:03.000Z",
                     (100000, 98000, 500, 300), (100000, 98000, 500, 300)),
        {"timestamp": "2026-06-14T11:00:04.000Z", "type": "turn_context",
         "payload": {"model": "gpt-5.6-terra"}},
        # request 2
        _token_count("2026-06-14T11:00:05.000Z",
                     (109800, 107604, 1120, 520), (9800, 9604, 620, 220)),
    ]
    return ("".join(json.dumps(lne, separators=(",", ":")) + "\n"
                    for lne in lines)).encode()


@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    yield from scratch_db.scratch_viz_database(monkeypatch, "canon_attr")


def _mirror(monkeypatch, tmp_path, fixture: Path, parent_bytes: bytes | None):
    """A lane mirror holding `fixture` as the fork and, when given, the
    parent rollout beside it. Yields the mirror root."""
    root = tmp_path / "mirror"
    fork_dir = root / "mini/sessions/toyproj/sessA/subagents/forkthread"
    fork_dir.mkdir(parents=True)
    shutil.copyfile(fixture, fork_dir / "wire.jsonl")
    if parent_bytes is not None:
        parent = root / "mini/sessions/toyproj/sessA/wire.jsonl"
        parent.write_bytes(parent_bytes)
    monkeypatch.setenv("R2_ENDPOINT", root.as_uri() + "/")
    monkeypatch.setenv("R2_BUCKET", "mini")
    return root


@pytest.fixture(name="fork_mirror")
def _fork_mirror_fixture(monkeypatch, tmp_path):
    """A lane mirror holding the sol-seeded fork fixture alone."""
    return _mirror(monkeypatch, tmp_path, FORK_FIXTURE, None)


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


def test_a_replayed_copy_loses_to_the_parents_original(
        fresh_db, monkeypatch, tmp_path):
    """Parent and fork declare DIFFERENT models, so neither half of the
    rank can hide: the fork's first declaration is gpt-5.6-terra while the
    parent ran gpt-5.6-sol, and the fork was spawned with the child's own
    model (a spawn_agent `model` argument). The replayed uuids' parent
    copies — carrying the model the parent had in force — win the dedup
    whatever the key order, so replayed history counts once, as the main
    session's, under the parent's model; the fork's losing copies ADOPT
    the parent's model in force (issue #713), so the stored row a later
    promotion surfaces agrees with the winner instead of carrying the
    fork's first declaration."""
    _mirror(monkeypatch, tmp_path, DIFFERENT_MODEL_FORK_FIXTURE,
            _parent_rollout())

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
        assert parent_model == "gpt-5.6-sol"
        assert fork_model == "gpt-5.6-sol"
        assert parent_canon is True, f"the original copy of {uuid} must win"
        assert fork_canon is False, f"the replayed copy of {uuid} must lose"


def test_a_replayed_copy_takes_the_model_the_parent_had_in_force(
        fresh_db, monkeypatch, tmp_path):
    """Issue #713's core: the fork's first declaration is the model at
    the fork point, not the model each replayed request ran on. The
    parent switches sol -> terra mid-history and the fork (cut after the
    switch) declares terra, so the fork's parse-time copy of the parent's
    SOL request is wrong and only the parent's copy can correct it."""
    _mirror(monkeypatch, tmp_path, DIFFERENT_MODEL_FORK_FIXTURE,
            _switching_parent_rollout())

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None

    rows = _replay_rows()
    assert len(rows) == 4, rows
    by_file = {(u, fk.split("toyproj/")[1]): (m, canon)
               for u, fk, m, canon in rows}
    for uuid, parent_model in zip(REPLAY_UUIDS,
                                  ("gpt-5.6-sol", "gpt-5.6-terra")):
        fork_model, fork_canon = by_file[
            (uuid, "sessA/subagents/forkthread/wire.jsonl")]
        stored, parent_canon = by_file[(uuid, "sessA/wire.jsonl")]
        assert stored == parent_model
        assert fork_model == parent_model, (
            f"the fork's copy of {uuid} must adopt the parent's model")
        assert parent_canon is True, f"the original of {uuid} must win"
        assert fork_canon is False, f"the replay of {uuid} must lose"


def test_the_forks_own_records_stay_canonical_and_its_own(
        fresh_db, monkeypatch, tmp_path):
    """The replay demotion is scoped to the replayed prefix: the fork's
    own request — parsed from after its own first model declaration —
    stays canonical, under the model the fork itself ran."""
    _mirror(monkeypatch, tmp_path, DIFFERENT_MODEL_FORK_FIXTURE,
            _parent_rollout())

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None

    with db.viz_conn() as c:
        rows = c.execute(
            """
            SELECT r.model, r.is_replay, r.is_canonical, f.is_main
              FROM records r JOIN files f ON f.file_key = r.file_key
             WHERE r.is_replay IS FALSE
               AND r.file_key LIKE '%subagents/forkthread%'
            """,
        ).fetchall()
    # The fork's own request (its counter continues past the parent's
    # readings) is the one non-replayed row on the fork file.
    assert len(rows) == 1, rows
    model, is_replay, canon, is_main = rows[0]
    assert model == "gpt-5.6-terra"
    assert is_replay is False
    assert canon is True
    assert is_main is False


def test_a_parent_arriving_later_adopts_into_the_stored_fork(
        fresh_db, monkeypatch, tmp_path):
    """The adoption is an ingest pass, so it also fires when the parent
    ARRIVES after the fork was stored: the fork's first ingest keeps the
    #653 fallback (its own first declaration), the second — the parent's
    uuids joining the scope — adopts the parent's models into the fork's
    stored rows."""
    root = _mirror(monkeypatch, tmp_path, DIFFERENT_MODEL_FORK_FIXTURE, None)
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    rows = {(u, fk.split("toyproj/")[1]): m
            for u, fk, m, _ in _replay_rows()}
    assert rows == {(u, "sessA/subagents/forkthread/wire.jsonl"): "gpt-5.6-terra"
                    for u in REPLAY_UUIDS}

    (root / "mini/sessions/toyproj/sessA/wire.jsonl").write_bytes(
        _switching_parent_rollout())
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    rows = {(u, fk.split("toyproj/")[1]): m
            for u, fk, m, _ in _replay_rows()}
    assert rows[(REPLAY_UUIDS[0],
                 "sessA/subagents/forkthread/wire.jsonl")] == "gpt-5.6-sol"
    assert rows[(REPLAY_UUIDS[1],
                 "sessA/subagents/forkthread/wire.jsonl")] == "gpt-5.6-terra"


def test_a_promoted_replay_keeps_the_adopted_models(
        fresh_db, monkeypatch, tmp_path):
    """When the parent rollout later leaves the mirror, the canonical
    pass promotes the fork's replayed copies — which keep the models
    they adopted while the parent was present, so the promotion surfaces
    the parent's model in force, not the fork's first declaration."""
    root = _mirror(monkeypatch, tmp_path, DIFFERENT_MODEL_FORK_FIXTURE,
                   _switching_parent_rollout())
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None

    (root / "mini/sessions/toyproj/sessA/wire.jsonl").unlink()
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None

    rows = {(u, fk.split("toyproj/")[1]): (m, canon)
            for u, fk, m, canon in _replay_rows()}
    assert len(rows) == 2, rows
    for uuid, model in zip(REPLAY_UUIDS, ("gpt-5.6-sol", "gpt-5.6-terra")):
        fork_model, fork_canon = rows[
            (uuid, "sessA/subagents/forkthread/wire.jsonl")]
        assert (fork_model, fork_canon) == (model, True), uuid


def test_adoption_extends_files_models(fresh_db, monkeypatch, tmp_path):
    """files.models lists every model that answered in the file; the fork
    journals its parent's requests, so once the originals are present the
    fork's list carries the parent's models too (issue #713)."""
    _mirror(monkeypatch, tmp_path, DIFFERENT_MODEL_FORK_FIXTURE,
            _switching_parent_rollout())
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None

    with db.viz_conn() as c:
        models = dict(c.execute(
            "SELECT file_key, models FROM files").fetchall())
    fork = next(v for k, v in models.items() if "forkthread" in k)
    parent = next(v for k, v in models.items()
                  if k.endswith("sessA/wire.jsonl"))
    assert fork == ["gpt-5.6-sol", "gpt-5.6-terra"]
    assert parent == ["gpt-5.6-sol", "gpt-5.6-terra"]


def test_a_fork_alone_attributes_its_replay_to_the_first_declared_model(
        fresh_db, fork_mirror):
    """With the parent file absent, the fork's replayed prefix has no
    competitor: it takes the file's first declared model (issue #653) and
    stays canonical — no `unknown` placeholder is stored."""
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None

    rows = _replay_rows()
    assert len(rows) == 2, rows
    assert all(model == "gpt-5.6-sol" and canon is True
               for _, _, model, canon in rows)


def test_the_replay_rank_applies_to_tool_uses_too(fresh_db):
    """The winner rule ranks a replayed copy below an original in the
    tool_uses partition as well: a replayed fork call (same call_id as
    the parent's) loses to the parent's copy whatever the key order."""
    with db.viz_conn() as c:
        c.execute("INSERT INTO projects (project_id, display_name, "
                  "first_seen_at, last_seen_at) VALUES ('p', 'p', now(), now())")
        c.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, "
            "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
            "parser_version) VALUES (%s, 'p', 's', TRUE, 'e', 1, now(), "
            "now(), %s)", ("main", constants.PARSER_VERSION))
        c.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, "
            "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
            "parser_version) VALUES (%s, 'p', 's', FALSE, 'e', 1, now(), "
            "now(), %s)", ("zzz-subagent", constants.PARSER_VERSION))
        # The parent's original call and the fork's replay, the fork's
        # key FIRST — the order the old file_key rule would have picked.
        for fk, replay in (("zzz-subagent", True), ("main", False)):
            c.execute(
                "INSERT INTO tool_uses (file_key, line_num, idx, ts, "
                "tool_name, model, tool_use_id, is_replay) "
                "VALUES (%s, 1, 0, now(), 'exec', 'gpt-5.6-sol', "
                "'call-replay-1', %s)", (fk, replay))
        c.commit()

    ingest.recompute_canonical()

    with db.viz_conn() as c:
        rows = dict(c.execute(
            "SELECT file_key, is_canonical FROM tool_uses "
            "WHERE tool_use_id = 'call-replay-1'").fetchall())
    assert rows == {"main": True, "zzz-subagent": False}


def test_a_replayed_tool_call_adopts_the_originals_model(fresh_db):
    """The adoption (issue #713) is keyed on tool_use_id too: a replayed
    fork call stored with the fork's first declaration takes the
    original's model, like its record does."""
    with db.viz_conn() as c:
        c.execute("INSERT INTO projects (project_id, display_name, "
                  "first_seen_at, last_seen_at) VALUES ('p', 'p', now(), now())")
        c.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, "
            "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
            "parser_version) VALUES (%s, 'p', 's', TRUE, 'e', 1, now(), "
            "now(), %s)", ("main", constants.PARSER_VERSION))
        c.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, "
            "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
            "parser_version) VALUES (%s, 'p', 's', FALSE, 'e', 1, now(), "
            "now(), %s)", ("zzz-subagent", constants.PARSER_VERSION))
        # The parent's original call and the fork's replay under the
        # fork's first declared model — the shape the pass corrects.
        for fk, replay, model in (("zzz-subagent", True, "gpt-5.6-terra"),
                                  ("main", False, "gpt-5.6-sol")):
            c.execute(
                "INSERT INTO tool_uses (file_key, line_num, idx, ts, "
                "tool_name, model, tool_use_id, is_replay) "
                "VALUES (%s, 1, 0, now(), 'exec', %s, "
                "'call-replay-1', %s)", (fk, model, replay))
        c.commit()

    ingest.recompute_canonical()

    with db.viz_conn() as c:
        rows = dict(c.execute(
            "SELECT file_key, model FROM tool_uses "
            "WHERE tool_use_id = 'call-replay-1'").fetchall())
    assert rows == {"main": "gpt-5.6-sol", "zzz-subagent": "gpt-5.6-sol"}


def test_a_null_original_model_is_never_adopted(fresh_db):
    """A winner model of NULL is not a model in force — the replayed copy
    keeps its own attribution instead of losing it (the adoption's
    `model IS NOT NULL` limb; records.model is NOT NULL, so the limb is
    reachable on tool_uses only)."""
    with db.viz_conn() as c:
        c.execute("INSERT INTO projects (project_id, display_name, "
                  "first_seen_at, last_seen_at) VALUES ('p', 'p', now(), now())")
        for fk, is_main in (("main", True), ("zzz-subagent", False)):
            c.execute(
                "INSERT INTO files (file_key, project_id, session_id, is_main, "
                "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
                "parser_version) VALUES (%s, 'p', 's', %s, 'e', 1, now(), "
                "now(), %s)", (fk, is_main, constants.PARSER_VERSION))
        c.execute(
            "INSERT INTO tool_uses (file_key, line_num, idx, ts, tool_name, "
            "model, tool_use_id, is_replay) VALUES "
            "('main', 1, 0, now(), 'exec', NULL, 'call-replay-1', NULL), "
            "('zzz-subagent', 3, 0, now(), 'exec', 'gpt-5.6-terra', "
            "'call-replay-1', TRUE)")
        c.commit()

    ingest.recompute_canonical()

    with db.viz_conn() as c:
        rows = dict(c.execute(
            "SELECT file_key, model FROM tool_uses "
            "WHERE tool_use_id = 'call-replay-1'").fetchall())
    assert rows == {"main": None, "zzz-subagent": "gpt-5.6-terra"}


def _claude_copy(model: str | None, output_tokens: int) -> str:
    """One Claude assistant line: model omitted entirely when None, which
    the #688 refusal rejects at parse_file."""
    message = {"role": "assistant",
               "usage": {"input_tokens": 100,
                         "cache_creation_input_tokens": 0,
                         "cache_read_input_tokens": 0,
                         "output_tokens": output_tokens}}
    if model is not None:
        message["model"] = model
    return json.dumps(
        {"type": "assistant", "timestamp": "2026-05-07T10:00:00Z",
         "uuid": "u-shared", "requestId": "req-1", "sessionId": "sessU",
         "message": message}, separators=(",", ":")) + "\n"


def test_a_claude_modelless_copy_is_refused(
        fresh_db, monkeypatch, tmp_path):
    """The Claude path used to store a model-less line as the `(unknown)`
    fallback (issue #688 removed it); the shape that exercised that rank
    member live — a model-less Claude copy in the file_key-first position
    the old rule would have picked — is now REFUSED at parse, storing no
    rows, while the attributed copy of the same uuid in the same run
    stores and stays canonical."""
    root = tmp_path / "mirror"
    for proj, model, out in (
            ("aaa-proj", None, 200),                    # -> refused
            ("zzz-proj", "claude-sonnet-4-5", 300)):
        d = root / "mini" / proj / "sessU"
        d.mkdir(parents=True)
        (d / "sessU.jsonl").write_text(_claude_copy(model, out))
    monkeypatch.setenv("R2_ENDPOINT", root.as_uri() + "/")
    monkeypatch.setenv("R2_BUCKET", "mini")

    result = ingest.run_ingest(trigger="manual")
    assert result["failed"] == 1
    assert result["error"] == "1 object failed after retries"

    with db.viz_conn() as c:
        rows = c.execute(
            """
            SELECT file_key, model, is_canonical
              FROM records WHERE uuid = 'u-shared'
             ORDER BY file_key
            """,
        ).fetchall()
        stored_row = c.execute("SELECT COUNT(*) FROM files").fetchone()
        assert stored_row is not None
        stored = stored_row[0]
    assert rows == [("mini/zzz-proj/sessU/sessU.jsonl",
                     "claude-sonnet-4-5", True)]
    assert stored == 1


def _seed_two_identities() -> None:
    """Two original/replay pairs on different uuids, canonical flags as a
    real ingest leaves them: the originals win, the replays lose."""
    with db.viz_conn() as c:
        c.execute("INSERT INTO projects (project_id, display_name, "
                  "first_seen_at, last_seen_at) VALUES ('p', 'p', now(), now())")
        for fk, is_main in (("main1", True), ("fork1", False),
                            ("main2", True), ("fork2", False)):
            c.execute(
                "INSERT INTO files (file_key, project_id, session_id, is_main, "
                "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
                "parser_version) VALUES (%s, 'p', 's', %s, 'e', 1, now(), "
                "now(), %s)", (fk, is_main, constants.PARSER_VERSION))
        c.execute(
            "INSERT INTO records (file_key, line_num, uuid, model, "
            "is_replay, is_canonical) VALUES "
            "('main1', 1, 'u1', 'gpt-5.6-sol', NULL, TRUE), "
            "('fork1', 3, 'u1', 'gpt-5.6-terra', TRUE, FALSE), "
            "('main2', 1, 'u2', 'gpt-5.6-sol', NULL, TRUE), "
            "('fork2', 3, 'u2', 'gpt-5.6-terra', TRUE, FALSE)")
        c.commit()


def test_an_incremental_scope_adopts_only_its_own_uuids(fresh_db):
    """The adoption's incremental limb (issue #713): a scoped run adopts
    only the uuids the run touched — the out-of-scope replayed copy keeps
    the #653 fallback until a run touches its identity."""
    _seed_two_identities()
    scope = Scope(False, "test", 2000)
    scope.affected_uuids = {"u1"}

    ingest.recompute_canonical(scope)

    with db.viz_conn() as c:
        rows = dict(c.execute(
            "SELECT file_key, model FROM records WHERE uuid IN ('u1', 'u2') "
            "AND is_replay IS TRUE").fetchall())
    assert rows == {"fork1": "gpt-5.6-sol", "fork2": "gpt-5.6-terra"}

    # The full pass picks up what the scoped run left.
    ingest.recompute_canonical()
    with db.viz_conn() as c:
        rows = dict(c.execute(
            "SELECT file_key, model FROM records WHERE uuid IN ('u1', 'u2') "
            "AND is_replay IS TRUE").fetchall())
    assert rows == {"fork1": "gpt-5.6-sol", "fork2": "gpt-5.6-sol"}


def test_two_replays_without_an_original_keep_their_declarations(fresh_db):
    """Every copy of the uuid is a replay (the parent was never ingested):
    no original means no model in force to adopt, so the loser keeps its
    own first declaration instead of adopting the winner-replay's."""
    with db.viz_conn() as c:
        c.execute("INSERT INTO projects (project_id, display_name, "
                  "first_seen_at, last_seen_at) VALUES ('p', 'p', now(), now())")
        for fk, is_main in (("aaa-fork", True), ("zzz-fork", False)):
            c.execute(
                "INSERT INTO files (file_key, project_id, session_id, is_main, "
                "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
                "parser_version) VALUES (%s, 'p', 's', %s, 'e', 1, now(), "
                "now(), %s)", (fk, is_main, constants.PARSER_VERSION))
        # Both copies replayed; the winner rule made the aaa copy canonical.
        c.execute(
            "INSERT INTO records (file_key, line_num, uuid, model, "
            "is_replay, is_canonical) VALUES "
            "('aaa-fork', 3, 'u3', 'gpt-5.6-sol', TRUE, TRUE), "
            "('zzz-fork', 3, 'u3', 'gpt-5.6-terra', TRUE, FALSE)")
        c.commit()

    ingest.recompute_canonical()

    with db.viz_conn() as c:
        rows = dict(c.execute(
            "SELECT file_key, model FROM records WHERE uuid = 'u3'").fetchall())
    assert rows == {"aaa-fork": "gpt-5.6-sol", "zzz-fork": "gpt-5.6-terra"}
