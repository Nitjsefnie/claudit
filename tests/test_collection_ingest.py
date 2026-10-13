"""Collection policy changes converge real stored rows and derived state."""
from __future__ import annotations

import json
from functools import partial
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from psycopg import sql

from test_ingest import _fresh_db_fixture
from test_collection_flags import GROUPS, sample, synthetic_rates
from test_canonical_model_attribution import (
    DIFFERENT_MODEL_FORK_FIXTURE, _switching_parent_rollout,
)
from backend import cache, db, flags, ingest, parse, parser_version, pricing, timing
from backend.flag_registry import COLLECTION_SPECS
from backend.flags import CollectionPolicy
from tests import test_ingest_forkserver as child_units

__all__ = ['_fresh_db_fixture', 'synthetic_rates']
FIX = Path(__file__).resolve().parents[1] / 'fixtures' / 'parser'
CLAUDE_KEY = 'proj/sess/sess.jsonl'
LANE_KEY = 'sessions/proj/sess/wire.jsonl'


@pytest.fixture(name="mirror")
def _mirror_fixture(monkeypatch, tmp_path):
    root = tmp_path / 'mirror'
    bucket = root / 'claude'
    bucket.mkdir(parents=True)
    monkeypatch.setenv('CLAUDIT_R2_ENDPOINT', root.as_uri())
    monkeypatch.setenv('CLAUDIT_R2_BUCKET', 'claude')
    monkeypatch.setenv('CLAUDIT_INGEST_WORKERS', '1')
    _config(monkeypatch)
    return bucket


def _config(monkeypatch, *disabled: str) -> None:
    env = {s.env: '0' for s in COLLECTION_SPECS if s.key in disabled}
    monkeypatch.setattr(flags, 'CONFIG', flags.resolve_config(env))
    cache.response_cache.clear()


def _put(mirror, key: str, blob: bytes) -> str:
    dest = mirror / key
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)
    return 'claude/' + key


def _blob() -> bytes:
    # Keep a genuine failure for error-kind aggregation and a successful dispatch.
    rows = [json.loads(line) for line in sample('claude').splitlines()]
    dispatch = json.loads(json.dumps(rows[3]))
    dispatch.update(uuid='a3', requestId='r3', timestamp='2026-09-01T12:00:03Z')
    dispatch['message']['content'] = [dispatch['message']['content'][1]]
    dispatch['message']['content'][0]['id'] = 'agent-ok'
    rows.extend([dispatch, {
        'type': 'user', 'message': {'role': 'user', 'content': [{
            'type': 'tool_result', 'tool_use_id': 'agent-ok', 'content': 'done'}]}}])
    return b''.join(json.dumps(row).encode() + b'\n' for row in rows)


def _run() -> dict:
    result = ingest.run_ingest('collection-test')
    assert result['error'] is None, result
    return result


# #959 stores these fields as dim FK ids; master's assertions read the
# transport field's TEXT value, so each id resolves through its dim
# table (a masked or absent id reads NULL, which is what "off" means).
_DIM_VALUES = {
    "model": "dim_model", "provider": "dim_provider",
    "stop_reason": "dim_stop_reason", "effort": "dim_effort",
    "cli_version": "dim_cli_version", "tool_name": "dim_tool_name",
    "error_kind": "dim_error_kind", "agent_type": "dim_agent_type",
    "agent_model": "dim_agent_model",
}


def _rows(table: str, key: str) -> list[dict]:
    """Every row of `table` belonging to one file_key, in transport shape.

    files keeps file_key; records/tool_uses key on file_id since #959,
    so the fact tables resolve the id through files first. Dim FK ids
    come back as their TEXT values, and `file_id` — a #959 storage
    surrogate master's rows never carried — is dropped so a
    delete-and-reingest snapshot compares like for like.
    """
    with db.viz_conn() as conn:
        if table == "files":
            where, params = "file_key = %s", (key,)
        else:
            where = ("file_id = (SELECT file_id FROM files "
                     "WHERE file_key = %s)")
            params = (key,)
        rows = [row[0] for row in conn.execute(
            sql.SQL('SELECT to_jsonb(t) FROM {} t WHERE ' + where +
                    ' ORDER BY to_jsonb(t)::text')
            .format(sql.Identifier(table)), params).fetchall()]
        for field, dim in _DIM_VALUES.items():
            marker = field + "_id"
            if not any(marker in row for row in rows):
                continue
            values = dict(conn.execute(
                sql.SQL("SELECT id, value FROM {}").format(
                    sql.Identifier(dim))).fetchall())
            for row in rows:
                if marker in row:
                    row[field] = values.get(row[marker])
                    del row[marker]
    return [{name: value for name, value in row.items()
             if name != "file_id"} for row in rows]


def _counts(tables: tuple[str, ...]) -> dict[str, int]:
    counts = {}
    with db.viz_conn() as conn:
        for table in tables:
            row = conn.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(table))).fetchone()
            assert row is not None
            counts[table] = row[0]
    return counts


def _marker() -> tuple:
    with db.viz_conn() as conn:
        row = conn.execute(
            'SELECT fingerprint, complete, last_full_at FROM ingest_derived_state'
        ).fetchone()
    assert row is not None
    return row


def _policy_child_parse(key: str, sidecar: str | None, etag: str | None, size: int | None, *, tables: dict, expected: CollectionPolicy, collection: CollectionPolicy | None = None) -> dict:
    real = parse.parse_file

    def checked(file_key, blob, *, collection=None):
        assert collection is not None and collection == expected
        return real(file_key, blob, collection=collection)
    with patch.object(parse, 'parse_file', checked):
        return child_units.priced_parse(key, sidecar, etag, size, tables=tables, collection=collection)


def _faulted_child_parse(key: str, sidecar: str | None, etag: str | None, size: int | None, *, tables: dict, failed_key: str, stage: str, collection: CollectionPolicy | None = None) -> dict:
    target, name = {'fetch': (ingest, '_fetch_with_retry'), 'parse': (parse, 'parse_file')}[stage]
    real = getattr(target, name)

    def failing(file_key, *args, **kwargs):
        if file_key == failed_key:
            raise OSError('injected file failure')
        return real(file_key, *args, **kwargs)
    with patch.object(target, name, failing):
        return child_units.priced_parse(key, sidecar, etag, size, tables=tables, collection=collection)


def test_null_stamp_equals_default_without_reparse(fresh_db, mirror):
    key = _put(mirror, CLAUDE_KEY, _blob())
    _run()
    assert _rows('records', key)[0]['output_tokens'] > 0
    with patch.object(parse, 'parse_file', wraps=parse.parse_file) as spy:
        for stamp in (None, []):
            with db.viz_conn() as conn:
                conn.execute('UPDATE files SET collection_flags = %s', (stamp,))
                conn.commit()
            assert _run()['reparsed'] == 0
            assert spy.call_count == 0
        with db.viz_conn() as conn:
            conn.execute('UPDATE files SET parser_version = %s',
                         (str(int(parser_version.PARSER_VERSION) - 1),))
            conn.commit()
        assert _run()['reparsed'] == 1
        assert spy.call_count == 1


def test_newer_parser_version_protects_rows_even_when_policy_differs(
        fresh_db, mirror, monkeypatch):
    key = _put(mirror, CLAUDE_KEY, _blob())
    _run()
    assert _rows('records', key)[0]['turn_flags']
    _config(monkeypatch, 'turn_context')
    assert _run()['reparsed'] == 1
    assert _rows('records', key)[0]['turn_flags'] is None
    _config(monkeypatch)
    _run()
    with db.viz_conn() as conn:
        conn.execute('UPDATE files SET parser_version = %s',
                     (str(int(parser_version.PARSER_VERSION) + 1),))
        conn.commit()
    before = _rows('files', key), _rows('records', key)
    _config(monkeypatch, 'turn_context')
    with patch.object(parse, 'parse_file', wraps=parse.parse_file) as spy:
        result = _run()
    assert result['newer'] == 1 and result['reparsed'] == 0
    assert spy.call_count == 0
    assert (_rows('files', key), _rows('records', key)) == before


@pytest.mark.parametrize('layout', [CLAUDE_KEY, LANE_KEY])
@pytest.mark.parametrize('group', GROUPS)
def test_collection_flip_off_and_on_replaces_stored_rows(
        fresh_db, mirror, monkeypatch, group, layout):
    key = _put(mirror, layout, _blob())
    spec = next(s for s in COLLECTION_SPECS if s.key == group)
    _run()
    before = {t: _rows(t, key) for t in ('records', 'tool_uses', 'files')}
    assert before['records'] and before['tool_uses']
    fields = {'records': spec.record_fields, 'tool_uses': spec.tool_fields,
              'files': spec.file_fields}
    if spec.rollup_tables:
        assert all(n > 0 for n in _counts(spec.rollup_tables).values())
    _config(monkeypatch, group)
    assert _run()['reparsed'] == 1
    _assert_masked(before, key, fields)
    assert _rows('files', key)[0]['collection_flags'] == [group]
    if spec.rollup_tables:
        assert all(n == 0 for n in _counts(spec.rollup_tables).values())
    with patch.object(parse, 'parse_file', wraps=parse.parse_file) as spy:
        assert _run()['reparsed'] == 0
        assert spy.call_count == 0
    _config(monkeypatch)
    assert _run()['reparsed'] == 1
    assert _rows('files', key)[0]['collection_flags'] == []
    for table in before:
        for row in before[table]:
            row.pop('parsed_at', None)
        after = _rows(table, key)
        for row in after:
            row.pop('parsed_at', None)
        assert after == before[table]
    assert (mirror / layout).read_bytes() == _blob()


def _row_identity(row: dict) -> tuple:
    return row.get('line_num', 0), row.get('idx', 0)


def _assert_masked(before: dict, key: str, fields: dict) -> None:
    for table, names in fields.items():
        for name in names:
            assert any(row[name] not in (None, [], 0, '') for row in before[table]), (table, name)
        after = _rows(table, key)
        assert len(after) == len(before[table])
        for on, off in zip(sorted(before[table], key=_row_identity),
                           sorted(after, key=_row_identity)):
            for name in names:
                assert off[name] is None, (table, name)
            skipped = set(names) | {'parsed_at', 'collection_flags'}
            assert {k: v for k, v in on.items() if k not in skipped} == {
                k: v for k, v in off.items() if k not in skipped}


@pytest.mark.parametrize('timed', [False, True])
def test_thread_and_process_workers_use_parent_policy(
        fresh_db, mirror, monkeypatch, timed):
    key = _put(mirror, CLAUDE_KEY, _blob())
    side_key = 'proj/sess/subagents/agent-worker.jsonl'
    side = _put(mirror, side_key, (FIX / 'teammate_member.jsonl').read_bytes())
    _put(mirror, side_key.replace('.jsonl', '.meta.json'),
         b'{"agentType":"code-reviewer","toolUseId":"a"}')
    monkeypatch.setattr(timing, 'TIMING_ON', timed)
    real = parse.parse_file
    expected = flags.ALL_COLLECTIONS_ON

    def checked(file_key, blob, *, collection=None):
        # Threads use this spy; workers recreate it in _policy_child_parse.
        assert collection is not None and collection == expected
        return real(file_key, blob, collection=collection)

    monkeypatch.setattr(parse, 'parse_file', checked)
    _run()
    assert _rows('records', key)[0]['turn_flags']
    assert _rows('files', side)[0]['agent_type'] == 'code-reviewer'
    snapshots = []
    for processes in (1, 2):
        with db.viz_conn() as conn:
            conn.execute('DELETE FROM files')
            conn.commit()
        monkeypatch.setenv('CLAUDIT_COLLECT_TURN_CONTEXT', '1')
        _config(monkeypatch, 'turn_context')
        expected = flags.CONFIG.collection
        monkeypatch.setenv('CLAUDIT_INGEST_PARSE_PROCESSES', str(processes))
        if processes > 1:
            tables = {name: getattr(pricing, name) for name in pricing.RateTables.__annotations__}
            monkeypatch.setattr(ingest, '_fetch_and_parse', partial(_policy_child_parse, tables=tables, expected=expected))
        _run()
        assert _rows('records', key)[0]['turn_flags'] is None
        assert _rows('files', key)[0]['collection_flags'] == ['turn_context']
        assert _rows('files', side)[0]['agent_type'] == 'code-reviewer'
        snapshot = [_rows(t, key) for t in ('files', 'records', 'tool_uses')]
        snapshot[0][0].pop('parsed_at')
        snapshots.append(json.dumps(snapshot, sort_keys=True))
    assert snapshots[0] == snapshots[1]


def test_disabled_rollups_are_cleared_without_rebuild(
        fresh_db, mirror, monkeypatch):
    _put(mirror, CLAUDE_KEY, _blob())
    _run()
    optional = ('tool_error_rollup', 'dispatch_rollup', 'dispatch_brief_rollup')
    retained = ('usage_rollup', 'tool_rollup', 'agent_rollup', 'latency_rollup')
    assert all(n > 0 for n in _counts(optional + retained).values())
    with db.viz_conn() as conn:
        saved = {t: conn.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(t))).fetchall() for t in optional}
    on_marker = _marker()
    _config(monkeypatch, 'tool_error_kinds', 'dispatch_analytics')
    assert flags.CONFIG.features['tool_error_rate']
    assert flags.CONFIG.features['agent_comparison']
    with patch.object(ingest, 'rebuild_tool_error_rollup', side_effect=AssertionError), \
            patch.object(ingest, 'rebuild_dispatch_rollup', side_effect=AssertionError), \
            patch.object(ingest, 'rebuild_dispatch_brief_rollup', side_effect=AssertionError):
        assert _run()['reparsed'] == 1
        assert all(n == 0 for n in _counts(optional).values())
        assert all(n > 0 for n in _counts(retained).values())
        assert _marker()[0] != on_marker[0] and _marker()[1] is True
        # Old rows outside dirty hours still disappear on a zero-todo ingest.
        with db.viz_conn() as conn:
            for table, rows in saved.items():
                for row in rows:
                    query = sql.SQL('INSERT INTO {} VALUES ({})').format(
                        sql.Identifier(table), sql.SQL(',').join([sql.Placeholder()] * len(row)))
                    conn.execute(query, row)
            conn.commit()
        assert all(n > 0 for n in _counts(optional).values())
        assert _run()['reparsed'] == 0
        assert all(n == 0 for n in _counts(optional).values())
    _config(monkeypatch)
    _run()
    assert all(n > 0 for n in _counts(optional + retained).values())


def test_policy_flip_promotes_full_with_no_queued_files(fresh_db, mirror, monkeypatch):
    key = _put(mirror, CLAUDE_KEY, _blob())
    _run()
    assert _rows('records', key)[0]['turn_flags']
    previous = _marker()
    _config(monkeypatch, 'turn_context')
    # Land actual OFF output ahead of the run, leaving its ON derived fingerprint.
    existing = ingest._existing_files()  # pylint: disable=protected-access
    _, todo, _, _, _ = ingest._collect_todo(  # pylint: disable=protected-access
        existing, parser_version.PARSER_VERSION, [])
    obj, proj, _ = todo[0]
    parsed = ingest._fetch_and_parse(  # pylint: disable=protected-access
        obj.key, collection=flags.CONFIG.collection)
    ingest._persist(obj, proj, parsed, parser_version.PARSER_VERSION)  # pylint: disable=protected-access
    assert _rows('records', key)[0]['turn_flags'] is None
    assert _marker() == previous
    reached = []
    real = ingest._rebuild_derived_state  # pylint: disable=protected-access

    def checked():
        scope = ingest.current_scope()
        assert scope is not None
        reached.append((scope.full, scope.reason))
        return real()

    with patch.object(ingest, '_rebuild_derived_state', checked):
        assert _run()['reparsed'] == 0
    assert reached == [(True, 'derived fingerprint changed')]
    assert _marker()[2] > previous[2]


def test_persist_defends_transport_stamp_without_null_coercion(
        fresh_db, mirror, monkeypatch):
    key = _put(mirror, CLAUDE_KEY, _blob())
    _run()
    assert _rows('records', key)[0]['turn_flags']
    assert _rows('tool_uses', key)[0]['result_chars'] > 0
    groups = ('turn_context', 'context_intake', 'file_extras', 'generation_speed')
    spec_fields = {
        table: tuple(field for spec in COLLECTION_SPECS
                     if spec.key in groups
                     for field in getattr(spec, group))
        for table, group in (('records', 'record_fields'),
                             ('tool_uses', 'tool_fields'), ('files', 'file_fields'))}
    for table, fields in spec_fields.items():
        for field in fields:
            assert any(row[field] not in (None, [], 0, '')
                       for row in _rows(table, key)), (table, field)
    _config(monkeypatch, *groups)
    real = parse.parse_file

    def unmasked(file_key, blob, *, collection):
        assert collection == flags.CONFIG.collection
        # Simulate an adapter returning populated placeholders after compute gating.
        return real(file_key, blob)

    with patch.object(parse, 'parse_file', unmasked):
        _run()
    assert _rows('files', key)[0]['collection_flags'] == [
        'context_intake', 'file_extras', 'generation_speed', 'turn_context']
    for table, fields in spec_fields.items():
        for row in _rows(table, key):
            assert all(row[field] is None for field in fields)


def test_file_models_off_stays_null_after_adoption(fresh_db, mirror, monkeypatch):
    parent = _switching_parent_rollout()
    # Include a real replayed tool call as well as the fixture's billing rows.
    call = {'timestamp': '2026-06-14T11:00:02.500Z', 'type': 'response_item',
            'payload': {'type': 'function_call', 'call_id': 'shared-tool',
                        'name': 'exec_command', 'arguments': '{"cmd":"true"}'}}
    lines = parent.splitlines(keepends=True)
    lines.insert(2, json.dumps(call).encode() + b'\n')
    _put(mirror, 'sessions/proj/sess/wire.jsonl', b''.join(lines))
    fork = DIFFERENT_MODEL_FORK_FIXTURE.read_bytes().splitlines(keepends=True)
    fork.insert(1, json.dumps(call).encode() + b'\n')
    key = _put(mirror, 'sessions/proj/sess/subagents/fork/wire.jsonl', b''.join(fork))
    _run()
    assert 'gpt-5.6-sol' in _rows('files', key)[0]['models']
    before = _rows('records', key), _rows('tool_uses', key)
    assert any(r['model'] == 'gpt-5.6-sol' and r['is_replay'] for r in before[0])
    assert before[1][0]['model'] == 'gpt-5.6-sol'
    _config(monkeypatch, 'file_extras')
    _run()
    assert _rows('files', key)[0]['models'] is None
    assert (_rows('records', key), _rows('tool_uses', key)) == before
    # Reenable while this fork fails to fetch. New parent attribution still
    # adopts, but must not refill metadata last parsed under the OFF stamp.
    _config(monkeypatch)
    parent_path = mirror / 'sessions/proj/sess/wire.jsonl'
    parent_path.write_bytes(parent_path.read_bytes().replace(
        b'gpt-5.6-sol', b'replay-test-model'))
    real = ingest._fetch_with_retry  # pylint: disable=protected-access

    def failing(file_key):
        if file_key == key:
            raise OSError('injected fork failure')
        return real(file_key)

    with patch.object(ingest, '_fetch_with_retry', failing):
        result = ingest.run_ingest('collection-reenable-failure')
    assert result['failed'] == 1
    assert _rows('files', key)[0]['models'] is None
    assert _rows('files', key)[0]['collection_flags'] == ['file_extras']
    assert any(row['model'] == 'replay-test-model' for row in _rows('records', key))
    _run()
    assert 'replay-test-model' in _rows('files', key)[0]['models']


def test_analytics_off_still_resolves_teammate_role(fresh_db, mirror, monkeypatch):
    lead_rows = [json.loads(line) for line in (FIX / 'teammate_lead.jsonl').read_bytes().splitlines()]
    lead_rows[1]['message']['content'][0]['input']['model'] = 'haiku'
    _put(mirror, 'proj/sess/sess.jsonl', b''.join(
        json.dumps(row).encode() + b'\n' for row in lead_rows))
    member_key = 'proj/sess/subagents/agent-member.jsonl'
    key = _put(mirror, member_key, (FIX / 'teammate_member.jsonl').read_bytes())
    _put(mirror, member_key.replace('.jsonl', '.meta.json'), json.dumps({
        'agentType': 'migrate-rest', 'name': 'migrate-rest',
        'taskKind': 'in_process_teammate'}).encode())
    _run()
    lead = 'claude/proj/sess/sess.jsonl'
    on = _rows('tool_uses', lead)[0]
    assert on['agent_model'] and on['dispatch_prompt_chars'] > 0
    assert _rows('files', key)[0]['agent_type'] == 'implementer'
    with db.viz_conn() as conn:
        before = conn.execute('SELECT * FROM agent_rollup ORDER BY 1,2,3,4').fetchall()
    assert before
    _config(monkeypatch, 'dispatch_analytics')
    _run()
    off = _rows('tool_uses', lead)[0]
    assert off['agent_model'] is None and off['dispatch_prompt_chars'] is None
    assert off['agent_type'] == on['agent_type']
    assert off['dispatch_name'] == on['dispatch_name'] == 'migrate-rest'
    assert _rows('files', key)[0]['agent_type'] == 'implementer'
    assert _rows('files', key)[0]['teammate_name'] == 'migrate-rest'
    with db.viz_conn() as conn:
        assert conn.execute('SELECT * FROM agent_rollup ORDER BY 1,2,3,4').fetchall() == before


@pytest.mark.parametrize('processes', [1, 2])
@pytest.mark.parametrize('stage', ['fetch', 'parse', 'persist'])
def test_failed_file_keeps_old_policy_stamp_and_retries(
        fresh_db, mirror, monkeypatch, stage, processes):
    key = _put(mirror, CLAUDE_KEY, _blob())
    peer = _put(mirror, LANE_KEY, _blob())
    _run()
    assert _rows('records', key)[0]['turn_flags']
    assert _rows('records', peer)[0]['turn_flags']
    assert _rows('files', key)[0]['collection_flags'] == []
    before = _rows('files', key), _rows('records', key)
    _config(monkeypatch, 'turn_context')
    monkeypatch.setenv('CLAUDIT_INGEST_PARSE_PROCESSES', str(processes))
    target, name = {'fetch': (ingest, '_fetch_with_retry'),
                    'parse': (parse, 'parse_file'), 'persist': (ingest, '_persist')}[stage]
    real = getattr(target, name)

    def failing(*args, **kwargs):
        file_key = args[0].key if stage == 'persist' else args[0]
        if file_key == key:
            if stage == 'persist':
                # A duplicate tool PK fails AFTER the file UPSERT, inside its transaction.
                args[2]['tool_uses'].append(dict(args[2]['tool_uses'][0]))
            else:
                raise OSError('injected file failure')
        return real(*args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(target, name, failing))
        if processes > 1 and stage != 'persist':
            tables = {name: getattr(pricing, name) for name in pricing.RateTables.__annotations__}
            stack.enter_context(patch.object(ingest, '_fetch_and_parse', partial(_faulted_child_parse, tables=tables, failed_key=key, stage=stage)))
        result = ingest.run_ingest('collection-failure')
    assert result['failed'] == 1 and result['error']
    assert (_rows('files', key), _rows('records', key)) == before
    assert _rows('files', peer)[0]['collection_flags'] == ['turn_context']
    assert _rows('records', peer)[0]['turn_flags'] is None
    assert _marker()[1] is False
    assert _run()['reparsed'] == 1
    assert _rows('files', key)[0]['collection_flags'] == ['turn_context']
    assert _rows('records', key)[0]['turn_flags'] is None
    assert _marker()[1] is True


def test_aborted_collection_rebuild_remains_incomplete(fresh_db, mirror, monkeypatch):
    _put(mirror, CLAUDE_KEY, _blob())
    _run()
    assert _counts(('dispatch_rollup',))['dispatch_rollup'] > 0
    _config(monkeypatch, 'dispatch_analytics')
    with patch.object(ingest, 'rebuild_rollup', side_effect=ingest.IngestAborted('test')):
        result = ingest.run_ingest('collection-abort')
    assert result['aborted'] is True
    assert _marker()[1] is False
    assert _run()['reparsed'] == 0
    assert _counts(('dispatch_rollup',))['dispatch_rollup'] == 0
    assert _marker()[1] is True
