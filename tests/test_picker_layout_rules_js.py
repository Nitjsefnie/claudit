from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "ci" / "panel_layout_rules.mjs"
PROJECTS_FIXTURE = ROOT / "fixtures" / "layout" / "projects.json"


def _node(body: str):
    url = MODULE.as_uri()
    script = f"const mod = await import({json.dumps(url)});\n{body}\n"
    proc = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True,
        text=True, timeout=60, check=False, encoding="utf-8", errors="strict", cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def test_picker_layout_fixture_has_wider_later_projects_and_long_id():
    projects = json.loads(
        PROJECTS_FIXTURE.read_text(encoding="utf-8"))["projects"]
    assert len(projects) >= 12
    first_width = max(len(p["display_name"]) for p in projects[:4])
    assert all(len(p["display_name"]) > first_width for p in projects[4:])
    assert len(projects[-1]["project_id"]) >= 60


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_picker_walk_checks_identity_completion_and_nonempty_pages():
    out = _node("""
      const check = mod.pickerWalkViolations;
      if (!check) { console.log('null'); }
      else {
        const p = (page, total, ids) => ({ page, total, ids,
          previousDisabled: page === 1, nextDisabled: page === total });
        const f = [p(1, 3, ['p1']), p(2, 3, ['p2']), p(3, 3, ['p3'])];
        const b = [...f].reverse();
        const kinds = (forward, backward, ids) =>
          check(forward, backward, ids).map(v => v.kind);
        const ids = ['p1', 'p2', 'p3'];
        const skippedNext = kinds([p(1, 3, ['p1']), p(3, 3, ['p2', 'p3'])], b, ids);
        const skippedPrev = kinds(f, [p(3, 3, ['p3']), p(1, 3, ['p1', 'p2'])], ids);
        const emptyLast = kinds([p(1, 2, ['p1', 'p2']), p(2, 2, [])],
          [p(2, 2, []), p(1, 2, ['p1', 'p2'])], ids);
        const duplicate = kinds([p(1, 2, ['p1', 'p1']), p(2, 2, ['p3'])],
          [p(2, 2, ['p3']), p(1, 2, ['p1', 'p1'])], ids);
        const stuckNext = kinds(f.map((page, i) => i === 2
          ? {...page, nextDisabled: false} : page), b, ids);
        const stuckPrevious = kinds(f, b.map((page, i) => i === 2
          ? {...page, previousDisabled: false} : page), ids);
        const single = kinds([p(1, 1, ['only'])], [p(1, 1, ['only'])], ['only']);
        const empty = kinds([p(1, 1, [])], [p(1, 1, [])], []);
        const manyIds = Array.from({length: 101}, (_, i) => `id-${i}`);
        const manyForward = manyIds.map((id, i) => p(i + 1, 101, [id]));
        const manyBackward = [...manyForward].reverse();
        console.log(JSON.stringify({
          skippedNext, skippedPrev, emptyLast, duplicate,
          stuckNext, stuckPrevious, single, empty,
          many: kinds(manyForward, manyBackward, manyIds),
        }));
      }
    """)
    assert out is not None, "pickerWalkViolations is missing"
    assert "sequence" in out["skippedNext"]
    assert "sequence" in out["skippedPrev"]
    assert "empty-page" in out["emptyLast"]
    assert "project-order" in out["duplicate"]
    assert "completion" in out["stuckNext"]
    assert "completion" in out["stuckPrevious"]
    assert out["single"] == []
    assert out["empty"] == []
    assert out["many"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_picker_fit_accepts_single_page_without_a_pager():
    out = _node("""
      const check = mod.pickerPageFitFindings;
      if (!check) { console.log('null'); }
      else console.log(JSON.stringify(check([{
        page: 1, total: 1, left: 0, right: 120, overflow: false,
        chipRight: 100, pagerLeft: null, pagerRight: null,
        pagerWidth: null, pagerVisible: false,
      }])));
    """)
    assert out is not None, "pickerPageFitFindings is missing"
    assert out == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_picker_page_geometry_reads_height_position_and_seat():
    out = _node("""
      const check = mod.pickerPageGeometryViolations;
      if (!check) { console.log('null'); }
      else {
        const first = { top: 52, height: 47, seat: 6, below: true };
        console.log(JSON.stringify({
          stable: check([first, {...first}]).map(v => v.kind),
          height: check([first, {...first, height: 90}]).map(v => v.kind),
          position: check([first, {...first, top: 72, seat: 6}]).map(v => v.kind),
          seat: check([first, {...first, top: 52, seat: 26}]).map(v => v.kind),
        }));
      }
    """)
    assert out is not None, "pickerPageGeometryViolations is missing"
    assert out["stable"] == []
    assert "height" in out["height"]
    assert "position" in out["position"]
    assert "seat" in out["seat"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_expected_projects_come_from_the_served_payload():
    out = _node("""
      if (!mod.pickerProjectIds) console.log('null');
      else {
        let requested;
        global.fetch = async (url) => {
          requested = url;
          return {ok: true, json: async () => ({projects: [
            {project_id: 'first'}, {project_id: 'last'}]})};
        };
        const ids = await mod.pickerProjectIds({evaluate: fn => fn()});
        console.log(JSON.stringify({ids, requested}));
      }
    """)
    assert out is not None, 'independent fixture reader is missing'
    assert out['ids'] == ['first', 'last']
    assert out['requested'].startswith('/api/projects?')


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_selection_checks_fitted_pages_including_healthy_single_page():
    out = _node("""
      const check = mod.pickerSelectionFindings;
      if (!check) console.log('null');
      else {
        const one = {page: 1, total: 1, ids: ['a', 'b'], activeIds: ['b'],
          jumpVisible: false, jumpWidth: null, pagerVisible: false,
          pagerWidth: null, left: 0, right: 1440, chipRight: 140,
          overflow: false};
        const off = {...one, total: 2, ids: ['a'], activeIds: [],
          jumpVisible: true, jumpWidth: 30, pagerVisible: true,
          pagerWidth: 140, pagerLeft: 200, pagerRight: 340};
        const on = {...off, page: 2, ids: ['b'], activeIds: ['b'],
          jumpVisible: false};
        console.log(JSON.stringify({
          one: check([one], 'b'), many: check([off, on], 'b'),
          lostSelection: check([{...one, activeIds: []}], 'b'),
          missingJump: check([{...off, jumpVisible: false}, on], 'b'),
          changedReserve: check([off, {...on, jumpWidth: 10}], 'b'),
        }));
      }
    """)
    assert out is not None, 'selection classifier is missing'
    assert out['one'] == []
    assert out['many'] == []
    assert out['lostSelection']
    assert out['missingJump']
    assert out['changedReserve']


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize('rule', ['project-picker-pages-fit', 'unknown-rule'])
def test_cases_only_rejects_rule_filter_before_running(rule):
    proc = subprocess.run(
        ["node", str(MODULE), '--picker-cases', '--rule', rule],
        capture_output=True, text=True, check=False, timeout=30)
    assert proc.returncode != 0, proc.stdout
    assert '--picker-cases cannot be combined with --rule' in proc.stderr
    assert 'PASS:' not in proc.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_layout_runner_never_accepts_zero_checks():
    out = _node("""
      const summarize = mod.runSummary;
      console.log(JSON.stringify(summarize ? [
        summarize({executed: 0, failures: [], fired: []}, false),
        summarize({executed: 13, failures: [], fired: []}, true),
      ] : null));
    """)
    assert out is not None, 'checked-run summary is missing'
    assert out[0]['ok'] is False
    assert 'no checks executed' in out[0]['message']
    assert out[1]['ok'] is True
    assert 'fixture' in out[1]['message']
    assert 'seed' not in out[1]['message']


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_layout_readiness_waits_for_api_and_render_turns():
    helper = (MODULE.parent / 'layout_waits.mjs').as_uri()
    out = _node(f"""
      const waits = await import({json.dumps(helper)});
      const handlers = {{}};
      let frames = 0, done = false;
      const page = {{on: (name, fn) => {{handlers[name] = fn;}},
        waitForFunction: async () => {{}},
        evaluate: async () => {{ frames++; }}}};
      waits.trackLayoutRequests(page);
      const request = {{url: () => 'http://fixture/api/projects'}};
      const stream = {{url: () => 'http://fixture/api/events'}};
      handlers.request(request); handlers.request(stream);
      const settling = waits.settleDashboard(page).then(() => {{done = true;}});
      await Promise.resolve(); await Promise.resolve();
      const premature = done;
      handlers.requestfinished(request);
      await settling;
      console.log(JSON.stringify({{premature, done, frames}}));
    """)
    assert out['premature'] is False
    assert out['done'] is True
    assert out['frames'] >= 2


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_full_picker_walk_uses_one_browser_round_trip():
    out = _node("""
      if (!mod.walkPickerPages) console.log('null');
      else {
        const calls = [];
        const states = Array.from({length: 101}, (_, i) => ({page: i + 1}));
        const page = {evaluate: async (probe, args) => {
          calls.push(args); return {states, completed: true, missing: false};
        }};
        const result = await mod.walkPickerPages(page, 101, 'forward');
        console.log(JSON.stringify({calls, pages: result.states.length}));
      }
    """)
    assert out is not None, 'batched browser walk is missing'
    assert len(out['calls']) == 1
    assert out['calls'][0]['maxPages'] == 101
    assert out['calls'][0]['direction'] == 'forward'
    assert out['pages'] == 101


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_batched_widths_reject_missing_or_reordered_samples():
    helper = (MODULE.parent / 'picker_cases.mjs').as_uri()
    out = _node(f"""
      const cases = await import({json.dumps(helper)});
      if (!cases.readPickerWidths) console.log('null');
      else {{
        const results = [];
        for (const samples of [[], [{{width: 2}}, {{width: 1}}],
          [{{width: 1}}, {{width: 2}}]]) {{
          try {{
            await cases.readPickerWidths({{evaluate: async () => samples}},
              () => {{}}, '.picker', [1, 2]);
            results.push('ok');
          }} catch (error) {{ results.push(error.message); }}
        }}
        console.log(JSON.stringify(results));
      }}
    """)
    assert out is not None, 'checked width batch is missing'
    assert out[:2] == ['width sweep sample sequence differs'] * 2
    assert out[2] == 'ok'


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_selected_audit_reuses_the_first_backward_walk():
    out = _node("""
      if (!mod.runPickerWalk) console.log('null');
      else {
        const directions = [];
        const states = [1, 2].map(page => ({page, total: 2, ids: [`p${page}`],
          previousDisabled: page === 1, nextDisabled: page === 2}));
        const page = {waitForSelector: async () => {},
          evaluate: async (fn, args) => {
            if (!args || typeof args === 'string') return ['p1', 'p2'];
            directions.push(args.direction);
            return {states: args.direction === 'forward' ? states : [...states].reverse(),
              completed: true, missing: false};
          }};
        const result = await mod.runPickerWalk(page, true);
        console.log(JSON.stringify({directions, facts: result.facts,
          forward: result.forward.states.map(s => s.page),
          backward: result.backward.states.map(s => s.page)}));
      }
    """)
    assert out is not None, 'backward-first audit is missing'
    assert out['directions'] == ['backward', 'forward']
    assert out['forward'] == [1, 2]
    assert out['backward'] == [2, 1]
    assert out['facts'] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_width_sweep_resets_at_its_own_viewport_before_sampling():
    helper = (MODULE.parent / 'picker_cases.mjs').as_uri()
    out = _node(f"""
      const cases = await import({json.dumps(helper)});
      if (!cases.resetPickerForSweep) console.log('null');
      else {{
        const calls = [], outcomes = [];
        let reset;
        const page = {{setViewportSize: async (size) => calls.push(size.width),
          evaluate: async (fn, args) => {{
            calls.push(args?.direction || 'render');
            return reset;
          }}}};
        for (reset of [{{completed: true, states: [{{page: 1}}]}},
          {{completed: false, states: [{{page: 1}}]}},
          {{completed: true, states: [{{page: 2}}]}}]) {{
          try {{
            await cases.resetPickerForSweep(page, () => {{}}, '.picker', 101);
            outcomes.push('ok');
          }} catch (error) {{ outcomes.push(error.message); }}
        }}
        console.log(JSON.stringify({{calls, outcomes}}));
      }}
    """)
    assert out is not None, 'sweep-owned reset is missing'
    assert out['calls'] == [1440, 'render', 'backward'] * 3
    assert out['outcomes'] == ['ok'] + [
        'picker did not reset before width sweep'] * 2


def _wait_node(body: str):
    """Run the real wait callbacks with a clock advanced by the control."""
    return _node("""
      const waits = await import('./scripts/ci/layout_waits.mjs');
      const {pickerProbe} = await import('./scripts/ci/picker_probe.mjs');
      const cases = await import('./scripts/ci/picker_cases.mjs');
      globalThis.fetch = async () => new Response('fixture script');
      let now = 0, serial = 0;
      const timers = new Map();
      globalThis.setTimeout = (fn, ms) => {
        const id = ++serial; timers.set(id, {fn, at: now + ms}); return id;
      };
      globalThis.clearTimeout = id => timers.delete(id);
      Object.defineProperty(globalThis, 'performance', {value: {now: () => now}});
      const flush = async () => {
        for (let i = 0; i < 100; i++) await Promise.resolve();
      };
      const advance = async ms => {
        now += ms;
        for (const [id, timer] of [...timers]) {
          if (timer.at <= now && timers.delete(id)) timer.fn();
        }
        await flush();
      };
      const never = new Promise(() => {});
      const observe = promise => {
        const result = {status: 'pending'};
        promise.then(value => Object.assign(result, {status: 'fulfilled', value}),
          error => Object.assign(result, {status: 'rejected', error: error.message}));
        return result;
      };
    """ + body)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_layout_timing_prefixed_switch_preserves_results_and_errors():
    out = _node("""
      const {layoutTiming} = await import('./scripts/ci/layout_waits.mjs');
      const log = console.log, entries = [];
      console.log = (kind, payload) => entries.push([kind, JSON.parse(payload)]);
      const run = () => 42;
      delete process.env.CLAUDIT_DEBUG_LAYOUT_TIMING;
      const off = await layoutTiming('off', run), quiet = entries.length;
      process.env.CLAUDIT_DEBUG_LAYOUT_TIMING = '1';
      const on = await layoutTiming('on', run);
      const failure = new Error('fixture failure');
      let preserved = false;
      try { await layoutTiming('error', async () => {throw failure;}); }
      catch (error) { preserved = error === failure; }
      log(JSON.stringify({off, on, quiet, preserved, entries}));
    """)
    assert out['off'] == out['on'] == 42
    assert out['quiet'] == 0
    assert out['preserved'] is True
    assert [entry[0] for entry in out['entries']] == ['LAYOUT_TIMING'] * 2
    assert [entry[1]['label'] for entry in out['entries']] == ['on', 'error']
    assert all(entry[1]['ms'] >= 0 for entry in out['entries'])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize('blocked,stage', [
    ('panel', 'panel'), ('requests', 'requests'), ('evaluate', 'frames'),
    ('frame-1', 'frames'), ('frame-2', 'frames'), ('fonts', 'fonts'),
    ('idle', 'idle'), ('frame-3', 'frames'), ('frame-4', 'frames'),
])
def test_readiness_deadline_covers_every_stage(blocked, stage):
    out = _wait_node("const blocked = " + json.dumps(blocked) + ";" + """
      const handlers = {};
      let frame = 0;
      globalThis.requestAnimationFrame = fn => {
        if (blocked !== `frame-${++frame}`) queueMicrotask(fn);
      };
      globalThis.requestIdleCallback = fn => {
        if (blocked !== 'idle') queueMicrotask(fn);
      };
      globalThis.document = {fonts: {
        ready: blocked === 'fonts' ? never : Promise.resolve()}};
      const page = {on: (name, fn) => {handlers[name] = fn;},
        waitForFunction: () => blocked === 'panel' ? never : Promise.resolve(),
        evaluate: fn => blocked === 'evaluate' ? never : fn()};
      waits.trackLayoutRequests(page);
      if (blocked === 'requests') handlers.request({url: () => 'http://test/api/projects'});
      const result = observe(waits.settleDashboard(page, 10));
      await flush(); await advance(9);
      const before = result.status;
      await advance(1);
      console.log(JSON.stringify({before, result, timers: timers.size}));
    """)
    assert out['before'] == 'pending'
    assert out['result']['status'] == 'rejected', out
    assert 'dashboard' in out['result']['error']
    assert stage in out['result']['error']
    assert out['timers'] == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_readiness_uses_one_budget_across_stages_and_stops_after_timeout():
    out = _wait_node("""
      let release, releaseEvaluation, evaluations = 0;
      const page = {on: () => {},
        waitForFunction: () => new Promise(resolve => {release = resolve;}),
        evaluate: () => {
          evaluations++;
          return new Promise(resolve => {releaseEvaluation = resolve;});
        }};
      waits.trackLayoutRequests(page);
      const result = observe(waits.settleDashboard(page, 10));
      await flush(); await advance(6); release(); await flush();
      await advance(3);
      const before = result.status;
      await advance(1);
      const atDeadline = {...result};
      releaseEvaluation(); await flush();
      console.log(JSON.stringify({before, result: atDeadline, evaluations, timers: timers.size}));
    """)
    assert out['before'] == 'pending'
    assert out['result']['status'] == 'rejected', out
    assert 'frames' in out['result']['error']
    assert out['evaluations'] == 1
    assert out['timers'] == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize('mode', ['render', 'sweep-reset', 'project-fetch', 'width-batch', 'page-walk'])
def test_stalled_browser_evaluation_has_a_deadline(mode):
    out = _wait_node("const mode = " + json.dumps(mode) + ";" + """
      const page = {evaluate: () => never, setViewportSize: async () => {}};
      const task = mode === 'render' ? waits.renderTurn(page, 10)
        : mode === 'project-fetch' ? mod.pickerProjectIds(page, 10)
        : mode === 'width-batch' ? cases.readPickerWidths(page, () => {}, '.picker', [320], 10)
        : mode === 'page-walk' ? mod.walkPickerPages(page, 2, 'forward', 10)
        : cases.resetPickerForSweep(page, () => {}, '.picker', 2, 10);
      const result = observe(task);
      await flush(); await advance(10);
      console.log(JSON.stringify({result, timers: timers.size}));
    """)
    assert out['result']['status'] == 'rejected', out
    label = {'project-fetch': 'project', 'width-batch': 'width', 'page-walk': 'forward'}.get(mode, 'frames')
    assert label in out['result']['error']
    assert out['timers'] == 0


def _probe_wait_node(body: str):
    return _wait_node("""
      let current = 1, disconnects = 0, frames = 0;
      const callbacks = new Map();
      globalThis.cancelAnimationFrame = id => callbacks.delete(id);
      globalThis.MutationObserver = class {
        observe() {}
        disconnect() { disconnects++; }
      };
      globalThis.getComputedStyle = () => ({display: 'flex', visibility: 'visible'});
      const box = {left: 0, right: 320, top: 0, bottom: 47, width: 320, height: 47};
      const pager = {getBoundingClientRect: () => box, querySelector: () => null};
      const strip = {style: {}, children: [], scrollWidth: 320, clientWidth: 320,
        getBoundingClientRect: () => box,
        querySelector: sel => sel === '.pp-pager' ? pager
          : sel === '.pp-count' ? {textContent: `${current} / 2`}
          : sel.includes('Next') ? {disabled: current === 2, click: () => {current++;}}
          : {disabled: current === 1, click: () => {current--;}}};
      globalThis.document = {querySelector: () => strip};
    """ + body)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize('direction,blocked', [
    ('widths', 1), ('widths', 2), ('widths', 3), ('widths', 4),
    ('forward', 1), ('backward', 1),
])
def test_picker_probe_frame_deadlines_name_the_width_or_page(direction, blocked):
    out = _probe_wait_node(
        f"const direction = {json.dumps(direction)}, blocked = {blocked};" + """
      if (direction === 'backward') current = 2;
      globalThis.requestAnimationFrame = fn => {
        const id = ++frames;
        if (id === blocked) callbacks.set(id, fn); else queueMicrotask(fn);
        return id;
      };
      const args = {selector: '.picker', timeoutMs: 10, maxPages: 2, direction};
      if (direction === 'widths') args.widths = [320, 321];
      const result = observe(pickerProbe(args));
      await flush(); await advance(9);
      const before = result.status;
      await advance(1);
      console.log(JSON.stringify({before, result, timers: timers.size,
        callbacks: callbacks.size, disconnects}));
    """)
    assert out['before'] == 'pending'
    assert out['result']['status'] == 'rejected', out
    assert 'frames' in out['result']['error']
    label = str(320 if blocked <= 2 else 321) if direction == 'widths' else direction
    assert label in out['result']['error']
    assert out['timers'] == 0
    assert out['callbacks'] == 0
    assert out['disconnects'] == (0 if direction == 'widths' else 1)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize('outcome', ['healthy', 'stuck-count', 'click-error'])
def test_picker_probe_cleans_up_navigation_timers_and_observers(outcome):
    out = _probe_wait_node("const outcome = " + json.dumps(outcome) + ";" + """
      globalThis.requestAnimationFrame = fn => {queueMicrotask(fn); return ++frames;};
      if (outcome !== 'healthy') {
        const query = strip.querySelector;
        strip.querySelector = sel => !sel.includes('Next') ? query(sel)
          : {disabled: false, click: () => {
            if (outcome === 'click-error') throw new Error('click failed');
          }};
      }
      const result = observe(pickerProbe({selector: '.picker', timeoutMs: 10,
        maxPages: 2, direction: 'forward'}));
      await flush();
      const before = timers.size;
      await advance(10);
      console.log(JSON.stringify({result, before, timers: timers.size, disconnects}));
    """)
    if outcome == 'healthy':
        assert out['result']['status'] == 'fulfilled', out
        assert [state['page'] for state in out['result']['value']['states']] == [1, 2]
        assert out['before'] == 0
    else:
        assert out['result']['status'] == 'rejected', out
        assert ('advance' if outcome == 'stuck-count' else 'click failed') in out['result']['error']
    assert out['timers'] == 0
    assert out['disconnects'] == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_healthy_readiness_and_width_frames_clear_all_deadline_timers():
    out = _probe_wait_node("""
      globalThis.requestAnimationFrame = fn => {queueMicrotask(fn); return ++frames;};
      globalThis.requestIdleCallback = fn => queueMicrotask(fn);
      document.fonts = {ready: Promise.resolve()};
      const page = {on: () => {}, waitForFunction: async () => {}, evaluate: fn => fn()};
      waits.trackLayoutRequests(page);
      await waits.settleDashboard(page, 10);
      await waits.renderTurn(page, 10);
      const samples = await pickerProbe({selector: '.picker', widths: [320, 321], timeoutMs: 10});
      console.log(JSON.stringify({widths: samples.map(s => s.width), timers: timers.size}));
    """)
    assert out['widths'] == [320, 321]
    assert out['timers'] == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize('blocked,label', [
    ('measurement', 'measurement'), ('selection', 'selected-chip'),
    ('width', 'width setter'), ('narrow', 'narrow viewport'),
    ('restore', 'restore viewport'), ('context', 'fixture context'),
    ('routes', 'fixture routes'), ('page', 'fixture page'),
    ('fulfill', 'fixture response'), ('close', 'fixture close'),
    *[(name, name) for name in [
        'project-picker-pages-fit', 'project-picker-pages-complete',
        'project-picker-pages-left-fit',
        'project-picker-project-universe', 'project-picker-pages-height',
        'project-picker-pages-position', 'project-picker-pages-seat',
        'project-picker-selected-fit',
    ]],
])
def test_introduced_protocol_calls_reject_a_stalled_renderer(blocked, label):
    out = _wait_node("const blocked = " + json.dumps(blocked) + ";" + """
      let routeHandler, entered, viewports = 0;
      const reached = new Promise(resolve => {entered = resolve;});
      const stall = () => {entered(); return never;};
      const state = {page: 1, total: 1, ids: ['p1'], activeIds: [],
        previousDisabled: true, nextDisabled: true, left: 0, right: 320,
        chipLeft: 40, chipRight: 100, pagerVisible: false, pagerWidth: null,
        height: 47, top: 0, seat: 0};
      const walk = {states: [state], completed: true, missing: false};
      const page = {on() {}, waitForSelector: async () => {},
        waitForFunction: async () => {}, click: async () => {},
        evaluate: (fn, args) => blocked.startsWith('project-picker-')
          || (blocked === 'measurement' && typeof args === 'string') ? stall()
          : args?.direction ? Promise.resolve(walk) : Promise.resolve(['p1']),
        $$eval: stall, $eval: stall,
        setViewportSize: async () => {
          viewports++;
          if ((blocked === 'narrow' && viewports === 1)
            || (blocked === 'restore' && viewports === 2)) await stall();
        },
        goto: async () => {
          if (blocked === 'fulfill') await routeHandler({
            request: () => ({url: () => 'http://test/api/projects'}),
            fulfill: stall});
        }};
      const context = {
        route: async (pattern, handler) => {
          if (pattern !== '**/api/**') return;
          routeHandler = handler; if (blocked === 'routes') await stall();
        },
        newPage: async () => blocked === 'page' ? stall() : page,
        close: async () => {if (blocked === 'close') await stall();},
      };
      const browser = {newContext: async () => blocked === 'context' ? stall() : context};
      const rule = id => mod.RULES.find(r => r.id === id);
      const task = blocked.startsWith('project-picker-') ? rule(blocked).seed(page)
        : blocked === 'measurement' ? mod.runPickerWalk(page)
        : blocked === 'selection' ? rule('project-picker-selected-fit').run({page, width: 320, restorePicker: false})
        : ['narrow', 'restore'].includes(blocked) ? rule('project-picker-pages-fit').run({page, width: 320})
        : cases.runPickerCases(browser, 'http://test', [
          {id: 'project-picker-pages-fit', run: async () => []},
          {id: 'project-picker-selected-fit', run: async () => []},
        ], {probe: () => {}, fit: () => [], selection: () => []});
      const result = observe(task);
      await reached; await flush();
      const before = result.status;
      await advance(61_000);
      console.log(JSON.stringify({before, result, timers: timers.size}));
    """)
    assert out['before'] == 'pending'
    assert out['result']['status'] == 'rejected', out
    assert label in out['result']['error']
    assert out['timers'] == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_picker_probe_and_fit_check_both_edges_of_every_chip_and_pager():
    out = _probe_wait_node("""
      const results = {};
      for (const [name, left, right, pagerLeft, pagerRight] of [
        ['inside', 40, 100, 200, 320], ['chip-left', -2, 100, 200, 320],
        ['chip-right', 40, 322, 200, 320], ['tolerance', -1, 321, -1, 321],
        ['pager-left', 40, 100, -2, 320], ['pager-right', 40, 100, 200, 322],
        ['empty', null, null, null, null],
      ]) {
        strip.children = left === null ? [] : [[40, 50], [left, right]].map(([l, r], i) => ({
          classList: {contains: cls => cls === 'pp-proj'}, dataset: {projectId: `p${i}`},
          getBoundingClientRect: () => ({left: l, right: r, width: r - l, height: 20}),
        }));
        pager.getBoundingClientRect = () => ({left: pagerLeft, right: pagerRight,
          width: pagerRight - pagerLeft, height: 20});
        if (name === 'empty') strip.querySelector = () => null;
        const observed = pickerProbe('.picker');
        results[name] = mod.pickerPageFitFindings([observed]);
      }
      console.log(JSON.stringify(results));
    """)
    for case in ['inside', 'tolerance', 'empty']:
        assert out[case] == [], (case, out[case])
    for case in ['chip-left', 'chip-right', 'pager-left', 'pager-right']:
        assert len(out[case]) == 1, (case, out[case])
        assert out[case][0]['gap'] >= 0
