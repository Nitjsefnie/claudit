"""Collection OFF masks only its storage and skips independent producers."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from backend import parse, pricing
from backend.flag_registry import COLLECTION_SPECS, CollectionSpec
from backend.flags import ALL_COLLECTIONS_ON, CollectionPolicy

FIX = Path(__file__).resolve().parents[1] / 'fixtures' / 'parser'
KEY = 'sessions/p/s/wire.jsonl'
GROUPS = tuple(s.key for s in COLLECTION_SPECS)


@pytest.fixture(autouse=True)
def synthetic_rates(monkeypatch):
    """Cost controls exercise the real pricing path against synthetic rate data."""
    rates = {'fresh': 7.0, 'create_5m': 11.0, 'create_1h': 13.0,
             'read': 3.0, 'output': 19.0}
    for model in ('claude-sonnet-4-5', 'gpt-5-6-sol', 'kimi-k3'):
        monkeypatch.setitem(pricing.MODEL_RATES, model, rates)
        monkeypatch.setitem(pricing.DATED_RATES, model, [])
    monkeypatch.setattr(pricing, 'DEFAULT_RATES', rates)


def policy(*disabled: str) -> CollectionPolicy:
    return CollectionPolicy(frozenset(disabled), COLLECTION_SPECS)


def sample(fmt: str) -> bytes:
    """Each supported optional column has a live value somewhere in this file."""
    if fmt == 'claude':
        return b''.join((FIX / name).read_bytes() for name in (
            'collection_reads.jsonl', 'collection_dispatch.jsonl', 'generation_s_last_line.jsonl'))
    args = {'subagent_type': 'explore', 'agent_type': 'explorer',
            'model': 'haiku', 'name': 'finder',
            'prompt': 'Read /tmp/brief/task.md IN FULL.'}
    toks = {'input_other': 100, 'input_cache_creation': 20,
            'input_cache_read': 30, 'output': 5}
    if fmt == 'legacy':
        rows = [
            {'message': {'type': 'TurnBegin', 'payload': {}}},
            {'message': {'type': 'ToolCall', 'payload': {
                'id': 'a', 'function': {'name': 'Agent', 'arguments': args}}}},
            {'message': {'type': 'ToolResult', 'payload': {
                'tool_call_id': 'a', 'return_value': {
                    'is_error': True, 'output': 'Exit code 1'}}}},
            {'message': {'type': 'StatusUpdate', 'payload': {'token_usage': toks}}},
        ]
    elif fmt == 'kimi-code':
        rows = [
            {'type': 'turn.prompt', 'input': [{'type': 'text', 'text': 'go'}]},
            {'type': 'config.update', 'profileName': 'explore'},
            {'type': 'context.append_loop_event', 'event': {
                'type': 'tool.call', 'toolCallId': 'a', 'name': 'Agent', 'args': args}},
            {'type': 'context.append_loop_event', 'event': {
                'type': 'tool.result', 'toolCallId': 'a',
                'result': {'isError': True, 'output': 'Exit code 1'}}},
            {'type': 'usage.record', 'model': 'kimi-code/k3', 'usage': {
                'inputOther': 100, 'inputCacheCreation': 20,
                'inputCacheRead': 30, 'output': 5}},
        ]
    else:
        usage = {'input_tokens': 150, 'cached_input_tokens': 30,
                 'cache_write_input_tokens': 20, 'output_tokens': 5,
                 'total_tokens': 155}
        rows = [
            {'type': 'session_meta', 'payload': {
                'id': 's', 'session_id': 's', 'agent_role': 'explorer'}},
            {'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started'}},
            {'type': 'response_item', 'payload': {
                'type': 'function_call', 'name': 'spawn_agent', 'call_id': 'a', 'arguments': json.dumps(args)}},
            {'type': 'response_item', 'payload': {
                'type': 'function_call_output', 'call_id': 'a', 'output': 'collab spawn failed: Exit code 1'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'total_token_usage': usage, 'last_token_usage': usage}}},
        ]
    for i, row in enumerate(rows):
        row['timestamp'] = f'2026-09-01T12:00:0{i}Z'
        if fmt == 'kimi-code':
            row['time'] = 1788264000000 + i * 1000
    return b''.join(json.dumps(row).encode() + b'\n' for row in rows)


@pytest.mark.parametrize('fmt', ['claude', 'codex', 'kimi-code', 'legacy'])
@pytest.mark.parametrize('group', GROUPS)
def test_each_collection_group_nulls_exactly_its_fields(fmt, group):
    blob = sample(fmt)
    on = parse.parse_file(KEY, blob)
    assert on == parse.parse_file(KEY, blob, collection=ALL_COLLECTIONS_ON)
    assert on['records'] and on['tool_uses'] and on['ctx_turns']
    spec = next(s for s in COLLECTION_SPECS if s.key == group)
    if group == 'dispatch_analytics':
        dispatch = next(t for t in on['tool_uses'] if t['agent_type'])
        assert dispatch['agent_type'] == 'Explore' and dispatch['agent_model'] == 'haiku'
        if fmt == 'claude':
            assert dispatch['dispatch_name'] == 'finder'
        elif fmt != 'codex':
            assert dispatch['dispatch_prompt_chars'] > 0
            assert dispatch['dispatch_brief_ref'] is True
    # Lane adapters cannot invent Claude-only metadata or context intake.
    supported = fmt == 'claude' or group not in (
        'turn_context', 'request_metadata', 'context_intake', 'generation_speed')
    for rows, fields in ((on['records'], spec.record_fields),
                         (on['tool_uses'], spec.tool_fields), ([on], spec.file_fields)):
        for field in fields:
            if supported and not (fmt == 'codex' and field in (
                    'dispatch_prompt_chars', 'dispatch_brief_ref')):
                assert any(row[field] not in (None, [], 0, '') for row in rows), field
            elif fmt == 'codex' and field.startswith('dispatch_'):
                assert all(row[field] is None for row in rows)
    off = parse.parse_file(KEY, blob, collection=policy(group))
    for on_rows, off_rows, fields in (
            (on['records'], off['records'], spec.record_fields),
            (on['tool_uses'], off['tool_uses'], spec.tool_fields),
            ([on], [off], spec.file_fields)):
        assert len(on_rows) == len(off_rows)
        for original, masked in zip(on_rows, off_rows):
            assert original.keys() == masked.keys()
            for field in fields:
                assert masked[field] is None
            if fields is spec.file_fields:
                original = {k: v for k, v in original.items() if k not in ('records', 'tool_uses')}
                masked = {k: v for k, v in masked.items() if k not in ('records', 'tool_uses')}
            assert {k: v for k, v in original.items() if k not in fields} == {
                k: v for k, v in masked.items() if k not in fields}


@pytest.mark.parametrize('fmt,target,group', [
    ('claude', 'backend.turn_flags.TurnWindow.observe', 'turn_context'),
    ('claude', 'backend.turn_flags.TurnWindow.take', 'turn_context'),
    ('claude', 'backend.turn_flags.TurnWindow._observe_snapshot', 'turn_context'),
    ('claude', 'backend.parse.GenerationTiming', 'generation_speed'),
    ('claude', 'backend.parse.resolve_cache_writes', 'cache_write_tokens'),
    ('claude', 'backend.bash_reads.scan_command', 'context_intake'),
    ('claude', 'backend.parse_tool_rows._tool_access', 'context_intake'),
    ('claude', 'backend.parse.resolve_rereads', 'context_intake'),
    ('claude', 'backend.parse_tool_rows._result_size', 'context_intake'),
    ('claude', 'backend.parse_tool_rows._dispatch_prompt_shape', 'dispatch_analytics'),
    ('claude', 'backend.parse_collection.collected_models', 'file_extras'),
    *[(fmt, target, group) for fmt, groups in (
        ('codex', ('cache_write_tokens', 'file_extras')),
        ('kimi-code', ('cache_write_tokens', 'file_extras', 'dispatch_analytics')),
        ('legacy', ('cache_write_tokens', 'file_extras', 'dispatch_analytics')),
    ) for target, group in (
        ('backend.parse.resolve_cache_writes', 'cache_write_tokens'),
        ('backend.parse_collection.collected_models', 'file_extras'),
        ('backend.kimi_tool_rows._dispatch_prompt_shape', 'dispatch_analytics'),
    ) if group in groups],
])
def test_off_skips_optional_work(fmt, target, group):
    blob = sample(fmt)
    if fmt == 'claude':
        blob += (b'{"type":"attachment","attachment":{"type":"prompt_snapshot",'
                 b'"tools":[{"name":"Read"}],"systemPrompt":"s"}}\n')
    on = parse.parse_file(KEY, blob)
    with patch(target, side_effect=AssertionError('optional producer ran')):
        with pytest.raises(AssertionError, match='optional producer ran'):
            parse.parse_file(KEY, blob)
        off = parse.parse_file(KEY, blob, collection=policy(group))
    assert off['ctx_turns'] == on['ctx_turns']
    assert off['prompt_ts'] == on['prompt_ts']
    for original, masked in zip(on['records'], off['records']):
        for key in ('cost_usd', 'output_tokens', 'thinking_tokens', 'uuid',
                    'request_id', 'reply_latency_s', 'ctx_input'):
            assert masked[key] == original[key]


def test_error_storage_off_keeps_churn_and_survival():
    """Classification and full failure text must survive storage switches."""
    calls = [
        ('Write', {'file_path': 'ok.py', 'content': 'x\ny\n'}, False, 'done'),
        ('Write', {'file_path': 'no.py', 'content': 'x\n'}, True,
         "The user doesn't want to proceed with this tool use"),
        ('Bash', {'command': 'false'}, True, 'Exit code 1'),
        ('Bash', {'command': "cat > x.py <<'EOF'\nx\ny\nEOF\nfalse"}, True,
         'Exit code 1\n' + 'padding\n' * 100),
        ('Bash', {'command': "cat > x.py <<'EOF'\nx\ny\nEOF\nfalse"}, True,
         'Exit code 1\n' + 'padding\n' * 100 + 'No space left on device'),
    ]
    rows = []
    for i, (name, args, failed, result) in enumerate(calls):
        rows.extend([
            {'type': 'assistant', 'message': {
                'role': 'assistant', 'model': 'claude-sonnet-4-5', 'usage': {'input_tokens': 100},
                'content': [{'type': 'tool_use', 'id': f't{i}', 'name': name, 'input': args}]}},
            {'type': 'user', 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': f't{i}', 'is_error': failed,
                 'content': result}]}},
        ])
    blob = b''.join(json.dumps(r).encode() + b'\n' for r in rows)
    on = parse.parse_file(KEY, blob)['tool_uses']
    assert [t['lines_added'] for t in on] == [2, 0, 0, 2, 0]
    assert [t['is_error'] for t in on] == [False, True, True, True, True]
    assert [t['error_kind'] for t in on] == [None, 'rejected', 'tool_error', 'tool_error', 'tool_error']
    assert on[1]['error_text'] and on[0]['write_targets'] == ['ok.py']
    assert on[3]['write_targets'] == ['x.py']
    # This marker is beyond stored error_text; only the full text can zero churn.
    assert 'No space left on device' not in on[4]['error_text']
    off = parse.parse_file(KEY, blob, collection=policy(
        'tool_error_kinds', 'error_text', 'context_intake'))['tool_uses']
    for original, masked in zip(on, off):
        for key in ('lines_added', 'lines_deleted', 'is_error', 'tool_use_id'):
            assert masked[key] == original[key]
        for key in ('error_kind', 'error_text', 'result_chars', 'read_kind',
                    'read_targets', 'write_targets', 'is_reread'):
            assert masked[key] is None


def test_registry_extension_masks_without_new_flag_plumbing():
    from backend.parse_collection import mask_collected_fields  # pylint: disable=import-outside-toplevel
    extra = CollectionSpec('synthetic_speed', 'TEST_COLLECT_SYNTHETIC_SPEED',
                           record_fields=('speed_sample',))
    specs = tuple(s for s in COLLECTION_SPECS if s.key != extra.key) + (extra,)
    on = {'records': [{'speed_sample': 7, 'output_tokens': 5}], 'tool_uses': []}
    assert mask_collected_fields(on, CollectionPolicy(frozenset(), specs)) is on
    assert on['records'][0]['speed_sample'] == 7
    off = mask_collected_fields(on, CollectionPolicy(frozenset({extra.key}), specs))
    assert off['records'][0] == {'speed_sample': None, 'output_tokens': 5}


@pytest.mark.parametrize('fmt', ['claude', 'codex', 'kimi-code', 'legacy'])
def test_all_off_retains_billing_identity_turns_and_latency(fmt):
    blob = sample(fmt)
    on = parse.parse_file(KEY, blob)
    off = parse.parse_file(KEY, blob, collection=policy(*GROUPS))
    assert on['records'] and on['prompt_ts'] and on['ctx_turns']
    assert on['records'][0]['output_tokens'] > 0
    assert on['records'][0]['cost_usd'] > 0
    assert on['records'][0]['reply_latency_s'] is not None
    for key in ('prompt_ts', 'ctx_turns', 'agent_type', 'agent_type_in_band', 'rate_limit_hits'):
        assert off[key] == on[key]
    for original, masked in zip(on['records'], off['records']):
        for key in ('fresh_tokens', 'cache_creation_tokens', 'cache_read_tokens',
                    'output_tokens', 'thinking_tokens', 'cost_usd', 'model',
                    'long_context', 'ctx_input', 'uuid', 'request_id', 'ts',
                    'reply_latency_s', 'text_chars'):
            assert masked[key] == original[key]


@pytest.mark.parametrize('fmt', ['claude', 'codex', 'kimi-code', 'legacy'])
def test_dispatch_analytics_off_preserves_sidecar_precedence(fmt):
    from backend.agent_sidecar import apply_agent_sidecar  # pylint: disable=import-outside-toplevel
    blob = sample(fmt)
    sidecar = json.dumps({'agentType': 'code-reviewer', 'toolUseId': 'a'}).encode()
    on = apply_agent_sidecar(parse.parse_file(KEY, blob), sidecar, KEY)
    off = apply_agent_sidecar(parse.parse_file(
        KEY, blob, collection=policy('dispatch_analytics')), sidecar, KEY)
    # In-band roles win in Codex and kimi-code; the sidecar fills the others.
    assert on['agent_type'] == ('Explore' if fmt in ('codex', 'kimi-code') else 'code-reviewer')
    assert off['agent_type'] == on['agent_type']
    assert off['agent_type_in_band'] == on['agent_type_in_band']
    # A teammate sidecar has a name and no role; it must retain that join key.
    member = json.dumps({'agentType': 'finder', 'name': 'finder',
                         'taskKind': 'in_process_teammate'}).encode()
    on = apply_agent_sidecar(parse.parse_file(KEY, blob), member, KEY)
    off = apply_agent_sidecar(parse.parse_file(
        KEY, blob, collection=policy('dispatch_analytics')), member, KEY)
    if fmt in ('claude', 'legacy'):
        assert on['teammate_name'] == 'finder'
        assert off['teammate_name'] == on['teammate_name']
    else:
        assert on['agent_type_in_band'] is True
        assert off == {**on, 'tool_uses': off['tool_uses']}


def test_request_metadata_off_keeps_closing_usage_merge():
    blob = sample('claude')
    opening = parse.parse_file(KEY, blob)['records'][0]
    closing = {'type': 'assistant', 'requestId': 'r1', 'effort': 'low',
               'message': {'role': 'assistant', 'model': 'claude-sonnet-4-5',
                           'stop_reason': 'end_turn', 'content': [],
                           'usage': {'input_tokens': 150, 'output_tokens': 17}}}
    blob += json.dumps(closing).encode() + b'\n'
    on = parse.parse_file(KEY, blob)['records'][0]
    off = parse.parse_file(KEY, blob, collection=policy('request_metadata'))['records'][0]
    assert opening['output_tokens'] == 5
    assert on['output_tokens'] == 17 and on['fresh_tokens'] == 150
    assert on['stop_reason'] == 'end_turn' and on['effort'] == 'high'
    assert off['stop_reason'] is None and off['effort'] is None
    assert {k: v for k, v in on.items() if k not in ('stop_reason', 'effort')} == {
        k: v for k, v in off.items() if k not in ('stop_reason', 'effort')}
