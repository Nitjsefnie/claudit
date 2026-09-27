"""The reprice's PROOF: reprice == full reparse (issue #193).

A rate change must rePRICE stored records — recompute cost_usd from the
stored token columns — instead of re-parsing every R2 object. The proof
is parity: repriced rows must equal what a full reparse of the same
bytes under the same rate tables yields. This module holds the
mirror/parity block (split from test_reprice.py, which outgrew the
SV-CI-RATCHETS test ceiling when the issue #249 coverage landed): the
lane and Claude-format blobs, the proof mirror, and
test_reprice_matches_full_reparse, whose flag parity covers the
reprice's re-derivation (issue #194) and the issue #249 fix — a
Claude-format record from a bare meter model keeps the parse-stored
NULL flag and a flat cost across the reprice.
"""
from __future__ import annotations

import json
import lzma
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture,
    _mini_r2_env_fixture,
)

from test_reprice import _pair

from backend import constants, db, ingest, parse, pricing

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIX_ROOT = _REPO_ROOT / "fixtures"

UTC = timezone.utc

# Rates deliberately unlike any real price, so an assertion against them
# can never be mistaken for a pricing fact (the same rule the conftest
# synthetic-rate fixtures follow).
_WINDOW_RATES = {"fresh": 1.0, "create_5m": 1.25, "create_1h": 2.0,
                 "read": 0.1, "output": 5.0}
_OPUS_RATES = {"fresh": 2.0, "create_5m": 2.5, "create_1h": 4.0,
               "read": 0.2, "output": 10.0}
_ASTRA_RATES = {"fresh": 3.0, "create_5m": 3.75, "create_1h": 6.0,
                "read": 0.3, "output": 15.0}


def _lane_blob(session_id: str, *, plan_type: str | None) -> bytes:
    """One codex rollout: a gpt-5.6-sol request at 300k fresh / 1k output,
    above the REAL 272k threshold, so the reprice's re-derivation must
    agree with the reparse in both plan shapes — under the test's
    threshold=1 patch and without it alike."""
    usage = {
        "input_tokens": 300_000, "cached_input_tokens": 0,
        "cache_write_input_tokens": 0, "output_tokens": 1_000,
        "reasoning_output_tokens": 0, "total_tokens": 301_000,
    }
    lines = [
        {"timestamp": "2026-09-01T12:00:00.000Z", "type": "session_meta",
         "payload": {"session_id": session_id}},
        {"timestamp": "2026-09-01T12:00:01.000Z", "type": "turn_context",
         "payload": {"model": "gpt-5.6-sol"}},
        {"timestamp": "2026-09-01T12:00:02.000Z", "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"total_token_usage": usage,
                              "last_token_usage": usage},
                     # plan_type rides rate_limits on every token_count of
                     # a subscription rollout; the API shape names none.
                     **({"rate_limits": {"plan_type": plan_type}}
                        if plan_type else {})}},
    ]
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


def _claude_blob() -> bytes:
    """One Claude-format transcript: a plain prompt, then one assistant
    line from the bare long-context model id gpt-6-sol with no provider
    and a 300k-fresh tally — above the REAL threshold, so the reprice's
    non-derivation (issue #249) must keep what the parse stored (NULL
    flag, flat cost), the same values a reparse stores."""
    lines = [
        {"type": "user", "timestamp": "2026-09-01T12:00:00.000Z",
         "uuid": "u249", "sessionId": "iss249-sess",
         "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "timestamp": "2026-09-01T12:00:05.000Z",
         "uuid": "a249", "requestId": "req-249",
         "sessionId": "iss249-sess", "parentUuid": "u249",
         "message": {"id": "msg_249", "type": "message", "role": "assistant",
                     "model": "gpt-6-sol",
                     "content": [{"type": "text", "text": "ok"}],
                     "stop_reason": "end_turn",
                     "usage": {"input_tokens": 300000,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0,
                               "output_tokens": 72}}},
    ]
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


def _proof_mirror(tmp_path) -> tuple[Path, dict[str, bytes]]:
    """The mini mirror plus three codex lane files, copied into
    tmp_path; returns (bucket, {stored file_key: plain blob}) for the
    lane files (whose stored keys keep the .xz suffix the tree omits)."""
    bucket = tmp_path / "r2" / "claude"
    shutil.copytree(_REPO_ROOT / "fixtures/r2_mini/claude", bucket)
    codex_blob = (_FIX_ROOT / "parser" / "codex_min.jsonl").read_bytes()
    lane_blobs = {
        "sessions/8805b8ac99ad/01a0-uuid/wire.jsonl.xz": codex_blob,
        # The same tally twice, once per plan shape: "pro" on every
        # token_count (the subscription rollout) and no rate_limits at
        # all (the API shape).
        "sessions/8805b8ac99ad/01b1-sub/wire.jsonl.xz": _lane_blob(
            "00000000-0000-4000-8000-0000000000b1", plan_type="pro"),
        "sessions/8805b8ac99ad/01c2-api/wire.jsonl.xz": _lane_blob(
            "00000000-0000-4000-8000-0000000000c2", plan_type=None),
    }
    for rel, blob in lane_blobs.items():
        path = bucket / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(lzma.compress(blob))
    claude_rel = "issue249/iss249-sess/iss249-sess.jsonl"
    claude_blob = _claude_blob()
    claude_path = bucket / claude_rel
    claude_path.parent.mkdir(parents=True, exist_ok=True)
    claude_path.write_bytes(claude_blob)
    lane_blobs[claude_rel] = claude_blob
    return bucket, {f"claude/{rel}": blob for rel, blob in lane_blobs.items()}


def _proof_blobs(bucket: Path, lane_blobs: dict[str, bytes]) -> dict:
    """{file_key: plain blob bytes} for every mirrored transcript."""
    blobs = {f"claude/{p.relative_to(bucket).as_posix()}": p.read_bytes()
             for p in sorted(bucket.rglob("*.jsonl"))}
    # The stored keys keep the .xz suffix; only the bytes r2 fetches
    # are decompressed.
    blobs.update(lane_blobs)
    return blobs


def _reparse_costs(blobs: dict) -> dict:
    """{(file_key, line_num): cost_usd} that a full reparse of the same
    bytes yields, under whatever the rate tables are right now."""
    expected = {}
    for file_key, blob in blobs.items():
        for rec in parse.parse_file(file_key, blob)["records"]:
            expected[(file_key, rec["line_num"])] = rec["cost_usd"]
    return expected


def _stored_costs() -> dict:
    """{(file_key, line_num): cost_usd} of every stored record."""
    with db.viz_conn() as c:
        return {(fk, ln): float(cost) for fk, ln, cost in c.execute(
            "SELECT file_key, line_num, cost_usd FROM records").fetchall()}


def _stored_rows() -> list:
    """(file_key, line_num, cost_usd, pricing_version) of every record."""
    with db.viz_conn() as c:
        return c.execute(
            "SELECT file_key, line_num, cost_usd, pricing_version "
            "FROM records").fetchall()


def test_reprice_matches_full_reparse(fresh_db, tmp_path, monkeypatch):
    """THE PROOF (issue #193): reprice == full reparse. Ingest the mini
    mirror plus codex lane files, MUTATE the loaded rate tables, bump
    PRICING_VERSION, run the reprice — then every stored cost_usd must
    equal what parse_file yields for the same fixture bytes under the
    same mutated tables.

    Shapes exercised here: exact-key rows (claude-opus-4-7,
    gpt-6-astra), a dated window (claude-sonnet-4-5, end 2099 so it
    covers every fixture timestamp) and the long-context meter: three
    codex lane files — the small fixture plus two 300k-fresh rollouts,
    one per plan shape ("pro" on every token_count, and no rate_limits)
    — whose flag parity now includes the reprice's re-derivation (issue
    #194). The threshold patch to 1 stays only so the small fixture
    parses TRUE; the two meter-shaped files sit above the REAL 272k, so
    the reprice (re-derivation) and the reparse agree for both plan
    shapes. A Claude-format file from the bare meter model gpt-6-sol
    with no provider (issue #249): its record keeps long_context NULL
    and a flat cost across the reprice, so reprice equals reparse for
    the shape the pass used to flip. The provider-row and
    weekly-schedule shapes have no
    fixture-backed record; the seeded tests price them through the same
    compute_cost call the parse path makes — parity by construction,
    and still a check on the pass's column mapping.
    """
    bucket, lane_blobs = _proof_mirror(tmp_path)
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")
    # Threshold 1: the small codex fixture parses long_context=TRUE; the
    # two 300k rollouts clear the REAL threshold either way, and the
    # claude rows re-derive nothing (their models carry no meter, and
    # the Claude-format row's stored flag is NULL — issue #249).
    monkeypatch.setattr(pricing, "LONG_CONTEXT_THRESHOLD", 1)
    assert ingest.run_ingest(trigger="manual")["error"] is None

    with db.viz_conn() as c:
        total, long_marked = _pair(
            c, "SELECT COUNT(*), COUNT(*) FILTER (WHERE long_context) "
               "FROM records")
    assert total > 0
    assert 0 < long_marked < total, (
        "the codex records must store long_context and the claude ones "
        "must not, or the long-context shape is not exercised")

    blobs = _proof_blobs(bucket, lane_blobs)

    # Mutate the loaded tables the fixtures price under: two exact keys
    # and a dated window covering every fixture timestamp (end 2099).
    monkeypatch.setattr(pricing, "MODEL_RATES", {
        **pricing.MODEL_RATES,
        "claude-opus-4-7": _OPUS_RATES,
        "gpt-6-astra": _ASTRA_RATES,
    })
    monkeypatch.setattr(pricing, "DATED_RATES", {
        **pricing.DATED_RATES,
        "claude-sonnet-4-5": [(datetime(2099, 1, 1, tzinfo=UTC),
                               _WINDOW_RATES)],
    })
    before = _stored_costs()

    # One past the tree's own version (derived, issue #198): the refresh
    # workflow bumps PRICING_VERSION itself, so a literal bump target can
    # collide with the working tree and turn the patch into a no-op — and
    # the mirror ingest stamps the tree's real value, so every row is
    # stale whatever the tree carries.
    next_version = str(int(constants.PRICING_VERSION) + 1)
    monkeypatch.setattr(constants, "PRICING_VERSION", next_version)
    assert ingest.reprice_stale() == total

    expected = _reparse_costs(blobs)
    rows = _stored_rows()
    assert len(rows) == len(expected), (
        "every stored row must have a reparse counterpart")
    # (file_key, line_num, cost_usd, pricing_version), indexed: keeping
    # the loop's local count under pylint's gate for a test this size.
    moved = 0
    for row in rows:
        assert row[3] == next_version, "a repriced row carries the new version"
        assert (row[0], row[1]) in expected
        assert float(row[2]) == pytest.approx(
            expected[(row[0], row[1])], abs=1e-9)
        moved += float(row[2]) != before[(row[0], row[1])]
    assert moved > 0, (
        "the mutated rates must actually move costs, or the parity "
        "assertion proves nothing")

    with db.viz_conn() as c:
        assert c.execute(
            "SELECT long_context, COUNT(*) FROM records "
            "WHERE file_key LIKE 'claude/sessions/%' GROUP BY 1"
        ).fetchall() == [(True, 3)], (
            "the reprice re-derives the codex rows' flag in both plan "
            "shapes (issue #194): every codex record stores TRUE")
        assert c.execute(
            "SELECT COUNT(*) FROM records WHERE file_key LIKE "
            "'claude/issue249/%' AND long_context IS NULL"
        ).fetchall() == [(1,)], (
            "the reprice keeps a Claude-format bare-meter-model record's NULL "
            "flag (issue #249): a reparse stores NULL, so reprice must too")
