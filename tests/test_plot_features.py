from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, api_export
from backend.flag_registry import FEATURE_SPECS
from tests.test_api_export_argv import _load_plot_db_module
from tests.test_feature_serving import policy_fixture

__all__ = ["policy_fixture"]

DRAWABLE = ('usage_timeseries', 'model_comparison', 'token_breakdown', 'burn_rate')
requires_matplotlib = pytest.mark.skipif(
    importlib.util.find_spec('matplotlib') is None,
    reason='matplotlib ships in requirements-dev.txt, not the test legs')


@pytest.fixture(name='plot')
def plot_modules(monkeypatch, request):
    mod = _load_plot_db_module(monkeypatch, request)
    monkeypatch.setattr(sys.modules['ccusage_plot_timeline'],
                        'get_claude_info', lambda: ('', ''))
    return mod


@pytest.fixture(name='events')
def synthetic_events() -> list[dict]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [{'timestamp': start + timedelta(minutes=10 * i), 'model': 'sample',
             'inputTokens': 100, 'outputTokens': 200, 'cacheCreateTokens': 300,
             'cacheReadTokens': 400, 'totalTokens': 1000, 'costUSD': 0.5}
            for i in range(4)]


def _render(monkeypatch, plot, events, disabled=()):
    from matplotlib.figure import Figure  # pylint: disable=import-outside-toplevel
    captured = []
    monkeypatch.setattr(Figure, 'savefig', lambda fig, *a, **kw: captured.append(fig))
    hits = Mock(return_value=[{'ts': events[1]['timestamp'], 'text': 'limited'}])
    kwargs = {'disabled_features': frozenset(disabled)} if disabled else {}
    plot.plot_timeline(events, '1h', 'unused.png', limit_hits=hits, **kwargs)
    return captured, hits


def _texts(fig) -> list[str]:
    from matplotlib.text import Text  # pylint: disable=import-outside-toplevel
    return [text.get_text() for text in fig.findobj(Text)]


def _rate_marks(fig) -> list:
    return [line for ax in fig.axes for line in ax.lines
            if line.get_zorder() == 9 and line.get_linewidth() == 2]


@requires_matplotlib
def test_rate_limit_off_removes_markers_legend_and_count(monkeypatch, plot, events):
    on, hits = _render(monkeypatch, plot, events)
    assert hits.call_count == 1
    assert len(_rate_marks(on[0])) == 1
    assert 'Rate limit hit' in _texts(on[0])
    off, hits = _render(monkeypatch, plot, events, ['rate_limits'])
    assert hits.call_count == 0
    assert not _rate_marks(off[0])
    assert not any('rate limit' in text.lower() for text in _texts(off[0]))
    assert any('Session Burn Rate' in text for text in _texts(off[0]))


@requires_matplotlib
@pytest.mark.parametrize('key,label,neighbor,axes_removed', [
    ('usage_timeseries', 'Input Tokens', 'Cost by Model', 12),
    ('model_comparison', 'Cost by Model', 'Token Breakdown', 1),
    ('token_breakdown', 'Token Breakdown', 'Cost by Model', 1),
    ('burn_rate', 'Session Burn Rate', 'Cost by Model', 1),
    ('summary', '4 API calls', 'Cost by Model', 0),
])
def test_disabled_png_group_allocates_no_axes_or_static_labels(
        monkeypatch, plot, events, key, label, neighbor, axes_removed):
    on, _ = _render(monkeypatch, plot, events)
    assert any(label in text for text in _texts(on[0]))
    off, hits = _render(monkeypatch, plot, events, [key])
    assert not any(label in text for text in _texts(off[0]))
    assert any(neighbor in text for text in _texts(off[0]))
    assert len(off[0].axes) == len(on[0].axes) - axes_removed
    assert all(ax.get_visible() for ax in off[0].axes)
    if key == 'usage_timeseries':
        assert not any(ax.get_ylabel() in ('cumulative', 'per 1min')
                       for ax in off[0].axes)
    if key == 'burn_rate':
        assert hits.call_count == 0
        assert 'Rate limit hit' in _texts(on[0])
        assert 'Rate limit hit' not in _texts(off[0])
    if key == 'summary':
        assert not any(' total' in text for text in _texts(off[0]))
        assert any(text.startswith('Total:') for text in _texts(off[0]))
        assert any(' total' in text for text in _texts(on[0]))


@requires_matplotlib
def test_all_drawable_groups_off_allocates_no_figure(monkeypatch, plot, events):
    on, hits = _render(monkeypatch, plot, events)
    assert on and hits.call_count == 1
    off, hits = _render(monkeypatch, plot, events, DRAWABLE)
    assert not off and hits.call_count == 0


@requires_matplotlib
def test_all_on_keeps_axes_order_and_layout(monkeypatch, plot, events):
    # Frozen all-ON bounds (x, y, width, height), independent of the layout helper.
    positions = [
        (0.125, 0.807326732673, 0.336956521739, 0.132673267327),
        (0.563043478261, 0.807326732673, 0.336956521739, 0.132673267327),
        (0.125, 0.628217821782, 0.336956521739, 0.132673267327),
        (0.563043478261, 0.628217821782, 0.336956521739, 0.132673267327),
        (0.125, 0.449108910891, 0.336956521739, 0.132673267327),
        (0.563043478261, 0.449108910891, 0.336956521739, 0.132673267327),
        (0.125, 0.27, 0.336956521739, 0.132673267327),
        (0.563043478261, 0.27, 0.336956521739, 0.132673267327),
        (0.125, 0.03, 0.775, 0.18),
    ]
    captured, _ = _render(monkeypatch, plot, events)
    fig = captured[0]
    assert [ax.get_title().split('  |')[0] for ax in fig.axes[:9]] == [
        'Input Tokens', 'Output Tokens', 'Cache Create Tokens', 'Cache Read Tokens',
        'Total Tokens', 'Cost (USD)', 'Cost by Model', 'Token Breakdown',
        'Session Burn Rate']
    for ax, expected in zip(fig.axes[:9], positions):
        assert tuple(ax.get_position().bounds) == pytest.approx(expected, rel=0, abs=1e-9)
    assert tuple(fig.get_size_inches()) == (18, 26)


def test_export_argv_carries_server_policy_and_child_uses_it(monkeypatch, plot, policy):
    expected = [api_export._EXPORT_PYTHON, api_export._EXPORT_SCRIPT,  # pylint: disable=protected-access
                '--output=out.png', '--period=7d', '--project=sample']
    assert api_export.build_export_argv('7d', 'sample', 'out.png') == expected
    policy(env={'CLAUDIT_HIDE_RATE_LIMITS': '1'})
    argv = api_export.build_export_argv('7d', 'sample', 'out.png')
    assert argv[:-1] == expected
    assert argv[-1].startswith('--disabled-features=')
    assert json.loads(argv[-1].split('=', 1)[1]) == ['rate_limits']
    monkeypatch.setenv('CLAUDIT_HIDE_RATE_LIMITS', '0')
    monkeypatch.setenv('CLAUDIT_FEATURE_RATE_LIMITS', '1')
    monkeypatch.setenv('CLAUDIT_FEATURE_BURN_RATE', '0')
    args = plot._build_parser().parse_args(argv[2:])  # pylint: disable=protected-access
    features = sys.modules['ccusage_plot_features']
    assert features.resolve_features(args.disabled_features) == frozenset({'rate_limits'})


@pytest.mark.parametrize('env', [
    {'CLAUDIT_FEATURE_RATE_LIMITS': '0'},
    {'CLAUDIT_HIDE_RATE_LIMITS': '1', 'CLAUDIT_FEATURE_RATE_LIMITS': '1'},
])
def test_direct_cli_resolves_env(monkeypatch, plot, events, env):
    for spec in FEATURE_SPECS:
        monkeypatch.delenv(spec.env, raising=False)
    monkeypatch.delenv('CLAUDIT_HIDE_RATE_LIMITS', raising=False)
    policies = []
    monkeypatch.setattr(plot, 'load_events', lambda *a, **kw: events)
    monkeypatch.setattr(plot, '_resolve_db_url', lambda _: 'unused')
    monkeypatch.setattr(plot, 'plot_timeline', lambda *a, **kw: policies.append(kw))
    monkeypatch.setattr(sys, 'argv', ['plot', '--all'])
    plot.main()
    assert not policies[-1]['disabled_features']
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    plot.main()
    assert policies[-1]['disabled_features'] == frozenset({'rate_limits'})


@pytest.mark.parametrize('disabled', [DRAWABLE, ('project_picker',)])
def test_cli_rejects_disabled_work_before_db(monkeypatch, plot, events, disabled):
    load = Mock(return_value=events)
    render = Mock()
    monkeypatch.setattr(plot, 'load_events', load)
    monkeypatch.setattr(plot, 'plot_timeline', render)
    monkeypatch.setattr(plot, '_resolve_db_url', lambda _: 'unused')
    argv = ['plot', '--all', '--project=sample']
    monkeypatch.setattr(sys, 'argv', argv)
    plot.main()
    assert load.call_count == render.call_count == 1
    load.reset_mock()
    render.reset_mock()
    monkeypatch.setattr(sys, 'argv', argv + ['--disabled-features=' + json.dumps(disabled)])
    with pytest.raises(SystemExit) as exc:
        plot.main()
    assert exc.value.code == (3 if disabled == DRAWABLE else 2)
    assert load.call_count == render.call_count == 0


@pytest.mark.parametrize('value', ['{}', 'null', '[1]', '["unknown"]', 'bad'])
def test_cli_validates_disabled_feature_keys(plot, value):
    parser = plot._build_parser()  # pylint: disable=protected-access
    assert parser.parse_args(['--disabled-features=["rate_limits"]']).disabled_features
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(['--disabled-features=' + value])
    assert exc.value.code == 2


@pytest.mark.parametrize('disabled', [('export_png',), DRAWABLE])
def test_export_route_off_precedes_lock_tempfile_and_spawn(monkeypatch, disabled, policy):
    app = FastAPI()
    app.include_router(api.router)
    render = Mock()

    async def write_png(_argv, path):
        render()
        Path(path).write_bytes(b'PNG')

    monkeypatch.setattr(api_export, '_render_export', write_png)
    with TestClient(app) as client:
        assert client.get('/api/export?range=7d').status_code == 200
        assert render.call_count == 1
        render.reset_mock()
        policy(env={spec.env: '0' for spec in FEATURE_SPECS
                    if spec.key in disabled})
        lock = Mock()
        tempfile = Mock()
        monkeypatch.setattr(api_export, '_export_lock', lock)
        monkeypatch.setattr(api_export.tempfile, 'mkstemp', tempfile)
        assert client.get('/api/export?range=7d').status_code == 404
        assert lock.mock_calls == tempfile.mock_calls == render.mock_calls == []


def test_explicit_empty_policy_overrides_child_env(monkeypatch, plot):
    monkeypatch.setenv('CLAUDIT_HIDE_RATE_LIMITS', '1')
    parser = plot._build_parser()  # pylint: disable=protected-access
    resolver = sys.modules['ccusage_plot_features'].resolve_features
    assert 'rate_limits' in resolver(parser.parse_args([]).disabled_features)
    assert not resolver(parser.parse_args(['--disabled-features=[]']).disabled_features)


@requires_matplotlib
@pytest.mark.parametrize('enabled,title,count', [
    ('usage_timeseries', 'Input Tokens', 12),
    ('model_comparison', 'Cost by Model', 1),
    ('token_breakdown', 'Token Breakdown', 1),
    ('burn_rate', 'Session Burn Rate', 1),
])
def test_one_surviving_group_has_no_empty_grid_rows(
        monkeypatch, plot, events, enabled, title, count):
    on, _ = _render(monkeypatch, plot, events)
    assert len(on[0].axes) == 15
    disabled = [key for key in DRAWABLE if key != enabled]
    off, _ = _render(monkeypatch, plot, events, disabled)
    fig = off[0]
    assert len(fig.axes) == count
    assert any(title in text for text in _texts(fig))
    covered = set()
    for ax in fig.axes:
        spec = ax.get_subplotspec()
        covered.update(spec.rowspan)
    assert covered == set(range(fig.axes[0].get_subplotspec().get_gridspec().nrows))


@requires_matplotlib
def test_direct_burn_renderer_filters_supplied_hits(monkeypatch, plot, events):
    from matplotlib import pyplot as plt  # pylint: disable=import-outside-toplevel
    renderer = sys.modules['ccusage_plot_render']
    sessions = renderer.build_sessions(events)
    hit = [{'ts': events[1]['timestamp'], 'text': 'limited'}]
    figures = []
    try:
        for show_rates in (True, False):
            fig, ax = plt.subplots()
            figures.append(fig)
            renderer.plot_burn_rate(ax, events, sessions, [], hit,
                                    show_rate_limits=show_rates)
        assert _rate_marks(figures[0])
        assert 'Rate limit hit' in _texts(figures[0])
        assert not _rate_marks(figures[1])
        assert not any('rate limit' in text.lower() for text in _texts(figures[1]))
        assert figures[1].axes[0].collections
    finally:
        for fig in figures:
            plt.close(fig)


@requires_matplotlib
def test_surviving_panel_serializes_real_png(plot, events, tmp_path):
    path = tmp_path / 'usage.png'
    plot.plot_timeline(events, '1h', str(path),
                       disabled_features=frozenset({'usage_timeseries',
                                                    'model_comparison', 'burn_rate'}))
    assert path.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    assert path.stat().st_size > 1000


@requires_matplotlib
def test_burn_only_header_does_not_overlap_panel_title(monkeypatch, plot, events):
    from matplotlib.backends.backend_agg import FigureCanvasAgg  # pylint: disable=import-outside-toplevel
    for disabled in ((), ('usage_timeseries', 'model_comparison', 'token_breakdown')):
        figures, _ = _render(monkeypatch, plot, events, disabled)
        fig = figures[0]
        canvas = FigureCanvasAgg(fig)
        canvas.draw()
        renderer = canvas.get_renderer()
        burn = next(ax for ax in fig.axes if ax.get_title().startswith('Session Burn Rate'))
        subtitle = next(text for text in fig.texts if 'API calls' in text.get_text())
        assert burn.title.get_window_extent(renderer).y1 < subtitle.get_window_extent(renderer).y0
