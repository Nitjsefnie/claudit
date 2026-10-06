# Fixture-driven parser tests live here 1:1 per testing convention; the module size is governed by the size baseline (scripts/ci/size_baseline.py), not pylint's flat module limit.
"""parse.py — per-file extraction.

Returns requestId max-merged records and per-turn ctx_turns.
Cross-file uuid dedup happens at query time; costs come from
pricing.MODEL_RATES.
"""
from pathlib import Path
from datetime import datetime, timezone

import pytest

from backend import parse


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


def test_naive_timestamp_is_stored_and_priced_as_utc():
    """An offset-less timestamp is UTC (issue #376): the same instant is
    priced and stored, so a reprice of the stored row prices what a
    reparse of the raw text prices. Before the normalisation the parser
    handed a naive datetime to pricing (UTC) AND to the driver (the
    session zone), so the two could disagree across a rate cutover."""
    out = parse.parse_file(
        "k/sess-naive/sess-naive.jsonl", _read("naive_timestamp.jsonl")
    )
    assert len(out["records"]) == 1
    assert out["records"][0]["ts"] == datetime(
        2026, 9, 10, 0, 30, tzinfo=timezone.utc)


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


def test_bash_mkdir_checkout_yields_directory_write_targets():
    """mkdir, git worktree add and a bare git clone name what they put
    on disk, resolved against the command's cwd."""
    out = parse.parse_file(
        "k/sess-mkc/sess-mkc.jsonl", _read("bash_mkdir_checkout.jsonl"))
    assert [tu["write_targets"] for tu in out["tool_uses"]] == [
        ["/tmp/wt", "/tmp/wt/issue200"],
        ["/work/y"],
    ]


def test_bash_mktemp_template_is_a_write_target():
    """A mktemp template is a named path, kept verbatim."""
    out = parse.parse_file(
        "k/sess-mkt/sess-mkt.jsonl", _read("bash_mktemp.jsonl"))
    assert out["tool_uses"][0]["write_targets"] == ["/tmp/probe.XXXXXX"]


def test_bash_captured_mktemp_template_is_a_write_target():
    """A captured substitution's mktemp books its template like the bare form."""
    out = parse.parse_file(
        "k/sess-mktc/sess-mktc.jsonl", _read("bash_mktemp_capture.jsonl"))
    assert out["tool_uses"][0]["write_targets"] == ["/tmp/probe.XXXXXX"]


def test_bash_quoted_capture_paren_template_is_a_write_target():
    """#741: the double-quoted capture tokenizes its body with quote
    provenance intact, so a literal paren in a quoted template books."""
    out = parse.parse_file(
        "k/sess-mktq/sess-mktq.jsonl", _read("bash_quoted_capture.jsonl"))
    assert out["tool_uses"][0]["write_targets"] == ["/tmp/p(1).XXXXXX"]


def test_bash_subshell_group_books_targets():
    """#748: commands inside a `( … )` subshell book reads and writes,
    and a redirect on the closing paren books its target."""
    out = parse.parse_file(
        "k/sess-ssg/sess-ssg.jsonl", _read("bash_subshell_group.jsonl"))
    tool = out["tool_uses"][0]
    assert tool["read_targets"] == ["/work/a.py"]
    assert tool["write_targets"] == ["/work/b.txt", "/work/c.txt"]


def test_bash_declaration_flag_capture_books_write_target():
    """#770: option flags after a declaration builtin are transparent to
    the capture scan, so a flagged capture books its inner writes."""
    out = parse.parse_file(
        "k/sess-dfc/sess-dfc.jsonl", _read("bash_declaration_flag_capture.jsonl"))
    assert out["tool_uses"][0]["write_targets"] == ["/tmp/probe.XXXXXX"]


def test_bash_nested_subshell_books_write_target():
    """#771: parens spaced apart are a real nested subshell, scanned
    like any other group; only the unspaced `((` stays refused."""
    out = parse.parse_file(
        "k/sess-nss/sess-nss.jsonl", _read("bash_nested_subshell.jsonl"))
    assert out["tool_uses"][0]["write_targets"] == ["/work/deep.txt"]


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


def test_nul_in_tool_name_and_argument_stays_in_parse_output():
    """A NUL (\\u0000 in the JSON) inside a tool name or tool argument
    reaches tool_uses intact at parse level (issue #670): the parser
    keeps the call, and the persist-level choke point strips the byte
    before it can reach a PostgreSQL text column (tests.test_ingest).
    """
    out = parse.parse_file(
        "k/sess-nul/sess-nul.jsonl", _read("nul_tool_use.jsonl"))
    tus = out["tool_uses"]
    assert sorted(tu["tool_name"] for tu in tus) == ["Read", "Skill\x00Skill"]
    read_tu = next(tu for tu in tus if tu["tool_name"] == "Read")
    assert read_tu["read_targets"] == ["/tmp/a\x00b.md"]
