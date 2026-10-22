from __future__ import annotations

import json
from pathlib import Path

from backend.flag_registry import FEATURE_SPECS
from tests.node_run import run_node

ROOT = Path(__file__).resolve().parents[1]


def _node(script: str, window: str = '{}') -> None:
    prelude = ("const assert = require('node:assert/strict');"
               f"global.window = {window};require('./src/feature-flags.js');")
    result = run_node(["node", "-e", prelude + script], cwd=ROOT,
                      capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_sparse_server_payload_preserves_surviving_features() -> None:
    script = """
require('./src/dashboard-shape.js');
require('./src/dashboard-fetch.js');
const map = window.backendDashToShape;
const rich = {
 hourly: [{hour:'2026-01-01T00:00:00Z', model:'sample', input_tokens:7,
           output_tokens:11, cost_usd:3}], bucket_s:3600,
 sessions: [{session_id:'real-session', start_ts:1767225600, requests:4,
             cost_usd:3, input_tokens:7, output_tokens:11}],
 response_sizes:[{ts:1767225600, model:'sample', n:4, p50:11, p90:19}],
 cost_by_model_provider:[
  {model:'sample', provider:'Host', cost_usd:3, total_tokens:18},
  {model:'sample', provider:'Host', cost_usd:2, total_tokens:7},
  {model:'sample', cost_usd:1, total_tokens:11}],
 rate_limit_hits:[{ts:'2026-01-01T00:00:00Z', content:'hit'}]
};
const on = map(rich);
assert.equal(on.events[0].input_tokens, 7);
assert.equal(on.sessionsOverride[0].session_id, 'real-session');
assert.equal(on.responseSizes[0].p50, 11);
assert.equal(on.limitHits.length, 1);
assert.deepEqual(on.costByModel, {'sample · Host':5, sample:1});
assert.deepEqual(on.tokensByModel, {'sample · Host':25, sample:11});
const legacy = map({...rich, cost_by_model_provider:null, cost_by_model:[{model:'other', cost_usd:4}]});
assert.deepEqual(legacy.costByModel, {other:4});
assert.deepEqual(legacy.tokensByModel, {});
const hourlyConsumers = ['usage_timeseries','thinking_output','line_churn','model_comparison',
  'token_breakdown','cache_ttl','burn_rate','summary'];
window.FEATURES = Object.fromEntries(hourlyConsumers.map(key => [key, false]));
const sparse = {...rich}; delete sparse.hourly; delete sparse.token_types;
const off = map(sparse);
assert.ok(off, 'sparse payload lost surviving features');
assert.deepEqual(off.events, []);
assert.equal(off.sessionsOverride[0].session_id, 'real-session');
assert.equal(off.responseSizes[0].p50, 11);
assert.ok(Number.isFinite(off.range.start) && Number.isFinite(off.range.end));
window.FEATURES = {rate_limits:false};
assert.deepEqual(map(rich).limitHits, []);
assert.equal(map(rich).hideRateLimits, true);
const partial = map({...rich, hourly:[{hour:'2026-01-01T00:00:00Z', model:'sample', thinking_tokens:23}]});
assert.equal(partial.events[0].thinking_tokens, 23);
for (const key of ['input_tokens','output_tokens','cache_create','cache_read','cost_usd']) {
  assert.ok(Number.isFinite(partial.events[0][key]), key);
}
const tx = {events:[], meta:[
  {type:'assistant_usage', ts:'2026-01-01T00:00:00Z', sessionId:'real-session',
   line:1, model:'sample', usage:{input_tokens:7, output_tokens:11}},
  {type:'rate_limit', ts:'2026-01-01T00:00:00Z', content:'hit'},
]};
window.resolveModelRate = () => ({rates:{fresh:0, out:0, c5:0, c1h:0, read:0}});
window.usageCtxInput = usage => usage.input_tokens;
window.FEATURES = {};
assert.equal(window.txToDashData(tx).limitHits.length, 1);
window.FEATURES = {rate_limits:false};
assert.deepEqual(window.txToDashData(tx).limitHits, []);
assert.equal(tx.meta.length, 2);
window.FEATURES = {usage_timeseries:false, tool_usage:true};
assert.equal(window.featureOn('tool_usage'), true);
const calls = [];
const fetchImpl = async url => {
  calls.push(url);
  return url.startsWith('/api/dashboard')
    ? {ok:false, status:404, json:async () => ({detail:'feature disabled'})}
    : {ok:true, json:async () => ({buckets:[{ts:1767225600, tool:'Read', n:4}]})};
};
(async () => {
  const range = '7d';
  let state = window.dashboardFetch.start();
  try { await window.dashboardFetch.load(`/api/dashboard?range=${range}`, {}, fetchImpl); }
  catch (error) { state = window.dashboardFetch.failed(error.message); }
  assert.equal(state.status, window.dashboardFetch.ERROR);
  const tools = await window.dashboardFetch.load(`/api/tool-usage?range=${range}`, {}, fetchImpl);
  assert.equal(tools.buckets[0].n, 4);
  assert.equal(off.sessionsOverride[0].session_id, on.sessionsOverride[0].session_id);
  assert.deepEqual(calls, ['/api/dashboard?range=7d', '/api/tool-usage?range=7d']);
})().catch(error => { console.error(error); process.exitCode = 1; });

"""
    _node(script, '{shortModelName: m => m}')


def test_feature_consumers_and_routes_follow_switches() -> None:
    script = """
require('./src/dashboard-shape.js');
const allOff = ALL_OFF;
for (const [flags, dashboard, models] of [
 [{}, true, true], [allOff, false, false],
 [{...allOff, summary:true}, true, false],
 [{...allOff, tool_usage:true}, false, true],
 [{...allOff, token_breakdown:true}, true, true],
 [{...allOff, rate_limits:true}, false, false],
]) {
 window.FEATURES = flags;
 assert.equal(window.dashboardFeaturesOn(), dashboard);
 assert.equal(window.modelFeaturesOn(), models);
}
for (const [route, guest, backend, flags, expected] of [
 ['sessions', false, true, {}, 'sessions'], ['sessions', true, true, {}, 'dashboard'],
 ['sessions', false, true, {sessions:false}, 'dashboard'],
 ['session', false, true, {}, 'session'], ['session', true, true, {}, 'dashboard'],
 ['session', false, true, {inspector:false}, 'dashboard'],
 ['cache', true, true, {}, 'cache'], ['cache', false, false, {}, 'dashboard'],
 ['cache', false, true, {cache:false}, 'dashboard'],
 ['dashboard', true, false, allOff, 'dashboard'],
]) {
 window.FEATURES = flags;
 assert.equal(window.featureRoute(route, guest, backend), expected);
}
"""
    _node(script.replace('ALL_OFF', json.dumps({s.key: False for s in FEATURE_SPECS})))


def test_browser_scenario_inventory_matches_registry() -> None:
    result = run_node([
        "node", "--input-type=module", "-e",
        "import {FEATURE_KEYS} from './scripts/ci/feature_surfaces.mjs';"
        "console.log(JSON.stringify(FEATURE_KEYS));",
    ], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert sorted(json.loads(result.stdout)) == sorted(s.key for s in FEATURE_SPECS)


def test_performance_reference_policy_keeps_operational_dimensions() -> None:
    script = """
require('./src/perf-feature-policy.js');
const samples = [
 ['metric','inspector_open','inspector'],
 ['region','inspector','inspector'], ['region','panel_context_chart','inspector'],
 ['region','project_picker','project_picker'], ['region','summary_strip','summary'],
 ['phase','sse_update','live_updates'],
 ['region','panel_cost_usd','usage_timeseries'],
 ['region','panel_thinking_output','thinking_output'],
 ['region','panel_cache_create','usage_timeseries'],
];
for (const [dimension, value, feature] of samples) {
 window.FEATURES = {};
 assert.equal(window.perfFeatureVisible(dimension, value), true, value);
 window.FEATURES = {[feature]:false};
 assert.equal(window.perfFeatureVisible(dimension, value), false, value);
}
window.FEATURES = {inspector:false, cache:false, live_updates:false};
assert.equal(window.perfFeatureVisible('metric', 'signin'), true);
assert.equal(window.perfFeatureVisible('region', 'topbar'), true);
assert.equal(window.perfFeatureVisible('region', 'range_picker'), true);
assert.equal(window.perfFeatureVisible('region', 'panel_grid'), true);
assert.equal(window.perfFeatureVisible('region', 'panel_cache_create'), true);
"""
    _node(script)
