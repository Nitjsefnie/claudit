"""parse.py — per-file extraction.

Each parse_file call returns:
  - records: one entry per assistant_usage AFTER within-file requestId
    max-merge. Cross-file uuid dedup happens at query time, not here.
  - ctx_turns: per-turn (idx, ts, line, input, output, delta) array
    (SV-PARSER-SPEC).

Cost is precomputed per record using pricing.MODEL_RATES so the read
path doesn't need to JOIN against rates.
"""
from pathlib import Path

import pytest

from backend import constants, parse


FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"


def _read(name):
    return (FIX / name).read_bytes()


def test_single_turn_emits_one_record_one_turn():
    out = parse.parse_file("k/sess-1/sess-1.jsonl", _read("single_turn.jsonl"))
    assert len(out["records"]) == 1
    r = out["records"][0]
    assert r["uuid"] == "a1"
    assert r["request_id"] == "req-1"
    assert r["model"] == "claude-sonnet-4-5"
    assert r["fresh_tokens"] == 100
    assert r["output_tokens"] == 200
    assert r["cost_usd"] == pytest.approx(100/1e6 * 3.00 + 200/1e6 * 15.00)
    assert len(out["ctx_turns"]) == 1
    t = out["ctx_turns"][0]
    assert t["idx"] == 1
    assert t["input"] == 100
    assert t["output"] == 200
    assert t["delta"] == 100   # first turn delta == input


def test_streaming_within_file_max_merges_per_request_id():
    """Two records same requestId, output_tokens=50 then 200.
    Phase 1 keeps ONE record with max-merged usage."""
    out = parse.parse_file(
        "k/sess-2/sess-2.jsonl", _read("streaming_merge.jsonl")
    )
    assert len(out["records"]) == 1
    assert out["records"][0]["output_tokens"] == 200
    assert out["records"][0]["fresh_tokens"] == 100


def test_unsplit_cache_charged_at_1h_rate():
    out = parse.parse_file(
        "k/sess-5/sess-5.jsonl", _read("unsplit_cache.jsonl")
    )
    r = out["records"][0]
    # cache_creation_tokens=1M, eph5=0, eph1h=0 → unsplit=1M → cost via 1h rate
    # Sonnet-4-5 1h rate is $6.00/MTok → expect $6.00
    assert r["cache_creation_tokens"] == 1_000_000
    assert r["eph5_tokens"] == 0
    assert r["eph1h_tokens"] == 0
    assert r["cost_usd"] == pytest.approx(6.00, rel=1e-9)


def test_ttl_split_charges_each_bucket():
    out = parse.parse_file(
        "k/sess-6/sess-6.jsonl", _read("ttl_split.jsonl")
    )
    r = out["records"][0]
    # eph5=1M @ $3.75 + eph1h=1M @ $6.00 = $9.75
    assert r["eph5_tokens"] == 1_000_000
    assert r["eph1h_tokens"] == 1_000_000
    assert r["cost_usd"] == pytest.approx(9.75, rel=1e-9)


def test_ctx_turns_match_canonical_shape():
    """Each turn is {idx, ts, line, input, output, delta} where:
      input  = fresh + cache_creation + cache_read
      output = output_tokens
      delta  = this_input - previous_input
    (SV-PARSER-SPEC)."""
    out = parse.parse_file(
        "k/sess-1/sess-1.jsonl", _read("single_turn.jsonl")
    )
    t = out["ctx_turns"][0]
    assert set(t.keys()) == {"idx", "ts", "line", "input", "output", "delta"}


def test_legacy_record_no_uuid_no_request_id():
    """Record with no uuid and no requestId still gets stored
    (Phase 1 dedup is by requestId, so empty requestId means each
    record stays distinct)."""
    blob = (
        b'{"type":"assistant","timestamp":"2026-05-07T10:00:01Z",'
        b'"message":{"role":"assistant","model":"claude-sonnet-4-5",'
        b'"content":[{"type":"text","text":"x"}],'
        b'"usage":{"input_tokens":10,"output_tokens":5,'
        b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
    )
    out = parse.parse_file("k/sess-x/sess-x.jsonl", blob)
    assert len(out["records"]) == 1
    assert out["records"][0]["uuid"] is None
    assert out["records"][0]["request_id"] == ""


def test_two_records_no_request_id_both_kept():
    """Without requestId the per-file Phase 1 merge can't dedup, so two
    records stay as two records."""
    blob = (
        b'{"type":"assistant","timestamp":"2026-05-07T10:00:01Z","uuid":"a",'
        b'"message":{"role":"assistant","model":"claude-sonnet-4-5",'
        b'"content":[{"type":"text","text":"x"}],'
        b'"usage":{"input_tokens":10,"output_tokens":5,'
        b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
        b'{"type":"assistant","timestamp":"2026-05-07T10:00:02Z","uuid":"b",'
        b'"message":{"role":"assistant","model":"claude-sonnet-4-5",'
        b'"content":[{"type":"text","text":"x"}],'
        b'"usage":{"input_tokens":10,"output_tokens":5,'
        b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
    )
    out = parse.parse_file("k/sess-x/sess-x.jsonl", blob)
    assert len(out["records"]) == 2


def test_empty_request_id_merges_on_message_id():
    """Z.ai-served transcripts carry no requestId (absent or ""), but
    every content-block line of one API message shares message.id. Those
    lines max-merge exactly like a requestId group: ONE record at the
    first line, the usage and stop_reason the closing line carried, the
    reply latency the first line consumed. request_id stays the stored
    '' — the fallback is a merge key, not a column value."""
    out = parse.parse_file(
        "zai/p/sess-m/sess-m.jsonl", _read("message_id_merge.jsonl")
    )
    assert len(out["records"]) == 1
    r = out["records"][0]
    assert r["line_num"] == 2
    assert r["request_id"] == ""
    assert (r["fresh_tokens"], r["cache_read_tokens"], r["output_tokens"]) \
        == (100, 50, 20)
    assert r["stop_reason"] == "tool_use"
    assert r["reply_latency_s"] == pytest.approx(5.0)
    assert r["text_chars"] == 2
    # The tool call keeps the line it was written on, as for a
    # requestId group.
    assert [(t["line_num"], t["tool_use_id"]) for t in out["tool_uses"]] \
        == [(3, "t1")]
    assert [(t["line"], t["input"]) for t in out["ctx_turns"]] == [(2, 150)]


def test_empty_request_id_distinct_message_ids_stay_separate():
    """The fallback keys on message.id, so two messages stay two records."""
    line = (
        '{{"type":"assistant","timestamp":"2026-05-07T10:00:0{n}Z",'
        '"message":{{"id":"msg_{n}","role":"assistant","model":"m",'
        '"content":[],"usage":{{"input_tokens":1,"output_tokens":1}}}}}}\n'
    )
    blob = (line.format(n=1) + line.format(n=2)).encode()
    out = parse.parse_file("zai/p/s/s.jsonl", blob)
    assert [r["line_num"] for r in out["records"]] == [1, 2]


def test_tool_use_matched_to_error_result():
    """A tool_use with a later tool_result is_error:true on the same
    tool_use_id → tool_uses entry has is_error=True."""
    out = parse.parse_file(
        "k/sess-err/sess-err.jsonl", _read("tool_error.jsonl")
    )
    assert len(out["tool_uses"]) == 1
    tu = out["tool_uses"][0]
    assert tu["tool_name"] == "Bash"
    assert tu["is_error"] is True


def test_tool_use_matched_to_success_result():
    """A tool_use with a later tool_result is_error:false → is_error=False."""
    out = parse.parse_file(
        "k/sess-ok/sess-ok.jsonl", _read("tool_success.jsonl")
    )
    assert len(out["tool_uses"]) == 1
    assert out["tool_uses"][0]["is_error"] is False


def test_tool_use_unmatched_stays_null():
    """A tool_use with NO later tool_result in the file → is_error=None."""
    out = parse.parse_file(
        "k/sess-pending/sess-pending.jsonl", _read("tool_unmatched.jsonl")
    )
    assert len(out["tool_uses"]) == 1
    assert out["tool_uses"][0]["is_error"] is None


def test_iterations_flattened_to_sum():
    """Multi-iteration usage: top-level fields are partial snapshots.
    Parser must sum across iterations for billing tokens."""
    out = parse.parse_file(
        "k/sess-it/sess-it.jsonl", _read("iterations_flatten.jsonl")
    )
    assert len(out["records"]) == 1
    r = out["records"][0]
    # Top-level in fixture: fresh=2, create=3690, read=232289, output=292
    # Sum across iterations: fresh=118735, create=3690, read=232289, output=5396
    assert r["fresh_tokens"] == 118735
    assert r["cache_creation_tokens"] == 3690
    assert r["cache_read_tokens"] == 232289
    assert r["output_tokens"] == 5396
    # eph1h = 2623 + 1067 = 3690, eph5 = 0
    assert r["eph1h_tokens"] == 3690
    assert r["eph5_tokens"] == 0


def test_prompt_count_excludes_instrumentation_and_interrupts():
    """prompt_count tracks substantive user text only — bash-IO blobs,
    command stubs, and interrupt markers don't count as prompts."""
    blob = (
        b'{"type":"user","timestamp":"2026-05-07T10:00:00Z","uuid":"u1",'
        b'"message":{"role":"user","content":"real prompt"}}\n'
        b'{"type":"user","timestamp":"2026-05-07T10:00:01Z","uuid":"u2",'
        b'"message":{"role":"user","content":"<bash-input>ls</bash-input>"}}\n'
        b'{"type":"user","timestamp":"2026-05-07T10:00:02Z","uuid":"u3",'
        b'"message":{"role":"user","content":"<command-name>foo</command-name>"}}\n'
        b'{"type":"user","timestamp":"2026-05-07T10:00:03Z","uuid":"u4",'
        b'"message":{"role":"user","content":"[Request interrupted by user]"}}\n'
        b'{"type":"user","timestamp":"2026-05-07T10:00:04Z","uuid":"u5",'
        b'"message":{"role":"user","content":"another real prompt"}}\n'
    )
    out = parse.parse_file("k/sess-p/sess-p.jsonl", blob)
    assert out["prompt_count"] == 2


def test_user_text_lines_drive_turn_boundaries():
    """compute_context_growth uses user_text lines as turn boundaries.
    Within one turn (between two user_text lines), the LAST
    assistant_usage is the turn's representative.
    Build a fixture: 1 user msg → 2 assistant_usage → 1 user msg →
    1 assistant_usage. Expect 2 ctx_turns."""
    blob = (
        b'{"type":"user","timestamp":"2026-05-07T10:00:00Z","uuid":"u1",'
        b'"message":{"role":"user","content":"first"}}\n'
        b'{"type":"assistant","timestamp":"2026-05-07T10:00:01Z","uuid":"a1",'
        b'"requestId":"r1",'
        b'"message":{"role":"assistant","model":"claude-sonnet-4-5",'
        b'"content":[{"type":"text","text":"x"}],'
        b'"usage":{"input_tokens":50,"output_tokens":1,'
        b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
        b'{"type":"assistant","timestamp":"2026-05-07T10:00:02Z","uuid":"a2",'
        b'"requestId":"r2",'
        b'"message":{"role":"assistant","model":"claude-sonnet-4-5",'
        b'"content":[{"type":"text","text":"x"}],'
        b'"usage":{"input_tokens":100,"output_tokens":2,'
        b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
        b'{"type":"user","timestamp":"2026-05-07T10:00:03Z","uuid":"u2",'
        b'"message":{"role":"user","content":"second"}}\n'
        b'{"type":"assistant","timestamp":"2026-05-07T10:00:04Z","uuid":"a3",'
        b'"requestId":"r3",'
        b'"message":{"role":"assistant","model":"claude-sonnet-4-5",'
        b'"content":[{"type":"text","text":"x"}],'
        b'"usage":{"input_tokens":200,"output_tokens":3,'
        b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
    )
    out = parse.parse_file("k/sess-x/sess-x.jsonl", blob)
    # Two user_text lines → two turns. Each turn's representative is the
    # LAST assistant_usage before the next user line.
    assert len(out["ctx_turns"]) == 2
    assert out["ctx_turns"][0]["input"] == 100  # a2 wins turn 1
    assert out["ctx_turns"][0]["output"] == 2
    assert out["ctx_turns"][1]["input"] == 200  # a3 wins turn 2
    assert out["ctx_turns"][1]["delta"] == 100  # 200 - 100


def test_mixed_dated_and_undated_records_sort_undated_first():
    """An undated usage row sorts before aware timestamps without a
    naive/aware comparison crash."""
    out = parse.parse_file(
        "k/sess-mixed/sess-mixed.jsonl", _read("mixed_timestamps.jsonl")
    )
    assert [(t["ts"], t["input"]) for t in out["ctx_turns"]] == [
        ("", 10),
        ("2026-05-07T10:00:02+00:00", 20),
    ]


def test_reply_latency_terminated_by_list_form_interrupt():
    """List-form interrupt content must terminate the reply-latency window,
    not be ignored. Regression: bogus ~1h latency outlier after interrupts."""
    out = parse.parse_file(
        "k/sess-interrupt/sess-interrupt.jsonl",
        _read("interrupt_list_content.jsonl"),
    )
    assert len(out["records"]) == 1
    assert out["records"][0]["reply_latency_s"] is None
    assert out["prompt_count"] == 1  # interrupt must not count as a prompt


def test_reply_latency_anchored_by_list_form_user_text():
    """Genuine user text in list-form content anchors the latency window
    and counts toward prompt_count."""
    out = parse.parse_file(
        "k/sess-list/sess-list.jsonl",
        _read("list_content_anchor.jsonl"),
    )
    assert len(out["records"]) == 1
    assert out["records"][0]["reply_latency_s"] == pytest.approx(10.0)
    assert out["prompt_count"] == 1


def test_reply_latency_ignores_replayed_prompt():
    """A verbatim re-dispatched user record (same uuid, different line)
    must not anchor reply-latency windows. Regression: bogus ~25m outliers
    on re-dispatched subagent transcripts."""
    out = parse.parse_file(
        "k/sess-rp/sess-rp.jsonl", _read("replayed_prompt.jsonl")
    )
    assert len(out["records"]) == 2
    assert out["records"][0]["reply_latency_s"] == pytest.approx(5.0)
    assert out["records"][1]["reply_latency_s"] is None
    assert out["prompt_count"] == 2


def test_reply_latency_terminated_by_synthetic_error_reply():
    """A `<synthetic>` API-error assistant record (e.g. "Login expired")
    is an assistant-side response: it TERMINATES the latency window even
    though it carries no billable usage and so gets no `records` row.
    Regression: the anchor survived until the session resumed hours later,
    producing a bogus 2.5h reply-latency outlier."""
    out = parse.parse_file(
        "k/sess-synth/sess-synth.jsonl",
        _read("synthetic_error_terminator.jsonl"),
    )
    assert len(out["records"]) == 1
    assert out["records"][0]["reply_latency_s"] is None


def test_reply_latency_terminated_by_rate_limit_reply():
    """A rate-limit assistant record terminates the window for the same
    reason: it is the assistant responding, just not with usage."""
    out = parse.parse_file(
        "k/sess-rlt/sess-rlt.jsonl",
        _read("rate_limit_terminator.jsonl"),
    )
    assert len(out["records"]) == 1
    assert out["records"][0]["reply_latency_s"] is None
    assert len(out["rate_limit_hits"]) == 1


def test_rate_limit_detection_is_shape_based_not_wording_based():
    """Every `error: "rate_limit"` record is a hit EXCEPT the transient
    per-minute 429. Regression: the detector allow-listed the single
    wording "out of extra usage", so when Claude Code switched to
    "You've hit your weekly limit" the hits stopped being recorded —
    silently, with the old wording still matching so nothing looked
    broken. Live corpus: no hit booked after 2026-05-06 despite a real
    weekly limit on 2026-08-09."""
    out = parse.parse_file(
        "k/sess-rlw/sess-rlw.jsonl", _read("rate_limit_weekly.jsonl")
    )
    hits = out["rate_limit_hits"]
    # 1 weekly cap, 3 legacy "out of extra usage", 4 session cap.
    # 2 transient 429, 5 model-unavailable, 6 proxy 429 are NOT caps.
    assert [h["line"] for h in hits] == [1, 3, 4]
    assert "weekly limit" in hits[0]["content"]
    assert "out of extra usage" in hits[1]["content"]
    assert "session limit" in hits[2]["content"]


def test_edit_call_yields_added_deleted_counts():
    """Edit churn comes from the CALL arguments (old_string/new_string),
    not the result text, DIFFED so context lines repeated in both
    payloads are not churn: "a\\nb\\n" → "a\\nb\\nc\\nd\\n" adds 2 and
    deletes 0. Two separate positive series (issue #10)."""
    out = parse.parse_file(
        "k/sess-edit/sess-edit.jsonl", _read("edit_churn.jsonl")
    )
    assert len(out["tool_uses"]) == 1
    tu = out["tool_uses"][0]
    assert tu["tool_name"] == "Edit"
    assert tu["is_error"] is False
    assert tu["lines_added"] == 2
    assert tu["lines_deleted"] == 0


def test_write_call_counts_whole_content_as_added():
    """A Write carries no old content in the call, so it is all
    additions; 'x\\ny\\nz' (no trailing newline) is 3 lines."""
    out = parse.parse_file(
        "k/sess-write/sess-write.jsonl", _read("write_churn.jsonl")
    )
    tu = out["tool_uses"][0]
    assert tu["tool_name"] == "Write"
    assert tu["lines_added"] == 3
    assert tu["lines_deleted"] == 0


def test_errored_edit_contributes_zero_churn():
    """An errored Edit changed nothing on disk — its churn is zeroed
    once the tool_result resolves is_error=True."""
    out = parse.parse_file(
        "k/sess-editerr/sess-editerr.jsonl", _read("edit_error.jsonl")
    )
    tu = out["tool_uses"][0]
    assert tu["is_error"] is True
    assert tu["lines_added"] == 0
    assert tu["lines_deleted"] == 0


def test_non_edit_tool_has_no_churn():
    """A tool call with nothing enumerable in it reports 0/0 — `ls`
    writes no lines, and no other tool name carries a payload we count."""
    out = parse.parse_file(
        "k/sess-ok/sess-ok.jsonl", _read("tool_success.jsonl")
    )
    tu = out["tool_uses"][0]
    assert tu["tool_name"] == "Bash"
    assert tu["lines_added"] == 0
    assert tu["lines_deleted"] == 0


def test_dated_rate_prices_each_record_at_its_own_timestamp(synthetic_dated_rate):
    # A record on either side of a dated cutover must be priced by when it
    # was spent, not by when the file is parsed. Driven through conftest's
    # synthetic window: the live DATED_RATES table is empty (Sonnet 5's
    # launch price became its standard price), so nothing real straddles a
    # cutover, but ingest must still honour per-record timestamps.
    w = synthetic_dated_rate
    out = parse.parse_file("k/sess-d/sess-d.jsonl", _read("dated_rate_sonnet5.jsonl"))
    before, after = out["records"][0], out["records"][1]
    assert before["cost_usd"] == pytest.approx(w.before["fresh"])
    assert after["cost_usd"] == pytest.approx(w.after["fresh"])


def test_sonnet_5_is_priced_flat_at_its_standard_rate():
    # Same fixture, live rate table: both records price at 2.00/MTok.
    out = parse.parse_file("k/sess-d/sess-d.jsonl", _read("dated_rate_sonnet5.jsonl"))
    assert [r["cost_usd"] for r in out["records"]] == [
        pytest.approx(2.00), pytest.approx(2.00),
    ]


def test_bash_heredoc_call_yields_added_counts():
    """A heredoc redirected into a file is a write like any other: the
    body's 3 lines are additions, and nothing is known to be deleted."""
    out = parse.parse_file(
        "k/sess-bash/sess-bash.jsonl", _read("bash_churn.jsonl")
    )
    tu = out["tool_uses"][0]
    assert tu["tool_name"] == "Bash"
    assert tu["is_error"] is False
    assert tu["lines_added"] == 3
    assert tu["lines_deleted"] == 0


def test_agent_type_from_attribution_agent():
    """A dispatched agent's transcript carries attributionAgent."""
    out = parse.parse_file(
        "k/sess-1/subagents/agent-a1.jsonl", _read("agent_attribution.jsonl"))
    assert out["agent_type"] == "implementer"


def test_agent_type_from_cli_agent_setting():
    """A `claude --agent X` session records a type:"agent-setting" line
    instead — a separate signal, not attributionAgent."""
    out = parse.parse_file("k/sess-1/sess-1.jsonl", _read("agent_setting.jsonl"))
    assert out["agent_type"] == "code-reviewer"


def test_agent_type_prefers_attribution_over_agent_setting():
    """Precedence, in the (unobserved) case both are present: the
    dispatch attribution beats the session's startup flag."""
    out = parse.parse_file(
        "k/sess-1/sess-1.jsonl", _read("agent_attribution_wins.jsonl"))
    assert out["agent_type"] == "adversary"


def test_agent_type_defaults_when_transcript_records_no_role():
    """A transcript carrying neither signal is unattributable. It could
    be a plain lead, a lead started with an explicit --agent flag, or a
    subagent predating Claude Code 2.1.126 — the file cannot tell them
    apart, so all three land in the one honest bucket."""
    out = parse.parse_file("k/sess-1/sess-1.jsonl", _read("single_turn.jsonl"))
    assert out["agent_type"] == parse.DEFAULT_AGENT_TYPE == "general-purpose"


def test_non_string_agent_setting_falls_back_to_the_default():
    """agentSetting is transcript data, so its type is not guaranteed.
    Dropping the isinstance guard resolves this file to the int 42 —
    a bar labelled 42, and a non-TEXT value handed to the files insert.
    (An EMPTY name is deliberately not the case tested here: the `or`
    in resolve_agent_type covers that independently, so no single
    defect makes such a test fail.)"""
    out = parse.parse_file(
        "k/sess-1/sess-1.jsonl", _read("agent_setting_nonstring.jsonl"))
    assert out["agent_type"] == "general-purpose"


def test_context_beyond_any_window_is_not_a_turn():
    """A record whose context exceeds MAX_PLAUSIBLE_CTX is a cumulative
    counter, not a request, and must not enter the ctx_turns trace.

    Found live: a one-line file in the `zai` bucket, written by another
    harness under the all-zeros sentinel session id, reported 115.8M
    cache-read tokens in a single record. No request reads that — the
    largest published window is 1M — but the Context Growth y-axis is
    scaled off the maximum, so that one row flattened every real trace on
    glmmeter to a hairline.

    The RECORD is still parsed and still priced. Only the ctx trace,
    whose axis the value destroys, rejects it.
    """
    out = parse.parse_file(
        "k/sess-1/sess-1.jsonl", _read("ctx_cumulative_counter.jsonl"))
    assert not out["ctx_turns"]
    assert len(out["records"]) == 1, "the record itself is still kept"


def test_a_full_million_token_window_is_still_a_turn():
    """The bound must not clip a legitimate [1m] request. Guards against
    setting it at or below the real 1M window."""
    assert constants.MAX_PLAUSIBLE_CTX > 1_000_000


def test_error_kind_classifies_settled_failures():
    """is_error rows carry a coarse, harness-generic error_kind and the
    leading text of the result; successful rows carry neither."""
    out = parse.parse_file(
        "k/sess-kind/sess-kind.jsonl", _read("error_kinds.jsonl")
    )
    by_idx = {tu["idx"]: tu for tu in out["tool_uses"]}
    assert by_idx[0]["error_kind"] == "failed"
    assert "PreToolUse hook" in by_idx[0]["error_text"]
    assert by_idx[1]["error_kind"] == "rejected"
    assert by_idx[2]["error_kind"] == "tool_error"
    assert by_idx[3]["error_kind"] is None
    assert by_idx[3]["error_text"] is None


def test_error_text_is_truncated():
    """error_text never exceeds ERROR_TEXT_MAX characters."""
    for tu in parse.parse_file(
        "k/sess-kind/sess-kind.jsonl", _read("error_kinds.jsonl")
    )["tool_uses"]:
        if tu["error_text"] is not None:
            assert len(tu["error_text"]) <= parse.ERROR_TEXT_MAX


def test_error_text_carries_no_nul_byte():
    """A failed tool_result that read binary content puts a NUL in the
    result text. PostgreSQL text columns cannot hold one, so a NUL that
    survives parsing aborts the whole ingest transaction and leaves every
    derived rollup unbuilt (issue #41). The classification and the
    readable part of the message must survive the stripping.
    """
    out = parse.parse_file(
        "k/sess-nul/sess-nul.jsonl", _read("nul_in_error_text.jsonl")
    )
    tu = out["tool_uses"][0]
    assert tu["error_kind"] is not None
    assert "\x00" not in tu["error_text"]
    assert "binary junk" in tu["error_text"]


def test_dispatch_briefing_shape_captured():
    """A dispatch records how it was briefed: prompt length, and whether
    the opening directive points at a written brief file instead of
    carrying the instructions inline.

    The late-path case is the one that matters -- a prompt mentioning a
    .md path only after BRIEF_REF_SCAN carries its brief inline and
    happens to cite a file, which is not the same thing as delegating
    to one.
    """
    out = parse.parse_file(
        "k/sess-b/sess-b.jsonl", _read("dispatch_brief_shape.jsonl")
    )
    by_idx = {tu["idx"]: tu for tu in out["tool_uses"]}

    assert by_idx[0]["dispatch_brief_ref"] is True
    assert by_idx[0]["dispatch_prompt_chars"] > 0

    assert by_idx[1]["dispatch_brief_ref"] is False
    assert by_idx[1]["dispatch_prompt_chars"] == 188

    assert by_idx[2]["dispatch_brief_ref"] is False, \
        "a path beyond the scan window is not a brief reference"

    # A dispatch with no prompt argument has no briefing shape at all,
    # which is distinct from having one that is inline.
    assert by_idx[3]["dispatch_brief_ref"] is None
    assert by_idx[3]["dispatch_prompt_chars"] is None

    # A non-dispatch tool never carries them, even naming a .md path.
    assert by_idx[4]["tool_name"] == "Bash"
    assert by_idx[4]["dispatch_brief_ref"] is None


def test_agent_dispatch_args_captured():
    """An Agent call records subagent_type/model from its arguments;
    a dispatch that names neither records NULLs, and a non-Agent tool
    never carries them."""
    out = parse.parse_file(
        "k/sess-d/sess-d.jsonl", _read("agent_dispatch.jsonl")
    )
    by_idx = {tu["idx"]: tu for tu in out["tool_uses"]}
    assert by_idx[0]["agent_type"] == "Explore"
    assert by_idx[0]["agent_model"] == "haiku"
    assert by_idx[1]["agent_type"] is None
    assert by_idx[1]["agent_model"] is None
    assert by_idx[2]["tool_name"] == "Bash"
    assert by_idx[2]["agent_type"] is None


def test_reread_flag_marks_only_the_redundant_whole_read():
    """cat, cat again, grep, edit, cat: only the second call added
    nothing new to the context."""
    out = parse.parse_file(
        "k/sess-reread/sess-reread.jsonl", _read("reread.jsonl")
    )
    tus = out["tool_uses"]
    assert [tu["is_reread"] for tu in tus] == [False, True, None, None, False]


def test_reread_records_read_kind_and_targets():
    out = parse.parse_file(
        "k/sess-reread/sess-reread.jsonl", _read("reread.jsonl")
    )
    tus = out["tool_uses"]
    first, sliced, edited = tus[0], tus[2], tus[3]
    assert first["read_kind"] == "whole"
    assert first["read_targets"] == ["/repo/n.md"]
    assert sliced["read_kind"] == "slice"
    assert sliced["read_targets"] == ["/repo/n.md"]
    # The Edit names what it CHANGED, which is what invalidates the
    # pending re-read; it reads nothing.
    assert edited["read_targets"] == []
    assert edited["write_targets"] == ["/repo/n.md"]


def test_result_chars_counts_every_settled_call():
    out = parse.parse_file(
        "k/sess-reread/sess-reread.jsonl", _read("reread.jsonl")
    )
    assert [tu["result_chars"] for tu in out["tool_uses"]] == [
        len("alpha"), len("alpha"), len("1:alpha"), len("ok"), len("delta"),
    ]


def test_result_chars_counts_an_image_payload():
    """Image results are 92% of duplicated read bytes over the live
    corpus; a text-only measure would report the cheapest half."""
    assert parse._result_size(  # pylint: disable=protected-access
        [{"type": "image", "source": {"data": "Q" * 40}}]
    ) == 40


def test_errored_read_neither_flags_nor_marks_seen():
    """A failed read returned an error, not the file — so it wasted
    nothing AND leaves the next read of that file un-flagged."""
    rows = [
        {"read_kind": "whole", "read_targets": ["/a"], "write_targets": [],
         "is_error": True, "is_reread": None},
        {"read_kind": "whole", "read_targets": ["/a"], "write_targets": [],
         "is_error": False, "is_reread": None},
    ]
    parse._resolve_rereads(rows)  # pylint: disable=protected-access
    assert [r["is_reread"] for r in rows] == [None, False]


def test_partial_overlap_is_not_a_reread():
    """`cat a b` after reading only `a` still brought `b` in."""
    rows = [
        {"read_kind": "whole", "read_targets": ["/a"], "write_targets": [],
         "is_error": False, "is_reread": None},
        {"read_kind": "whole", "read_targets": ["/a", "/b"],
         "write_targets": [], "is_error": False, "is_reread": None},
    ]
    parse._resolve_rereads(rows)  # pylint: disable=protected-access
    assert [r["is_reread"] for r in rows] == [False, False]


def test_errored_compound_bash_keeps_the_heredoc_write():
    """`cat > f <<EOF … EOF` followed by `python3 f` that exits 1: the
    result is an error, but the exit status is the LAST stage's and the
    heredoc landed on disk before it ran. Zeroing here hides the write
    that most editing under bypass permissions goes through."""
    out = parse.parse_file(
        "k/sess-hd/sess-hd.jsonl", _read("bash_heredoc_error.jsonl")
    )
    tu = out["tool_uses"][0]
    assert tu["is_error"] is True
    assert tu["lines_added"] == 2
    assert tu["lines_deleted"] == 0
    assert tu["write_targets"] == ["/repo/scripts/gen.py"]


@pytest.mark.parametrize("family,paths,churn", [
    ("printf", ["/work/probe.txt"], (2, 0)),
    ("copy", ["/work/dst.txt"], (1, 0)),
    ("brace_words", ["/work/{dest}"], (1, 0)),
    ("install", ["/work/dst.dump"], (1, 0)),
    ("move", ["/work/README.md"], (1, 0)),
    ("fd_redirects", ["/work/README.md"], (1, 0)),
    ("heredoc_override", ["/work/out.txt"], (1, 0)),
    ("perl", ["/work/file.ts"], (1, 0)),
    ("sed_append", ["/work/.gitignore"], (3, 0)),
    ("python_loop", ["/work/a.md", "/work/b.md"], (1, 0)),
    ("python_concat", ["/work/README.md"], (1, 0)),
])
def test_bash_recovered_tool_fields(family, paths, churn):
    out = parse.parse_file("k/s/s.jsonl", _read(f"bash_{family}.jsonl"))
    tool = out["tool_uses"][0]
    assert tool["write_targets"] == paths
    assert (tool["lines_added"], tool["lines_deleted"]) == churn


@pytest.mark.parametrize("family", ["move", "fd_redirects"])
def test_new_bash_write_invalidates_reread(family):
    out = parse.parse_file("k/s/s.jsonl", _read(f"bash_{family}.jsonl"))
    read = {"read_kind": "whole", "read_targets": ["/work/README.md"],
            "write_targets": [], "is_error": False, "is_reread": None}
    rows = [read.copy(), out["tool_uses"][0], read.copy()]
    parse._resolve_rereads(rows)  # pylint: disable=protected-access
    assert [row["is_reread"] for row in rows] == [False, None, False]


def test_new_bash_payload_is_zeroed_on_error():
    data = _read("bash_printf.jsonl").replace(
        b'"content":"ok"', b'"content":"Exit code 1","is_error":true')
    tool = parse.parse_file("k/s/s.jsonl", data)["tool_uses"][0]
    assert tool["is_error"] is True
    assert (tool["lines_added"], tool["lines_deleted"]) == (0, 0)


def test_bash_exit_marker():
    tool = parse.parse_file(
        "k/s/s.jsonl", _read("bash_exit_marker.jsonl"))["tool_uses"][0]
    assert tool["is_error"] is False
    assert tool["write_targets"] == ["/tmp/v3run.out"]
    assert (tool["lines_added"], tool["lines_deleted"]) == (1, 0)


@pytest.mark.parametrize("result", [
    "Exit code 1", "PreToolUse hook denied this call",
])
def test_bash_exit_marker_is_zeroed_on_error(result):
    data = _read("bash_exit_marker.jsonl").replace(
        b'"content":"ok"',
        f'"content":"{result}","is_error":true'.encode())
    tool = parse.parse_file("k/s/s.jsonl", data)["tool_uses"][0]
    assert tool["is_error"] is True
    assert (tool["lines_added"], tool["lines_deleted"]) == (0, 0)


def test_nonzero_exit_is_a_tool_error_not_a_failed_launch():
    """`Exit code N` is the harness's wrapper for a Bash command that
    RAN and failed — 58% of all errored results in a recent sample. In
    `failed` it is indistinguishable from a hook denial, which is the
    one distinction error_kind exists to draw."""
    out = parse.parse_file(
        "k/sess-exit/sess-exit.jsonl", _read("bash_exit_kinds.jsonl")
    )
    by_idx = {tu["idx"]: tu for tu in out["tool_uses"]}
    assert by_idx[0]["error_kind"] == "tool_error"
    assert by_idx[1]["error_kind"] == "failed"
    assert by_idx[2]["error_kind"] == "rejected"


def test_a_call_that_never_ran_wrote_nothing():
    """A hook denial or a user rejection stops the call before the
    shell sees it: its write targets must not invalidate a later
    re-read, because the bytes did not change."""
    out = parse.parse_file(
        "k/sess-exit/sess-exit.jsonl", _read("bash_exit_kinds.jsonl")
    )
    by_idx = {tu["idx"]: tu for tu in out["tool_uses"]}
    assert by_idx[1]["write_targets"] == []
    assert by_idx[2]["write_targets"] == []
    assert by_idx[1]["lines_added"] == 0


@pytest.mark.parametrize("family,expected,paths", [
    ("redirect", (1, 0), ["/work/out.txt"]),
    ("echo", (1, 0), ["/work/out.txt"]),
    ("python", (1, 0), []),
    ("sed", (1, 1), ["/work/out.txt"]),
])
def test_bash_write_estimate_fixtures(family, expected, paths):
    tool = parse.parse_file("k/s/s.jsonl", _read("bash_estimate_" + family + ".jsonl"))["tool_uses"][0]
    assert (tool["lines_added"], tool["lines_deleted"]) == expected
    assert tool["write_targets"] == paths


@pytest.mark.parametrize("result", ["Exit code 1", "PreToolUse hook denied this call", "User rejected tool use"])
@pytest.mark.parametrize("family", ["redirect", "echo", "python", "sed"])
def test_bash_write_estimates_remain_zero_on_error(family, result):
    data = _read("bash_estimate_" + family + ".jsonl").replace(
        b'"content":"ok"', ('"content":"' + result + '","is_error":true').encode())
    tool = parse.parse_file("k/s/s.jsonl", data)["tool_uses"][0]
    assert (tool["lines_added"], tool["lines_deleted"]) == (0, 0)


@pytest.mark.parametrize("family,expected,paths", [
    ("destination_null", (0, 0), []),
    ("cat_empty", (0, 0), ["/work/out.txt"]),
    ("helper_empty", (0, 0), ["/work/out.txt"]),
    ("edit_discarded", (0, 0), ["/work/out.txt"]),
    ("edit_overwritten", (0, 0), ["/work/out.txt"]),
    ("edit_written", (2, 1), ["/work/out.txt"]),
    ("helper_rebind", (1, 0), ["/work/out.txt"]),
    ("helper_deferred", (0, 0), []),
    ("copy_empty", (0, 0), ["/work/out.txt"]),
    ("perl_unknown", (1, 0), []),
])
def test_bash_review_write_estimate_fields(family, expected, paths):
    tool = parse.parse_file("k/s/s.jsonl", _read("bash_review_" + family + ".jsonl"))["tool_uses"][0]
    assert (tool["lines_added"], tool["lines_deleted"]) == expected
    assert tool["write_targets"] == paths


def test_windows_bash_write_invalidates_raw_read_without_rewriting_paths():
    tools = parse.parse_file("k/s/s.jsonl", _read("windows_reread.jsonl"))["tool_uses"]
    assert [row["is_reread"] for row in tools] == [False, None, False]
    assert tools[0]["read_targets"] == ["C:/w/f.py"]
    assert tools[1]["write_targets"] == [r"C:\w\f.py"]
    assert tools[2]["read_targets"] == ["C:/w/f.py"]
    assert (tools[1]["lines_added"], tools[1]["lines_deleted"]) == (1, 1)


def test_windows_copy_preserves_write_estimate():
    tool = parse.parse_file("k/s/s.jsonl", _read("windows_copy.jsonl"))["tool_uses"][0]
    assert tool["write_targets"] == [r"C:\w\out\f.py"]
    assert (tool["lines_added"], tool["lines_deleted"]) == (1, 0)


@pytest.mark.parametrize("first,second,expected", [
    ('C:\\Work\\f.py', '\\\\?\\C:\\Work\\f.py', False),
    (r"C:\Work\f.py", "c:/Work/f.py", True),
    ("C:/Work/./f.py", r"C:\Work\f.py", True),
    (r"C:\Work\f.py", r"C:\Work\F.py", False),
    (r"C:\Work\f.py", "/c/Work/f.py", False),
    ("/work/f.py", "/work/F.py", False),
    ("/work/./f.py", "/work/f.py", False),
    (r"\\server\share\x\..\f.py", r"\\server\share\f.py", True),
    ("//server/share/f.py", r"\\server\share\f.py", False),
    (r"\\?\C:\Work\.\f.py", r"\\?\C:\Work\f.py", False),
])
def test_rereads_compare_supported_windows_spellings_only(first, second, expected):
    rows = [{"read_kind": "whole", "read_targets": [path], "write_targets": [], "is_error": False, "is_reread": None}
            for path in (first, second)]
    parse._resolve_rereads(rows)  # pylint: disable=protected-access
    assert rows[1]["is_reread"] is expected
    assert [row["read_targets"] for row in rows] == [[first], [second]]


@pytest.mark.parametrize("name", ["Write", "Edit", "NotebookEdit"])
def test_raw_windows_write_invalidates_equivalent_read_without_rewriting(name):
    first, written = "C:/Work/f.py", r"c:\Work\f.py"
    kind, reads, writes = parse._tool_access(name, {"file_path": written}, r"C:\Work")  # pylint: disable=protected-access
    rows = [{"read_kind": "whole", "read_targets": [first], "write_targets": [], "is_error": False, "is_reread": None},
            {"read_kind": kind, "read_targets": reads, "write_targets": writes, "is_error": False, "is_reread": None},
            {"read_kind": "whole", "read_targets": [first], "write_targets": [], "is_error": False, "is_reread": None}]
    parse._resolve_rereads(rows)  # pylint: disable=protected-access
    assert [row["is_reread"] for row in rows] == [False, None, False]
    assert rows[1]["write_targets"] == [written]


@pytest.mark.parametrize("namespace", ["drive", "unc"])
def test_verbatim_copy_target_invalidates_raw_read(namespace):
    tools = parse.parse_file("k/s/s.jsonl", _read("windows_verbatim_" + namespace + ".jsonl"))["tool_uses"]
    assert [row["is_reread"] for row in tools] == [False, None, False]
    assert tools[0]["read_targets"] == tools[1]["write_targets"] == tools[2]["read_targets"]
    assert (tools[1]["lines_added"], tools[1]["lines_deleted"]) == (1, 0)


def test_stop_reason_effort_thinking_merge_per_request():
    """The closing line of a streamed reply carries stop_reason; the
    opening line carries effort and a placeholder output count. One
    record per requestId keeps the closing stop_reason, the first
    effort, and the max thinking_tokens. A request that never closes
    keeps stop_reason NULL, which is how the output undercount is found."""
    out = parse.parse_file(
        "k/sess-sr/sess-sr.jsonl", _read("stop_reason_merge.jsonl")
    )
    assert len(out["records"]) == 2
    closed, unclosed = out["records"]
    assert closed["stop_reason"] == "end_turn"
    assert closed["effort"] == "high"
    assert closed["thinking_tokens"] == 120
    assert closed["output_tokens"] == 300
    assert unclosed["stop_reason"] is None
    assert unclosed["effort"] is None
    assert unclosed["thinking_tokens"] == 0


def test_tool_use_id_kept_on_tool_uses():
    """tool_use_id survives error resolution: ingest dedups sidecar
    replays on it (tool_uses.is_canonical)."""
    out = parse.parse_file(
        "k/sess-err/sess-err.jsonl", _read("tool_error.jsonl")
    )
    assert out["tool_uses"][0]["tool_use_id"] == "toolu_01"


def test_turn_flags_attach_window_events_to_next_request():
    """Events between two requests land on the NEXT request: a blocking
    Stop hook, a date rollover and a typed prompt; then tool results
    (with an image), a /model command, and the version/effort deltas
    detected against the previous request. A request with an empty
    window carries nothing, and a user line of tool results alone is
    not a prompt."""
    out = parse.parse_file("k/sess-tf/sess-tf.jsonl", _read("turn_flags.jsonl"))
    first, second, third = out["records"]
    assert first["turn_flags"] == ["date_change", "stop_hook_block", "user_prompt"]
    assert first["turn_tool_results"] == 0
    assert first["cli_version"] == "2.1.250"
    assert second["turn_flags"] == [
        "effort_switch", "image_result", "model_switch", "version_switch"]
    assert second["turn_tool_results"] == 2
    assert second["cli_version"] == "2.1.251"
    assert third["turn_flags"] == []
    assert third["turn_tool_results"] == 0


def test_prompt_snapshot_flags_land_on_the_request_they_describe():
    """A prompt_snapshot attachment is written AFTER the response it
    describes, so its flags are backfilled onto the previous record, not
    folded forward. The preamble snapshot without a tool list is ignored,
    and the first snapshot with tools is only the baseline, so r1 carries
    just the process start (resume) and the typed prompt; r2 gets
    tools_change from the snapshot that follows it."""
    out = parse.parse_file("k/sess-ps/sess-ps.jsonl", _read("prompt_snapshot.jsonl"))
    first, second = out["records"]
    assert first["turn_flags"] == ["resume", "user_prompt"]
    assert first["turn_tool_results"] == 0
    assert first["cli_version"] == "2.1.272"
    assert second["turn_flags"] == ["tools_change"]
    assert second["turn_tool_results"] == 1


def test_prompt_rerender_and_system_change_backfill_with_resume():
    """The snapshot after r2 changes only the system prompt (system_change);
    r2 also opened a turn on a cwd that moved since r1 (cwd_rebuild,
    alongside the plain cwd_switch). A session_context before r3 marks a
    resume, and its identical snapshot is a prompt_rerender."""
    out = parse.parse_file("k/sess-pr/sess-pr.jsonl", _read("prompt_rerender.jsonl"))
    first, second, third = out["records"]
    assert first["turn_flags"] == ["user_prompt"]
    assert second["turn_flags"] == [
        "cwd_rebuild", "cwd_switch", "system_change", "user_prompt"]
    assert third["turn_flags"] == ["prompt_rerender", "resume", "user_prompt"]


def test_cwd_rebuild_marks_only_the_turn_that_reopens_on_a_new_cwd():
    """The record cwd follows the shell cwd, but the system prompt only
    re-resolves at a turn boundary: r2 moved mid-turn (cwd_switch only),
    and r3 opens a turn on a cwd different from the previous turn start
    (cwd_rebuild without cwd_switch, since r2 already sat there)."""
    out = parse.parse_file("k/sess-cr/sess-cr.jsonl", _read("cwd_rebuild.jsonl"))
    first, second, third = out["records"]
    assert first["turn_flags"] == ["user_prompt"]
    assert second["turn_flags"] == ["cwd_switch"]
    assert second["turn_tool_results"] == 1
    assert third["turn_flags"] == ["cwd_rebuild", "user_prompt"]
    assert third["turn_tool_results"] == 0


def test_models_lists_every_model_in_the_file():
    out = parse.parse_file("k/sess-sr/sess-sr.jsonl", _read("stop_reason_merge.jsonl"))
    assert out["models"] == ["claude-sonnet-4-5"]


def test_openrouter_provider_is_stored_and_prices_the_record():
    """An OpenRouter assistant line names its serving host as
    message.provider; the record keeps it and is priced from that host's
    row. A line without the field stores NULL and prices by the model
    alone, exactly as before the provider table existed."""
    out = parse.parse_file("k/s/s.jsonl", _read("openrouter_provider.jsonl"))
    novita, bare = out["records"]
    assert novita["provider"] == "Novita"
    assert bare["provider"] is None
    # Novita's deepseek-v4.1-flash row: 0.285 in / 0.0057 read / 1.14 out.
    assert novita["cost_usd"] == pytest.approx(
        round((1000 * 0.285 + 2000 * 0.0057 + 300 * 1.14) / 1e6, 6))
    # No provider: DEFAULT (Opus 4.7 list, 5 / 0.50 / 25), as before.
    assert bare["cost_usd"] == pytest.approx(
        round((1000 * 5.00 + 2000 * 0.50 + 300 * 25.00) / 1e6, 6))
