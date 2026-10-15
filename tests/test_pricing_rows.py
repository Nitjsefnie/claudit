"""Provider storage rows preserve values and reject ambiguous histories."""
from __future__ import annotations

import copy
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from backend import pricing_document
ROOT = Path(__file__).resolve().parents[1]
ROW_KEY, HOST = 'anthropic/synthetic-9', 'OpenInference'


def _doc():
    return {
        'models': {'claude-opus-4-7': [{'fresh': 3, 'output': 15, 'read': 0.3}]},
        'providers': {ROW_KEY: {HOST: []}},
        'provider_rates_fetched': '2026-09-24T22:03:13Z',
        'long_context_meters': [],
        'openrouter': {'models': {ROW_KEY: {'id': ROW_KEY}},
                       'vendor': {'prefixes': ['anthropic']}},
    }


def _codec():
    from backend import pricing_rows  # pylint: disable=import-outside-toplevel
    return pricing_rows


def _typed(value):
    if isinstance(value, dict):
        return {key: _typed(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_typed(item) for item in value]
    return type(value).__name__, value


def _fixture():
    doc = _doc()
    doc['providers'][ROW_KEY][HOST] = [
        {'from': None, 'fresh': 1, 'output': 2.0, 'read': 0.1},
        {'from': ['2026-01-01T00:00:00Z', '2026-01-03T00:00:00Z'],
         'fresh': 2.0, 'output': 3, 'read': 0, 'note': '20% off'},
        {'from': '2026-01-02T00:00:00Z', 'fresh': 3, 'output': 4,
         'read': 0.0, 'note': '20.0% off', 'create_5m': 4.5,
         'create_1h': 6, 'web_search': 0.012,
         'band': {'fresh': [2, 4.0], 'create_5m': [2, 5], 'create_1h': [2, 6]},
         'schedule': [{'start': 0, 'end': 100, 'rates': {
             'fresh': 1, 'output': 2, 'read': 0}}]},
        {'from': '2026-01-04T00:00:00Z', 'fresh': 4, 'output': 5,
         'read': 0, 'web_search': 0.01},
    ]
    return doc


def test_rows_round_trip_all_slots_and_types_without_mutation():
    codec = _codec()
    doc = _fixture()
    before = copy.deepcopy(doc)
    encoded = codec.encode_pricing_doc(doc)
    rows = encoded['providers'][ROW_KEY][HOST]
    assert rows[0] == [None, 1, 2.0, 0.1]
    assert rows[1] == [[1767225600, 172800], 2.0, 3, 0, 20]
    assert rows[2][:5] == [86400, 3, 4, 0.0, '20.0% off']
    assert rows[2][5]['create_1h'] == 6
    assert rows[3] == [172800, 4, 5, 0, {'web_search': 0.01}]
    assert encoded['models'] == doc['models']
    assert _typed(codec.decode_pricing_doc(encoded)) == _typed(doc)
    assert _typed(doc) == _typed(before)
    assert codec.migrate_pricing_doc(encoded) == encoded
    assert codec.migrate_pricing_doc(doc) == encoded


@pytest.mark.parametrize('seed', range(12))
def test_generated_rate_vectors_round_trip_with_exact_numeric_types(seed):
    rng = random.Random(seed)
    doc = _fixture()
    for entry in doc['providers'][ROW_KEY][HOST]:
        for field in ('fresh', 'read', 'output'):
            value = rng.randrange(0, 100)
            entry[field] = value if rng.choice([True, False]) else float(value)
        entry['note'] = rng.choice(['0% off', '12.5% off', '001% off', '', 'special'])
    codec = _codec()
    stored = json.loads(json.dumps(codec.encode_pricing_doc(doc)))
    assert _typed(codec.decode_pricing_doc(stored)) == _typed(doc)


def _node(tmp_path, doc, browser=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / 'pricing.json').write_text(json.dumps(doc), encoding='utf-8')
    for name in ('pricing-loader.js', 'hhmm-spelling.js', 'vendor-tables.js', 'pricing-rows.js'):
        shutil.copy(ROOT / 'src' / name, tmp_path / name)
    script = f"""
      global.window = {{}};
      if ({str(browser).lower()}) {{
        global.document = {{ currentScript: {{ dataset: {{ pricing: 'pricing.json' }},
          src: require('url').pathToFileURL({str(tmp_path / 'pricing-loader.js')!r}).href }} }};
        global.XMLHttpRequest = class {{
          open(method, url) {{ this.url = url; }}
          setRequestHeader() {{}}
          send() {{ this.status = 200; this.responseURL = this.url;
            this.responseText = require('fs').readFileSync({str(tmp_path / 'pricing.json')!r}, 'utf8'); }}
        }};
      }}
      let error = null, decoded = null;
      try {{
        const loader = require({str(tmp_path / 'pricing-loader.js')!r});
        decoded = loader.decodePricingDoc({json.dumps(doc)});
      }} catch (e) {{ error = e.message; }}
      console.log(JSON.stringify({{error, decoded, tables: window}}));
    """
    result = subprocess.run(['node', '-e', script], capture_output=True,
                            text=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which('node') is None, reason='node not available')
@pytest.mark.parametrize("browser", [False, True])
def test_browser_decodes_like_python_and_flattens_all_instants(tmp_path, browser):
    codec = _codec()
    stored = codec.encode_pricing_doc(_fixture())
    got = _node(tmp_path, stored, browser=browser)
    assert got['error'] is None, got['error']
    assert got['decoded'] == codec.decode_pricing_doc(stored)
    from backend.pricing_load import load_tables  # pylint: disable=import-outside-toplevel
    tables = load_tables(stored)
    assert tables['PROVIDER_RATES'][ROW_KEY, HOST]['fresh'] == 4
    windows = tables['PROVIDER_DATED_RATES'][ROW_KEY, HOST]
    assert [rates['fresh'] for _, rates in windows] == [1, 2, 3, 2]
    assert [end.day for end, _ in windows] == [1, 2, 3, 4]
    assert [w['rates']['fresh'] for w in got['tables']['providerDatedRates'][ROW_KEY][HOST]] == [1, 2, 3, 2]
    assert tables['PROVIDER_SCHEDULES'][ROW_KEY, HOST].keys() == {2}
    assert tables['PROVIDER_BANDS'][ROW_KEY, HOST].keys() == {2}
    assert got['tables']['providerSchedules'][ROW_KEY][HOST].keys() == {'2'}
    assert got['tables']['providerBands'][ROW_KEY][HOST].keys() == {'2'}


BAD_ROWS = [
    [None, 1, 2], [None, 1, 2, 0, {}, 'extra'],
    [None, 1, 2, 0, True], [None, 1, 2, 0, {'fresh': 8}],
    [-1, 1, 2, 0], [0.5, 1, 2, 0], [True, 1, 2, 0],
    [[], 1, 2, 0], [[1, -1], 1, 2, 0], [[1, 0], 1, 2, 0],
    [[1, False], 1, 2, 0], [253402300800, 1, 2, 0],
    [1.0, 1, 2, 0],
]


@pytest.mark.parametrize('row', BAD_ROWS)
def test_invalid_rows_refused_naming_the_provider_row(row):
    codec = _codec()
    doc = codec.encode_pricing_doc(_fixture())
    doc['providers'][ROW_KEY][HOST] = [row]
    with pytest.raises(ValueError, match=r'via OpenInference\[0\]'):
        codec.decode_pricing_doc(doc)


@pytest.mark.skipif(shutil.which('node') is None, reason='node not available')
@pytest.mark.parametrize('row', BAD_ROWS)
@pytest.mark.parametrize('browser', [False, True])
def test_browser_refusal_names_the_same_row(tmp_path, row, browser):
    codec = _codec()
    doc = codec.encode_pricing_doc(_fixture())
    doc['providers'][ROW_KEY][HOST] = [row]
    got = _node(tmp_path, doc, browser=browser)
    assert got['error'] and f'{ROW_KEY} via {HOST}[0]' in got['error'], got


def test_missing_marker_and_legacy_objects_refused_at_storage_boundary():
    codec = _codec()
    doc = codec.encode_pricing_doc(_fixture())
    doc.pop('_format')
    with pytest.raises(ValueError, match='_format'):
        pricing_document.check_compact_shape(doc)
    doc = _fixture()
    doc['_format'] = 'pricing-rows/3'
    with pytest.raises(ValueError, match=r'via OpenInference\[0\]'):
        pricing_document.check_compact_shape(doc)


def test_serializer_stores_rows_and_expand_restores_editable_entries():
    doc = _fixture()
    codec = _codec()
    raw = json.loads(pricing_document.serialize_pricing_doc(doc))
    pricing_document.check_compact_shape(raw)
    assert isinstance(raw['providers'][ROW_KEY][HOST][0], list)
    assert _typed(pricing_document.expand_pricing_doc(raw)) == _typed(
        pricing_document.expand_pricing_doc(doc))
    assert codec.decode_pricing_doc(raw)['providers'][ROW_KEY][HOST][1]['from'] == [
        '2026-01-01T00:00:00Z', '2026-01-03T00:00:00Z']


def test_serializer_keeps_each_provider_row_on_one_compact_line():
    doc = _fixture()
    doc['providers'][ROW_KEY][HOST][0]['note'] = 'café "rows"\nnext'
    doc['zz_layout'] = {'list': ['first', 'second']}
    serialized = pricing_document.serialize_pricing_doc(doc)
    stored = json.loads(serialized)
    row_lines = [line.strip().removesuffix(',') for line in serialized.splitlines()
                 if line.startswith('        [')]
    assert row_lines[0] == r'[null,1,2.0,0.1,"café \"rows\"\nnext"]'
    assert row_lines[1] == '[[1767225600,172800],2.0,3,0,20]'
    assert [json.loads(line) for line in row_lines] == stored['providers'][ROW_KEY][HOST]
    standard = json.dumps(stored, indent=2, sort_keys=True, allow_nan=False) + '\n'
    assert len(serialized.encode()) < len(standard.encode())
    assert serialized.split('  "providers": ', 1)[0] == standard.split('  "providers": ', 1)[0]
    assert serialized.split('  "zz_layout": ', 1)[1] == standard.split('  "zz_layout": ', 1)[1]


def test_serializer_is_idempotent_and_loads_the_same_tables():
    from backend.pricing_load import load_tables  # pylint: disable=import-outside-toplevel
    doc = _fixture()
    before = copy.deepcopy(doc)
    serialized = pricing_document.serialize_pricing_doc(doc)
    stored = json.loads(serialized)
    decoded = _codec().decode_pricing_doc(stored)
    assert pricing_document.serialize_pricing_doc(decoded) == serialized
    assert pricing_document.serialize_pricing_doc(stored) == serialized
    assert load_tables(stored) == load_tables(doc)
    assert _typed(doc) == _typed(before)


@pytest.mark.skipif(shutil.which('node') is None, reason='node not available')
def test_node_loader_reads_the_serialized_row_layout(tmp_path):
    from backend.pricing_load import load_tables  # pylint: disable=import-outside-toplevel
    serialized = pricing_document.serialize_pricing_doc(_fixture())
    (tmp_path / 'pricing.json').write_text(serialized, encoding='utf-8')
    for name in ('pricing-loader.js', 'hhmm-spelling.js', 'vendor-tables.js', 'pricing-rows.js'):
        shutil.copy(ROOT / 'src' / name, tmp_path / name)
    script = """
      global.window = {};
      const fs = require('fs');
      const loader = require('./pricing-loader.js');
      console.log(JSON.stringify({
        decoded: loader.decodePricingDoc(JSON.parse(fs.readFileSync('pricing.json', 'utf8'))),
        rates: window.providerRates,
        windows: window.providerDatedRates
      }));
    """
    result = subprocess.run(['node', '-e', script], cwd=tmp_path, capture_output=True,
                            text=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout)
    stored = json.loads(serialized)
    assert got['decoded'] == _codec().decode_pricing_doc(stored)
    tables = load_tables(stored)
    fields = [('fresh', 'fresh'), ('c5', 'create_5m'), ('c1h', 'create_1h'),
              ('read', 'read'), ('out', 'output'), ('search', 'web_search')]
    browser_rates = [got['rates'][ROW_KEY][HOST],
                     *[w['rates'] for w in got['windows'][ROW_KEY][HOST]]]
    python_rates = [tables['PROVIDER_RATES'][ROW_KEY, HOST],
                    *[rates for _, rates in tables['PROVIDER_DATED_RATES'][ROW_KEY, HOST]]]
    assert [[rates.get(js, 0) for js, _ in fields] for rates in browser_rates] == [
        [rates.get(py, 0) for _, py in fields] for rates in python_rates]


@pytest.mark.parametrize('instants', [
    ['2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z'],
    ['2026-01-01T00:00:00Z', '2026-12-01T00:00:00Z'],
])
def test_lists_refuse_duplicate_instants_and_every_instant_after_fetch(instants):
    doc = _fixture()
    doc['providers'][ROW_KEY][HOST][1]['from'] = instants
    from backend.pricing_load import load_tables  # pylint: disable=import-outside-toplevel
    with pytest.raises(ValueError, match=r'via OpenInference\['):
        load_tables(doc)


def test_long_decimal_discount_is_not_rounded_during_encoding():
    doc = _fixture()
    doc['providers'][ROW_KEY][HOST][1]['note'] = '12.3456789% off'
    codec = _codec()
    assert codec.decode_pricing_doc(codec.encode_pricing_doc(doc)) == doc


def test_migration_normalizes_absent_from_without_mutating_the_legacy_doc():
    doc = _fixture()
    del doc['providers'][ROW_KEY][HOST][0]['from']
    before = copy.deepcopy(doc)
    migrated = _codec().migrate_pricing_doc(doc)
    assert migrated['providers'][ROW_KEY][HOST][0][0] is None
    assert _codec().decode_pricing_doc(migrated)['providers'][ROW_KEY][HOST][0]['from'] is None
    assert _typed(doc) == _typed(before)


@pytest.mark.skipif(shutil.which('node') is None, reason='node not available')
@pytest.mark.parametrize('note', ['12.3456789% off', '1000000000000000000000% off'])
def test_numeric_discount_encoding_preserves_the_same_note_in_both_loaders(tmp_path, note):
    doc = _fixture()
    doc['providers'][ROW_KEY][HOST][1]['note'] = note
    stored = _codec().encode_pricing_doc(doc)
    got = _node(tmp_path, stored)
    assert got['error'] is None, got['error']
    assert got['decoded']['providers'][ROW_KEY][HOST][1]['note'] == note
    assert _codec().decode_pricing_doc(stored)['providers'][ROW_KEY][HOST][1]['note'] == note


def test_encoder_refuses_reused_instants_across_timestamp_lists():
    doc = _fixture()
    doc['providers'][ROW_KEY][HOST][1]['from'][1] = '2026-01-02T00:00:00Z'
    with pytest.raises(ValueError, match=r'via OpenInference\[2\]'):
        _codec().encode_pricing_doc(doc)


@pytest.mark.skipif(shutil.which('node') is None, reason='node not available')
def test_list_price_belongs_to_the_latest_occurrence_and_schedules_repeat(tmp_path):
    from backend.pricing_load import load_tables  # pylint: disable=import-outside-toplevel
    doc = _fixture()
    row = doc['providers'][ROW_KEY][HOST][1]
    row['from'][1] = '2026-01-05T00:00:00Z'
    row['schedule'] = [{'rates': {'fresh': 1, 'output': 2, 'read': 0}}]
    stored = _codec().encode_pricing_doc(doc)
    tables = load_tables(stored)
    got = _node(tmp_path, stored)
    assert got['error'] is None, got['error']
    assert tables['PROVIDER_RATES'][ROW_KEY, HOST]['fresh'] == 2.0
    assert got['tables']['providerRates'][ROW_KEY][HOST]['fresh'] == 2.0
    assert list(tables['PROVIDER_SCHEDULES'][ROW_KEY, HOST]) == [1, 2, 4]
    assert list(got['tables']['providerSchedules'][ROW_KEY][HOST]) == ['1', '2', '4']


@pytest.mark.skipif(shutil.which('node') is None, reason='node not available')
@pytest.mark.parametrize('stamps', [
    ['2026-01-01T00:00:00Z'],
    ['2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z'],
])
def test_a_single_grouped_state_has_a_start_and_keeps_every_window(tmp_path, stamps):
    from backend.pricing_load import load_tables  # pylint: disable=import-outside-toplevel
    doc = _fixture()
    doc['providers'][ROW_KEY][HOST] = [{'from': stamps, 'fresh': 1, 'output': 2, 'read': 0}]
    stored = _codec().encode_pricing_doc(doc)
    tables = load_tables(stored)
    got = _node(tmp_path, stored)
    assert got['error'] is None, got['error']
    assert tables['PROVIDER_STARTS'][ROW_KEY, HOST].isoformat() == '2026-01-01T00:00:00+00:00'
    assert len(tables['PROVIDER_DATED_RATES'].get((ROW_KEY, HOST), [])) == len(stamps) - 1
    assert len(got['tables']['providerDatedRates'].get(ROW_KEY, {}).get(HOST, [])) == len(stamps) - 1


@pytest.mark.skipif(shutil.which('node') is None, reason='node not available')
def test_every_grouped_instant_is_bounded_by_the_fetch_in_both_loaders(tmp_path):
    from backend.pricing_load import load_tables  # pylint: disable=import-outside-toplevel
    doc = _fixture()
    doc['providers'][ROW_KEY][HOST][1]['from'][1] = '2026-12-01T00:00:00Z'
    stored = _codec().encode_pricing_doc(doc)
    with pytest.raises(ValueError, match=r'via OpenInference\[1\]:.*after provider_rates_fetched'):
        load_tables(stored)
    got = _node(tmp_path, stored)
    assert got['error'] and 'OpenInference[1]' in got['error']
    assert 'after provider_rates_fetched' in got['error']


@pytest.mark.parametrize('damage', [
    lambda d: d.pop('_format'),
    lambda d: d.update(_format='other'),
    lambda d: d.update(providers=[]),
    lambda d: d['providers'].update({ROW_KEY: []}),
    lambda d: d['providers'][ROW_KEY].update({HOST: {}}),
    lambda d: d['providers'][ROW_KEY].update({HOST: []}),
    lambda d: d['providers'][ROW_KEY][HOST].append([None, 1, 2, 0]),
])
def test_storage_boundary_refuses_bad_markers_and_containers(damage):
    stored = _codec().encode_pricing_doc(_fixture())
    damage(stored)
    with pytest.raises(ValueError):
        pricing_document.check_compact_shape(stored)


def test_legacy_offset_timestamp_encodes_as_the_same_utc_instant():
    doc = _fixture()
    doc['providers'][ROW_KEY][HOST][2]['from'] = '2026-01-02T05:30:00+05:30'
    decoded = _codec().decode_pricing_doc(_codec().encode_pricing_doc(doc))
    assert decoded['providers'][ROW_KEY][HOST][2]['from'] == '2026-01-02T00:00:00Z'


def test_raw_load_decodes_checks_then_expands_once(monkeypatch):
    from backend import pricing_load  # pylint: disable=import-outside-toplevel
    stored = _codec().encode_pricing_doc(_fixture())
    steps = []
    for name in ('decode_pricing_doc', 'check_compact_shape', 'expand_pricing_doc'):
        original = getattr(pricing_load, name)

        def traced(*args, _name=name, _original=original, **kwargs):
            steps.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(pricing_load, name, traced)
    tables = pricing_load.load_tables(stored)
    assert tables['PROVIDER_RATES'][ROW_KEY, HOST]['fresh'] == 4
    assert steps == ['decode_pricing_doc', 'check_compact_shape', 'expand_pricing_doc']


@pytest.mark.skipif(shutil.which('node') is None, reason='node not available')
@pytest.mark.parametrize('number', [1e-7, 1e-6, 1e20, 1e21, -0.0, 12.3456789])
def test_numeric_note_rendering_matches_javascript(tmp_path, number):
    stored = _codec().encode_pricing_doc(_fixture())
    stored['providers'][ROW_KEY][HOST][1][4] = number
    got = _node(tmp_path, stored)
    assert got['error'] is None, got['error']
    assert got['decoded'] == _codec().decode_pricing_doc(stored)
