"""Pure halves of the rendered project-picker acceptance rules."""
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
        text=True, timeout=60, check=False, encoding="utf-8", errors="strict",
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
