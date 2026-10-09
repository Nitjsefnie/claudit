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
from psycopg import sql

# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture,
    _mini_r2_env_fixture,
)

from test_reprice import _pair
from tests import scratch_db

from backend import (
    constants,
    db,
    ingest,
    parse,
    pricing,
    rate_fingerprint,
)

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
    and a 300k-fresh tally — above the REAL threshold, so both the parse
    (issue #765's derivation) and the reprice store the metered shape,
    the same values a reparse stores."""
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


def _assert_reprice_parity(blobs: dict, before: dict, next_version: str,
                           repriced: int) -> None:
    """Every stored row equals the reparse of the same bytes under the
    loaded tables, a repriced row carries the new version, and the
    pass's changed count is exactly the rows whose cost moved (issue
    #339: tallies that price identically under the mutated tables are
    restamped, not rewritten)."""
    expected = _reparse_costs(blobs)
    rows = _stored_rows()
    assert len(rows) == len(expected), (
        "every stored row must have a reparse counterpart")
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
    assert repriced == moved, (
        "the pass's changed count must be exactly the rows whose cost "
        "moved under the mutated tables")


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
    with no provider (issue #249): the parse path itself now derives the
    meter decision for a member above its threshold (issue #765), and
    the reprice re-derives the same from the stored columns — reprice
    equals reparse for the shape the pass used to flip. The provider-row and
    weekly-schedule shapes have no
    fixture-backed record; the seeded tests price them through the same
    compute_cost call the parse path makes — parity by construction,
    and still a check on the pass's column mapping.
    """
    bucket, lane_blobs = _proof_mirror(tmp_path)
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")
    # Threshold 1: the small codex fixture parses long_context=TRUE; the
    # two 300k rollouts clear the REAL threshold either way, and the
    # claude rows carry no meter decision (non-members), and the
    # Claude-format member row's flag comes from the parse (issue #765).
    monkeypatch.setattr(pricing, "LONG_CONTEXT_THRESHOLD", 1)
    # The membership is the test's own (SV-TEST-DATA): exactly the
    # bare meter model, so the claude fixture rows stay non-members
    # whatever the live fold does, and no per-model threshold stands
    # between the patch above and the codex re-derivation.
    monkeypatch.setattr(pricing, "LONG_CONTEXT_MODELS",
                        frozenset({"gpt-6-sol"}))
    monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS", {})
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

    # Mutate the loaded tables the fixtures price under: the claude row is
    # still a models-table key, while the two vendor keys moved at the
    # migration — their bare ids price through their (key, vendor_host)
    # rows — so the mutation lands on those tables. The dated window ends
    # 2099, covering every fixture timestamp.
    monkeypatch.setattr(pricing, "MODEL_RATES", {
        **pricing.MODEL_RATES,
        "claude-opus-4-7": _OPUS_RATES,
    })
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {
        **pricing.PROVIDER_RATES,
        ("gpt-6-astra", pricing.VENDOR_HOSTS["gpt-6-astra"]): _ASTRA_RATES,
    })
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {
        **pricing.PROVIDER_DATED_RATES,
        ("claude-sonnet-4-5", pricing.VENDOR_HOSTS["claude-sonnet-4-5"]): [
            (datetime(2099, 1, 1, tzinfo=UTC), _WINDOW_RATES)],
    })
    # The fingerprints the mirror ingest just stamped came from the
    # pre-mutation tables (rate_fingerprint memoizes per pair; the
    # production tables are immutable at runtime, but this test is not
    # production). Empty the memo so the pass fingerprints the pairs
    # under the mutated tables — the same discipline the fingerprint
    # tests follow of clearing both caches around a table patch.
    rate_fingerprint.clear_fingerprint_cache()
    before = _stored_costs()

    # One past the tree's own version (derived, issue #198): the refresh
    # workflow bumps PRICING_VERSION itself, so a literal bump target can
    # collide with the working tree and turn the patch into a no-op — and
    # the mirror ingest stamps the tree's real value, so every row is
    # stale whatever the tree carries.
    next_version = str(int(constants.PRICING_VERSION) + 1)
    monkeypatch.setattr(constants, "PRICING_VERSION", next_version)
    # Issue #339: the count is rows whose rate-derived data moved, not
    # every stale row — tallies that price identically under the mutated
    # tables (zero-token rows) are restamped, not rewritten. The proof
    # below asserts the count equals the rows the parity diff observes
    # moving.
    repriced = ingest.reprice_stale()

    _assert_reprice_parity(blobs, before, next_version, repriced)

    with db.viz_conn() as c:
        assert c.execute(
            "SELECT long_context, COUNT(*) FROM records "
            "WHERE file_key LIKE 'claude/sessions/8805b8ac99ad/%' "
            "GROUP BY 1"
        ).fetchall() == [(True, 3)], (
            "the reprice re-derives the codex rows' flag in both plan "
            "shapes (issue #194): every codex record stores TRUE")
        assert c.execute(
            "SELECT long_context, COUNT(*) FROM records "
            "WHERE file_key LIKE 'claude/issue249/%' GROUP BY 1"
        ).fetchall() == [(True, 1)], (
            "the Claude-format bare-meter-model record stores the parse's "
            "decision (issue #765) and the reprice re-derives the same: "
            "reprice equals reparse (issue #249's law)")


# Rates deliberately unlike any real price (SV-TEST-DATA). E2 prices the
# post-cutover side through the list row; CHEAP376 is the window the
# reprice pass must consult only BEFORE the cutover.
_E2_376 = {"fresh": 9.0, "create_5m": 11.25, "create_1h": 18.0,
           "read": 0.9, "output": 45.0}
_CHEAP376 = {"fresh": 0.5, "create_5m": 0.625, "create_1h": 1.0,
             "read": 0.05, "output": 2.5}
_OPUS376 = "claude-opus-4-7"
_CUTOVER_376 = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)


def test_reprice_matches_reparse_for_offset_less_timestamps(
        fresh_db, tmp_path, monkeypatch):
    """Issue #376: an offset-less record timestamp is UTC — the SAME
    instant is priced and stored — so a reprice of the stored row prices
    what a reparse of the raw text prices, even in a non-UTC session
    zone with a rate cutover between the two interpretations. Before the
    parse-side normalisation the parser priced the naive text as UTC but
    the driver stored it in the session zone; after a PRICING_VERSION
    bump the reprice then stored the cutover's other side, which no
    reparse reproduces."""
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")
    sess = tmp_path / "r2" / "claude" / "issue376" / "sess376"
    sess.mkdir(parents=True)
    sess.joinpath("sess376.jsonl").write_bytes(
        b'{"type":"user","timestamp":"2026-09-10T00:29:00","uuid":"u376",'
        b'"message":{"role":"user","content":"hi"}}\n'
        b'{"type":"assistant","timestamp":"2026-09-10T00:30:00",'
        b'"uuid":"a376","requestId":"req-376",'
        b'"message":{"id":"msg_376","role":"assistant","model":'
        b'"claude-opus-4-7","content":[{"type":"text","text":"naive"}],'
        b'"stop_reason":"end_turn",'
        b'"usage":{"input_tokens":10,"output_tokens":1,'
        b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n')
    # The session zone is non-UTC BEFORE the ingest: the naive-ts bug is
    # a session-zone interpretation, invisible in a UTC test cluster.
    with scratch_db.admin_connection() as admin:
        admin.execute(sql.SQL(
            "ALTER DATABASE {} SET timezone = 'Europe/Berlin'").format(
                sql.Identifier(fresh_db)))
    db.reset_viz_pool()
    assert ingest.run_ingest(trigger="manual")["error"] is None

    with db.viz_conn() as c:
        stored_utc_wall = c.execute(
            "SELECT ts AT TIME ZONE 'UTC' FROM records").fetchall()
    assert stored_utc_wall == [(datetime(2026, 9, 10, 0, 30),)], (
        f"the stored instant must be the UTC reading of the raw text: "
        f"{stored_utc_wall}")

    # Move the pair AFTER the ingest (the #249 route: a rate change with
    # its bump), so the reprice pass must RECOMPUTE from the stored
    # instant instead of proving the row clean by fingerprint: a window
    # appears that ends at the cutover, and the list row moves with it.
    # claude-opus-4-7 is a tracked vendor key since the migration: the
    # pair its bare path reads is (key, vendor_host), so the window and
    # the list move land there.
    opus376_row = (_OPUS376, pricing.VENDOR_HOSTS[_OPUS376])
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {
        **pricing.PROVIDER_DATED_RATES,
        opus376_row: [(_CUTOVER_376, _CHEAP376)]})
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {
        **pricing.PROVIDER_RATES,
        opus376_row: _E2_376})
    rate_fingerprint.clear_fingerprint_cache()

    expected = _reparse_costs({
        "claude/issue376/sess376/sess376.jsonl":
            sess.joinpath("sess376.jsonl").read_bytes()})
    assert list(expected.values()) == [round(pricing.compute_cost(
        _OPUS376, fresh=10, output=1, eph5=0, eph1h=0, unsplit_create=0,
        read=0, ts=datetime(2026, 9, 10, 0, 30, tzinfo=UTC),
        adjustments=pricing.CostAdjustments(long_context=False)), 6)]

    before = _stored_costs()
    next_version = str(int(constants.PRICING_VERSION) + 1)
    monkeypatch.setattr(constants, "PRICING_VERSION", next_version)
    assert ingest.reprice_stale() == 1, (
        "the moved pair must actually reprice, or the parity assertion "
        "proves nothing")

    stored = _stored_costs()
    assert stored == expected, (
        "the reprice must store what a reparse of the same bytes prices "
        "(issue #376)")
    assert stored != before, (
        "the window must move the cost, or the cutover sits between the "
        "two interpretations")
    with db.viz_conn() as c:
        assert c.execute(
            "SELECT COUNT(*) FROM records WHERE pricing_version = %s",
            (next_version,)).fetchall() == [(1,)]
