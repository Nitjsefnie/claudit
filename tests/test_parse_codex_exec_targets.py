from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.flags import resolve_config
from backend.parse import parse_file


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/codex/rollout_exec_targets.jsonl"


def _parse_program(program: str) -> dict:
    lines = [
        {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "synthetic-call",
            "input": program,
        }},
    ]
    blob = "\n".join(json.dumps(line) for line in lines).encode()
    return parse_file("codex/exec-targets.jsonl", blob)["tool_uses"][0]


@pytest.mark.parametrize("index,kind,reads,writes", [
    (0, None, [], ["/workspace/toy-project/added.py", "/workspace/toy-project/updated.py",
                   "/workspace/toy-project/moved.py", "/workspace/toy-project/deleted.py"]),
    (1, "whole", ["/workspace/toy-project/input.py"], ["/workspace/toy-project/output.py"]),
    (2, "whole", ["/workspace/toy-project/monitor.py"], []),
    (3, None, None, None),
    (4, "whole", ["/workspace/toy-project/failed-input.py"], []),
], ids=["apply-patch", "exec-command", "monitor", "function-call", "rejected-write"])
def test_rollout_exec_targets(index, kind, reads, writes):
    rows = parse_file("codex/exec-targets.jsonl", FIXTURE.read_bytes())["tool_uses"]
    row = rows[index]
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == (kind, reads, writes)


def test_wrapped_targets_are_booked_before_rejected_result_settles():
    lines = FIXTURE.read_bytes().splitlines()
    row = parse_file("codex/exec-targets.jsonl", b"\n".join(lines[:-1]))["tool_uses"][-1]
    assert row["is_error"] is None
    assert row["write_targets"] == ["/workspace/toy-project/failed-output.py"]


def test_wrapped_targets_collection_off_keeps_all_intake_columns_null():
    policy = resolve_config({"CLAUDIT_COLLECT_CONTEXT_INTAKE": "0"}).collection
    rows = parse_file("codex/exec-targets.jsonl", FIXTURE.read_bytes(), collection=policy)["tool_uses"]
    for row in rows:
        assert [row[field] for field in (
            "read_kind", "read_targets", "write_targets", "result_chars", "is_reread",
        )] == [None] * 5


@pytest.mark.parametrize("program,kind,reads,writes", [
    ('tools.exec_command({cmd:"cat first.py",workdir:"/workspace/toy-project/one"});'
     'tools.exec_command({workdir:"/workspace/toy-project/two",cmd:"head second.py"});'
     'tools.exec_command({cmd:"cat third.py"});',
     "slice", ["/workspace/toy-project/one/first.py", "/workspace/toy-project/two/second.py",
               "third.py"], []),
    ('tools.exec_command({cmd:"cat local.py",workdir:42});', "whole", ["local.py"], []),
    ('tools.exec_command({cmd:`cat ${path}`,workdir:"/workspace/toy-project"});', None, [], []),
    ('tools.monitor({command:["bash","-lc","cat local.py"],workdir:null});',
     "whole", ["local.py"], []),
    ('tools.monitor({command:["bash","run.sh"]});', None, [], []),
    ('tools.monitor({command:["bash","-lc",42]});', None, [], []),
    ('tools.monitor({command:["unknown","-c","cat local.py"]});', None, [], []),
    ('tools.monitor({command:["python3","-c","open(\\\"written.py\\\", \\\"w\\\").write(\\\"x\\\")"]});',
     None, [], []),
    ('tools.exec_command({cmd:"cat local.py",other:{"workdir":"/workspace/toy-project/wrong"}});',
     "whole", ["local.py"], []),
    ('tools.apply_patch("*** Begin Patch\\n*** Add File: \\n*** End Patch");', None, [], []),
], ids=["per-object-cwd", "nonstring-cwd", "dynamic-command", "null-cwd", "no-inline-payload",
        "nonstring-argv", "unknown-program", "python-inline", "nested-cwd", "no-patch-path"])
def test_wrapped_targets_literal_arguments(program, kind, reads, writes):
    row = _parse_program(program)
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == (kind, reads, writes)


def test_wrapped_tool_error_preserves_targets_and_existing_zero_churn_rule():
    blob = FIXTURE.read_bytes().replace(b'Tool use was rejected by user', b'Exit code 1')
    row = parse_file("codex/exec-targets.jsonl", blob)["tool_uses"][-1]
    assert row["is_error"] is True
    assert row["error_kind"] == "tool_error"
    assert row["write_targets"] == ["/workspace/toy-project/failed-output.py"]
    assert (row["lines_added"], row["lines_deleted"]) == (0, 0)


@pytest.mark.parametrize("name", ["apply_patch", "exec_command", "spawn_agent", "wait_agent", "send_message"])
def test_unwrapped_function_calls_keep_targets_null(name):
    lines = [
        {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
        {"type": "response_item", "payload": {
            "type": "function_call", "name": name, "call_id": "synthetic-plain",
            "arguments": json.dumps({"cmd": "cat input.py", "command": ["bash", "-lc", "cat other.py"]}),
        }},
    ]
    blob = "\n".join(json.dumps(line) for line in lines).encode()
    row = parse_file("codex/plain-targets.jsonl", blob)["tool_uses"][0]
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == (None, None, None)


@pytest.mark.parametrize("program,reads,writes", [
    ('tools.exec_command({cmd:"cat input.py",yield_time_ms:1_000});', ["input.py"], []),
    ('tools.exec_command({yield_time_ms:1_000,cmd:"cat input.py"});', ["input.py"], []),
    ('tools.exec_command({cmd:"cat input.py",yield_time_ms:1_000,'
     'workdir:"/workspace/toy-project",max_output_tokens:budget});',
     ["/workspace/toy-project/input.py"], []),
    ('tools.monitor({command:["bash","-lc","cat input.py"],yield_time_ms:1_000,'
     'workdir:"/workspace/toy-project"});', ["/workspace/toy-project/input.py"], []),
    ('tools.exec_command({workdir:cwd,cmd:"cat input.py"});', ["input.py"], []),
    ('tools.exec_command({cmd:command,workdir:"/workspace/toy-project"});'
     'tools.exec_command({cmd:"cat input.py",other:undefined});', ["input.py"], []),
    ('tools.exec_command({other:{workdir:"/workspace/toy-project/wrong",value:1_000},'
     'cmd:"cat input.py",workdir:"/workspace/toy-project/right"});',
     ["/workspace/toy-project/right/input.py"], []),
    ('tools.exec_command({other:choose(1_000, {workdir:"/workspace/toy-project/wrong"}),'
     'cmd:"cat input.py"});', ["input.py"], []),
    ('tools.exec_command({other:[1_000,`workdir: "/workspace/toy-project/wrong"`],'
     'cmd:"cat input.py"});', ["input.py"], []),
    ("tools.exec_command({other:'},workdir:\"/workspace/toy-project/wrong\"',"
     'cmd:"cat input.py",yield_time_ms:1_000});', ["input.py"], []),
    ('tools.exec_command({other:1_000,/* }, workdir:"wrong" */cmd:"cat input.py"});',
     ["input.py"], []),
    ('tools.exec_command({other:1_000,// }, workdir:"wrong"\ncmd:"cat input.py"});',
     ["input.py"], []),
    ('tools.apply_patch("*** Begin Patch\\n*** Add File: /workspace/toy-project/new.py\\n+x\\n*** End Patch");'
     'tools.exec_command({cmd:"cat input.py",other:undefined});',
     ["input.py"], ["/workspace/toy-project/new.py"]),
], ids=["numeric-separator-after", "numeric-separator-before", "cwd-after-nonjson-sibling",
        "monitor-nonjson-sibling", "dynamic-cwd", "dynamic-command-no-cwd-leak",
        "nested-object-cwd", "nested-call-cwd", "nested-array-template", "quoted-delimiters",
        "block-comment", "line-comment", "patch-independent"])
def test_wrapped_targets_fields_survive_nonjson_siblings(program, reads, writes):
    row = _parse_program(program)
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == ("whole", reads, writes)
