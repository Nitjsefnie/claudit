"""Tests moved from test_parse.py to keep test modules under 700 lines."""
from __future__ import annotations

import json
from typing import Any

import pytest

from backend import constants, parse, pricing
from backend.rereads import resolve_rereads

from tests.test_parse import (
    _read,
)


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


def test_dispatch_agent_type_takes_its_canonical_name(tmp_path):
    """One role, one name (issue #650): the asked type folds a plugin
    namespace and a cross-lane spelling, exactly like the stored side."""
    dispatch_line = {
        "type": "assistant", "sessionId": "s", "uuid": "a0",
        "requestId": "req-0", "timestamp": "2026-05-07T10:00:01Z",
        "message": {"role": "assistant", "model": "m",
                    "content": [{"type": "tool_use", "id": "t1",
                                 "name": "Task",
                                 "input": {"subagent_type":
                                           "superpowers:code-reviewer",
                                           "prompt": "review"}}],
                    "usage": {"input_tokens": 1, "output_tokens": 1}},
    }
    blob = "\n".join([
        json.dumps({"type": "user", "sessionId": "s", "uuid": "u0",
                    "timestamp": "2026-05-07T10:00:00Z",
                    "message": {"role": "user", "content": "go"}}),
        json.dumps(dispatch_line),
    ]) + "\n"
    key = "k/sess-canon/sess-canon.jsonl"
    path = tmp_path / "canon.jsonl"
    path.write_text(blob)
    out = parse.parse_file(key, path.read_bytes())
    assert out["tool_uses"][0]["agent_type"] == "code-reviewer"


def test_the_file_agent_type_folds_in_band_names(tmp_path):
    """attributionAgent and agent-setting take the canonical name too
    (issue #650)."""
    blob = "\n".join([
        json.dumps({"type": "user", "sessionId": "s", "uuid": "u0",
                    "isSidechain": True, "attributionAgent": "explorer",
                    "timestamp": "2026-05-07T10:00:00Z",
                    "message": {"role": "user", "content": "go"}}),
        json.dumps({"type": "assistant", "sessionId": "s", "uuid": "a0",
                    "requestId": "req-0", "isSidechain": True,
                    "attributionAgent": "explorer",
                    "timestamp": "2026-05-07T10:00:01Z",
                    "message": {"role": "assistant", "model": "m",
                                "content": [{"type": "text", "text": "ok"}],
                                "usage": {"input_tokens": 1,
                                          "output_tokens": 1}}}),
    ]) + "\n"
    path = tmp_path / "fold.jsonl"
    path.write_text(blob)
    out = parse.parse_file("k/sess-fold/sess-fold.jsonl",
                           path.read_bytes())
    assert out["agent_type"] == "Explore"
    assert out["agent_type_in_band"] is True


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
    resolve_rereads(rows)
    assert [r["is_reread"] for r in rows] == [None, False]


def test_partial_overlap_is_not_a_reread():
    """`cat a b` after reading only `a` still brought `b` in."""
    rows = [
        {"read_kind": "whole", "read_targets": ["/a"], "write_targets": [],
         "is_error": False, "is_reread": None},
        {"read_kind": "whole", "read_targets": ["/a", "/b"],
         "write_targets": [], "is_error": False, "is_reread": None},
    ]
    resolve_rereads(rows)
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
    # The mkdir limb now books the directory it creates beside the
    # heredoc's file (#655).
    assert tu["write_targets"] == ["/repo/scripts", "/repo/scripts/gen.py"]


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
    resolve_rereads(rows)
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
    resolve_rereads(rows)
    assert rows[1]["is_reread"] is expected
    assert [row["read_targets"] for row in rows] == [[first], [second]]


@pytest.mark.parametrize("name", ["Write", "Edit", "NotebookEdit"])
def test_raw_windows_write_invalidates_equivalent_read_without_rewriting(name):
    first, written = "C:/Work/f.py", r"c:\Work\f.py"
    kind, reads, writes = parse._tool_access(name, {"file_path": written})  # pylint: disable=protected-access
    rows = [{"read_kind": "whole", "read_targets": [first], "write_targets": [], "is_error": False, "is_reread": None},
            {"read_kind": kind, "read_targets": reads, "write_targets": writes, "is_error": False, "is_reread": None},
            {"read_kind": "whole", "read_targets": [first], "write_targets": [], "is_error": False, "is_reread": None}]
    resolve_rereads(rows)
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
    """Window events attach to the next request; tool-result-only user lines
    and empty windows add no prompt flags."""
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


def test_turn_flags_user_prompt_pasted_content():
    out = parse.parse_file("k/s/s.jsonl", _read("turn_flags_user_prompt_pasted_content.jsonl"))
    assert out["records"][0]["turn_flags"] == ["user_prompt"]


def test_turn_flags_user_prompt_image_only():
    out = parse.parse_file("k/s/s.jsonl", _read("turn_flags_user_prompt_image_only.jsonl"))
    assert out["records"][0]["turn_flags"] == ["user_prompt"]


def test_turn_flags_user_prompt_excludes_is_meta_text():
    out = parse.parse_file("k/s/s.jsonl", _read("turn_flags_user_prompt_excludes_is_meta_text.jsonl"))
    assert out["records"][0]["turn_flags"] == []


def test_prompt_snapshot_flags_land_on_the_request_they_describe():
    """Snapshots backfill the request they describe; preamble and baseline
    snapshots add no flags, while later changes land on that request."""
    out = parse.parse_file("k/sess-ps/sess-ps.jsonl", _read("prompt_snapshot.jsonl"))
    first, second = out["records"]
    assert first["turn_flags"] == ["resume", "user_prompt"]
    assert first["turn_tool_results"] == 0
    assert first["cli_version"] == "2.1.272"
    assert second["turn_flags"] == ["tools_change"]
    assert second["turn_tool_results"] == 1


def test_prompt_rerender_and_system_change_backfill_with_resume():
    """System changes backfill, and identical snapshots mark rerenders.
    Resume and cwd changes join the flags on their corresponding requests."""
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
    # Priced at the record's own time: the host's row, and the bare model
    # lane alone — exactly as before the provider table existed.
    tokens: dict[str, Any] = {"fresh": 1000, "eph5": 0, "eph1h": 0, "unsplit_create": 0, "read": 2000, "output": 300}
    assert novita["cost_usd"] == pytest.approx(round(pricing.compute_cost(novita["model"], ts=novita["ts"], res=pricing.resolve(novita["model"], novita["ts"], "Novita"), **tokens), 6))  # sv-test-data: allow (derived: expected priced from the same loaded tables as the record)
    assert bare["cost_usd"] == pytest.approx(round(pricing.compute_cost(bare["model"], ts=bare["ts"], **tokens), 6))


def test_search_requests_fold_into_parse_cost_without_a_fee_column(monkeypatch):
    """Claude's nested server_tool_use count multiplies the host's
    explicit per-search rate, while an uncounted record stays NULL."""
    search_rate = 0.0137
    pair = ("acme/acme-9", "SearchHost")
    token_rates = {"fresh": 2.0, "create_5m": 2.5, "create_1h": 4.0,
                   "read": 0.2, "output": 10.0}
    monkeypatch.setitem(pricing.PROVIDER_RATES, pair, {
        **token_rates, "web_search": search_rate,
    })
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {})
    out = parse.parse_file("k/s/request_fee.jsonl",
                           _read("request_fee.jsonl"))
    search_rec, bare = out["records"]
    assert search_rec["provider"] == "SearchHost"
    assert search_rec["web_search_requests"] == 3
    assert bare["web_search_requests"] is None
    tokens: dict[str, Any] = {"fresh": 1000, "eph5": 0, "eph1h": 0,
                              "unsplit_create": 0, "read": 2000,
                              "output": 300}
    assert search_rec["cost_usd"] == round(pricing.compute_cost(
        search_rec["model"], ts=search_rec["ts"], **tokens,
        adjustments=pricing.CostAdjustments(web_search_requests=3),
        res=pricing.resolve(search_rec["model"],
                            search_rec["ts"], "SearchHost")), 6)
    assert bare["cost_usd"] == pytest.approx(round(
        pricing.compute_cost(bare["model"], ts=bare["ts"],
                             fresh=500, eph5=0, eph1h=0, unsplit_create=0,
                             read=0, output=100), 6))


def test_provider_merges_across_streaming_chunks_first_non_null_wins():
    """One streamed reply is written as several assistant lines sharing a
    requestId, and the serving host is named on whichever chunk carries
    message.provider — often not the first. The merged record keeps the
    FIRST non-null provider: a later chunk fills an empty one, and an
    absent one never wipes it — while usage still max-merges as before."""
    out = parse.parse_file(
        "k/sess-pm/sess-pm.jsonl", _read("provider_merge.jsonl")
    )
    assert len(out["records"]) == 2
    carried, kept = out["records"]
    assert carried["request_id"] == "req-1"
    assert kept["request_id"] == "req-2"
    # req-1's provider arrives on the SECOND chunk and must still be kept.
    assert carried["provider"] == "Novita"
    # req-2's trailing provider-less chunk must not wipe its provider.
    assert kept["provider"] == "Novita"
    # usage max-merged as before, and one record per requestId.
    assert carried["output_tokens"] == 200      # max(50, 200)
    assert carried["fresh_tokens"] == 1000
    assert kept["output_tokens"] == 300         # max(100, 300)
    assert kept["fresh_tokens"] == 1000


def test_provider_merge_conflicting_providers_first_wins():
    """The case the sibling test leaves open: two chunks of ONE reply
    naming DIFFERENT non-null hosts. The merged record keeps the FIRST
    provider it saw — a later chunk never overwrites an already-named
    host — while usage still max-merges across the conflicting lines."""
    out = parse.parse_file(
        "k/sess-pmc/sess-pmc.jsonl", _read("provider_merge_conflict.jsonl")
    )
    assert len(out["records"]) == 1
    merged = out["records"][0]
    assert merged["request_id"] == "req-1"
    # 'Chutes' arrives on the second chunk; the record keeps 'Novita'.
    assert merged["provider"] == "Novita"
    assert merged["output_tokens"] == 200       # max(50, 200)


def test_provider_strips_surrounding_whitespace():
    """message.provider is stored stripped: ' Chutes ' is the host Chutes,
    and a whitespace-only value is no provider at all."""
    provider = parse._provider  # pylint: disable=protected-access
    assert provider({"provider": " Chutes "}) == "Chutes"
    assert provider({"provider": "   "}) is None


def test_provider_non_string_values_are_none():
    """A malformed line whose provider is not a string stores NULL rather
    than raising or stringifying the value; a missing key is the normal
    no-provider shape."""
    provider = parse._provider  # pylint: disable=protected-access
    for value in (42, None, ["Novita"]):
        assert provider({"provider": value}) is None, value
    assert provider({}) is None
