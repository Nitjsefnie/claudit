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


@pytest.mark.parametrize("program,kind,reads,writes", [
    ('tools.exec_command({"cmd":"pwd"});', None, [], []),
    ('tools.exec_command({"cmd":"cat"});', None, [], []),
    ('tools.exec_command({"cmd":"/usr/bin/true"});', None, [], []),
    ('tools.exec_command({"cmd":"cat real.py","workdir":"/workspace/toy-project"});',
     "whole", ["/workspace/toy-project/real.py"], []),
    ('await tools.exec_command({"workdir":42,"cmd":"cat real.py","budget":1e3});',
     "whole", ["real.py"], []),
    ('tools.exec_command({"cmd":"cat wrong.py","cmd":"cat real.py"});',
     "whole", ["real.py"], []),
    ('tools.monitor({"command":["bash","-lc","cat real.py"],"workdir":null});',
     "whole", ["real.py"], []),
    ('tools.exec_command({"cmd":"cat real.py > output.py"});', "whole", ["real.py"], ["output.py"]),
    ('tools.exec_command({"\\u0063md":"cat hidden.py"});', None, [], []),
    ('tools . /* gap */ exec_command({"cmd":"cat real.py"});', "whole", ["real.py"], []),
    ('tools.exec_command({"cmd":"cat real.py"}).then(handle);', "whole", ["real.py"], []),
    ('// tools.exec_command({"cmd":"cat ghost.py"});\n', None, [], []),
    ('tools.exec_command({"cmd":"cat real.py","other":"\u2028"});', None, [], []),
], ids=["bare-command", "bare-reader", "absolute-command", "literal-cwd", "scalar-options",
        "last-key-wins", "argv", "redirect", "escaped-key", "call-trivia", "chained-call", "comment",
        "raw-string-line-separator"])
def test_wrapped_targets_complete_json_calls(program, kind, reads, writes):
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


@pytest.mark.parametrize("other", [
    "/}/", r"/[},/]/g", r"/\/[}]/", "()=>/}/", "12 / 3", "value / divisor", "value++ / divisor",
    "値 / divisor",
], ids=["closing-brace", "character-class", "escaped-slash", "arrow-regex", "division",
        "variable-division", "postfix-division", "unicode-operand"])
def test_wrapped_targets_survive_regex_siblings(other):
    row = _parse_program('tools.exec_command({other:' + other + ',cmd:"cat input.py"});')
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == ("whole", ["input.py"], [])


@pytest.mark.parametrize("other", [
    "settings.return / divisor",
    "settings.throw / divisor",
    "settings.await / divisor",
    "settings.in / divisor",
    "settings /* object */ . /* property */ return / divisor",
    "settings?.return / divisor",
    "(function(){return /}/})()",
], ids=["return-property", "throw-property", "await-property", "in-property",
        "member-access-comments", "optional-member-access", "return-keyword-regex"])
def test_wrapped_targets_preserve_division_after_keyword_properties(other):
    row = _parse_program('tools.exec_command({other:' + other + ',cmd:"cat input.py"});')
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == ("whole", ["input.py"], [])


@pytest.mark.parametrize("other", [
    "(async()=>[...await /}/])()",
    "(async()=>[await /}/])()",
    "(async()=>[... /* spread */ await /* operand */ /}/])()",
    "[.../}/]",
    "(function*(){return [...yield /}/]})()",
    "[...settings.await / divisor]",
    "[...settings?.return / divisor]",
    "(async()=>[...await /}/, settings.return / divisor, 1_000])()",
], ids=["spread-await", "await-control", "spread-comments", "spread-regex",
        "spread-yield", "spread-member-division", "spread-optional-member", "mixed-contexts"])
def test_wrapped_targets_survive_spread_before_await(other):
    row = _parse_program('tools.exec_command({other:' + other + ',cmd:"cat input.py"});')
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == ("whole", ["input.py"], [])


def test_wrapped_monitor_targets_survive_spread_before_await():
    row = _parse_program(
        'tools.monitor({other:(async()=>[...await /}/])(),'
        'command:["bash","-lc","cat input.py"],workdir:"/workspace/toy-project"});')
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == (
        "whole", ["/workspace/toy-project/input.py"], [])


@pytest.mark.parametrize("ending", [
    "other:[}",
    "other:[",
    "other:/unterminated",
    "/* unterminated",
], ids=["mismatched-delimiter", "unclosed-array", "unclosed-regex", "unclosed-comment"])
def test_wrapped_targets_discard_unbounded_objects(ending):
    row = _parse_program('tools.exec_command({cmd:"cat input.py",' + ending)
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == (None, [], [])


@pytest.mark.parametrize("program,kind,reads", [
    ('tools.exec_command({cmd:"cat input.py",workdir:"/workspace/toy-project/"+suffix});',
     "whole", ["input.py"]),
    ('tools.exec_command({cmd:"cat input.py"+suffix,workdir:"/workspace/toy-project"});',
     None, []),
    ('tools.monitor({command:["bash","-lc","cat input.py"].concat(extra)});', None, []),
    ('tools.exec_command({cmd:"cat input.py" /* literal */,workdir:"/workspace/toy-project" // cwd\n});',
     "whole", ["/workspace/toy-project/input.py"]),
    ('tools.exec_command({cmd:"cat input.py",workdir:"/workspace/toy-project" /* prefix */ +suffix});',
     "whole", ["input.py"]),
], ids=["computed-workdir", "computed-cmd", "computed-argv", "literal-comments", "comment-before-expression"])
def test_wrapped_targets_require_complete_literal_values(program, kind, reads):
    row = _parse_program(program)
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == (kind, reads, [])


@pytest.mark.parametrize("program,writes", [
    ('tools.apply_patch({patch}+suffix);', []),
    ('const patch = {patch} /* prefix */ +suffix; tools.apply_patch(patch);', []),
    ('tools.apply_patch(prefix+{patch});', []),
    ('tools.apply_patch(({patch})+suffix);', []),
    ('const patch = [{patch}].join(suffix); tools.apply_patch(patch);', []),
    ('tools.apply_patch({patch} /* literal */);', ["/workspace/toy-project/new.py"]),
    ('const patch = {patch} // literal\n; tools.apply_patch(patch);', ["/workspace/toy-project/new.py"]),
], ids=["computed-argument", "computed-variable", "computed-prefix", "computed-parenthesized",
        "computed-array", "literal-argument", "literal-variable"])
def test_wrapped_patch_targets_require_complete_literal_values(program, writes):
    patch = json.dumps("*** Begin Patch\n*** Add File: /workspace/toy-project/new.py\n+x\n*** End Patch")
    row = _parse_program(program.replace("{patch}", patch))
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == (None, [], writes)


@pytest.mark.parametrize("prefix", [
    '// tools.exec_command({cmd:"cat ghost.py"});\n',
    '/* tools.exec_command({cmd:"cat ghost.py"}); */',
    """const example = 'tools.exec_command({cmd:"cat ghost.py"});';""",
    'const example = "tools.exec_command({cmd:\\"cat ghost.py\\"});";',
    'const example = `tools.exec_command({cmd:"cat ghost.py"});`;',
    'const example = `outer ${`tools.exec_command({cmd:"cat ghost.py"});`}`;',
    'const example = /tools.exec_command({cmd:"cat ghost.py"})/;',
    'mytools.exec_command({cmd:"cat ghost.py"});',
    'other.tools.exec_command({cmd:"cat ghost.py"});',
], ids=["line-comment", "block-comment", "single-quote", "double-quote", "template", "nested-template",
        "regex", "identifier-boundary", "member-boundary"])
def test_wrapped_targets_ignore_call_shaped_noncode(prefix):
    row = _parse_program(prefix + 'text(await tools.exec_command({cmd:"cat real.py"}));')
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == ("whole", ["real.py"], [])


def test_wrapped_targets_discover_nested_real_calls():
    row = _parse_program(
        'tools.exec_command({other:await tools.monitor({command:["bash","-lc","cat inner.py"]}),'
        'cmd:"cat outer.py"});')
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == (
        "whole", ["outer.py", "inner.py"], [])


@pytest.mark.parametrize("prefix", [
    "if (ready) /}/.test(value);",
    "if (check(ready)) /}/.test(value);",
    "while (ready) /}/.test(value);",
    "for (;ready;) /}/.test(value);",
    "for (const item of items) /}/.test(item);",
    "for await (const item of items) /}/.test(item);",
    "if /* keyword */ (ready) /* statement */ /}/.test(value);",
    'if (ready) /tools.exec_command({cmd:"cat ghost.py"})/.test(value);',
    "check(ready) / divisor;",
    "(ready) / divisor;",
    "settings.if(ready) / divisor;",
    "settings?.while(ready) / divisor;",
    "if (ready) check(ready) / divisor;",
    "const example = `${(()=>{if (ready) /}/.test(value);})()}`;",
], ids=["if-regex", "nested-condition", "while-regex", "for-regex", "for-of-regex", "for-await-regex",
        "control-comments", "regex-ghost-call", "call-division", "group-division",
        "keyword-method-division", "optional-keyword-method-division",
        "statement-call-division", "template-control-regex"])
def test_wrapped_targets_preserve_regex_after_control_parentheses(prefix):
    row = _parse_program(prefix + '\nawait tools.exec_command({cmd:"cat real.py"});')
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == ("whole", ["real.py"], [])


def test_wrapped_targets_preserve_control_regex_in_sibling_body():
    row = _parse_program(
        'tools.exec_command({other:(()=>{if (ready) /}/.test(value);})(),cmd:"cat real.py"});')
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == ("whole", ["real.py"], [])


@pytest.mark.parametrize("fields,reads", [
    ('cmd:"cat wrong.py",...{cmd:"cat actual.py"}', []),
    ('cmd:"cat wrong.py",...options', []),
    ('...options,cmd:"cat actual.py"', ["actual.py"]),
    ('cmd:"cat wrong.py",...options,cmd:"cat actual.py"', ["actual.py"]),
    ('cmd:"cat wrong.py",["cmd"]:"cat actual.py"', []),
    ('cmd:"cat wrong.py",[key]:"unknown"', []),
    ('cmd:"cat wrong.py",get [key](){return "unknown"}', []),
    ('[key]:"unknown",cmd:"cat actual.py"', ["actual.py"]),
    ('cmd:"cat wrong.py",[key]:"unknown",cmd:"cat actual.py"', ["actual.py"]),
    ('workdir:"/wrong",...options,cmd:"cat actual.py"', ["actual.py"]),
    ('workdir:"/wrong",[key]:"unknown",cmd:"cat actual.py"', ["actual.py"]),
    ('...options,cmd:"cat actual.py",workdir:"/workspace/toy-project"',
     ["/workspace/toy-project/actual.py"]),
], ids=["literal-spread", "dynamic-spread", "leading-spread", "restore-after-spread",
        "literal-computed-key", "dynamic-computed-key", "computed-getter", "leading-computed-key",
        "restore-after-computed-key", "uncertain-cwd-spread", "uncertain-cwd-computed",
        "restore-cwd"])
def test_wrapped_targets_respect_uncertain_overwrites(fields, reads):
    row = _parse_program('tools.exec_command({' + fields + '});')
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == (
        "whole" if reads else None, reads, [])


@pytest.mark.parametrize("fields,reads", [
    ('command:["bash","-lc","cat wrong.py"],...options', []),
    ('command:["bash","-lc","cat wrong.py"],[key]:other', []),
    ('...options,command:["bash","-lc","cat actual.py"]', ["actual.py"]),
], ids=["spread", "computed-key", "restored"])
def test_wrapped_monitor_respects_uncertain_overwrites(fields, reads):
    row = _parse_program('tools.monitor({' + fields + '});')
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == (
        "whole" if reads else None, reads, [])


@pytest.mark.parametrize("program,writes", [
    ('const unused = {unused}; tools.apply_patch({actual});', ["actual.py"]),
    ('const patch = {unused}; tools.apply_patch(patch + suffix);', []),
    ('const first = {unused}; const second = {actual};'
     'tools.apply_patch(second); tools.apply_patch(first);', ["actual.py", "unused.py"]),
    ('let patch = {unused}; patch = replacement; tools.apply_patch(patch);', []),
    ('let patch = {unused}; patch += suffix; tools.apply_patch(patch);', []),
    ('tools.apply_patch(patch); const patch = {unused};', []),
    ('tools.apply_patch(missing);', []),
    ('const patch = {actual}; tools.apply_patch(patch); tools.apply_patch(patch);', ["actual.py"]),
    ('// tools.apply_patch({unused});\ntools.apply_patch({actual});', ["actual.py"]),
    ("const example = 'tools.apply_patch({unused});'; tools.apply_patch({actual});", ["actual.py"]),
    ('const patch = {unused}; // tools.apply_patch(patch);\ntools.apply_patch({actual});', ["actual.py"]),
    ('const patch = {unused}; function change(patch) { tools.apply_patch(patch); }', []),
    ('let patch = {unused}; const example = `${patch = replacement}`; tools.apply_patch(patch);', []),
], ids=["unused-binding", "computed-argument", "separate-bindings", "reassigned",
        "compound-assignment", "before-declaration", "unresolved", "repeated-call",
        "commented-call", "quoted-call", "commented-reference", "shadowed-binding", "template-modification"])
def test_wrapped_patch_targets_follow_actual_arguments(program, writes):
    for name in ("unused", "actual"):
        patch = json.dumps("*** Begin Patch\n*** Add File: " + name + ".py\n+x\n*** End Patch")
        program = program.replace("{" + name + "}", patch)
    row = _parse_program(program)
    assert (row["read_kind"], row["read_targets"], row["write_targets"]) == (None, [], writes)
