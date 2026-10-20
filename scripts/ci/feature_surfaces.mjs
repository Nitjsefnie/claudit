// Real browser ON/OFF controls; rich payloads stay identical under every policy.
import { createSweepContext } from './cdn_route.mjs';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { API, FIXTURES } from './panel_server.mjs';

const GROUPS = {
  usage_timeseries: ['Input Tokens', 'Output Tokens', 'Cache Create', 'Cache Read', 'Total Tokens', 'Cost (USD)'],
  thinking_output: ['Thinking Output'], line_churn: ['Lines Added', 'Lines Deleted'],
  model_comparison: ['Cost by Model', 'Tokens by Model'],
  token_breakdown: ['Token Breakdown — by tokens', 'Token Breakdown — by cost'],
  project_comparison: ['Cost by Project', 'Tokens by Project'],
  cache_ttl: ['Prompt-Cache TTL Split'], response_sizes: ['Response Sizes'],
  context_growth: ['Context Growth — comparison'], burn_rate: ['Session Burn Rate'],
  context_comparison: ['Cost by Context Size', 'Tokens by Context Size'],
  tool_usage: ['Tool Usage Ratio'], reply_latency: ['Reply Latency'],
  page_performance: ['Page performance'], tool_error_rate: ['Tool Error Rate'],
  agent_comparison: ['Cost by Agent Type', 'Tokens by Agent Type'],
  activity_heatmap: ['Activity Heatmap', 'Activity Heatmap — legend'],
};
const ENDPOINTS = {
  context_growth: '/api/context-growth/traces', context_comparison: '/api/cost-by-context',
  tool_usage: '/api/tool-usage', reply_latency: '/api/reply-latency',
  page_performance: '/api/web-metrics', tool_error_rate: '/api/tool-error-rate',
  agent_comparison: '/api/cost-by-agent', activity_heatmap: '/api/activity-heatmap',
  project_picker: '/api/projects', cache: '/api/cache', inspector: '/api/sessions/probe/transcript',
  export_png: '/api/export', live_updates: '/api/events',
};
const SHELL = {
  sessions: '.navbtn:text-is("Sessions")', cache: '.navbtn:text-is("Cache")',
  inspector: '.navbtn:text-is("Inspector")', project_picker: '[data-perf-region="project_picker"]',
  export_png: 'button:text-is("Export PNG")', live_updates: '[aria-live="polite"]',
};
export const FEATURE_KEYS = [...Object.keys(GROUPS), 'summary', 'rate_limits', ...Object.keys(SHELL)];
const panel = title => `[data-panel=${JSON.stringify(title)}]`;

// Exported so an injected broken helper can prove this oracle fails closed.
export async function requireAbsent(page, selectors, label) {
  for (const selector of selectors) {
    assert.equal(await page.locator(selector).count(), 0, `${label}: residual ${selector}`);
  }
}

async function scenarioPage(browser, base, policy, options = {}) {
  const ctx = await createSweepContext(browser, { viewport: { width: 1440, height: 1000 } });
  const requests = [];
  const errors = [];
  if (options.synthetic) {
    await ctx.route(base + '/', async route => {
      const response = await route.fetch();
      const html = (await response.text()).replace("window.BACKEND_URL = '/';", "window.BACKEND_URL = '';");
      await route.fulfill({response, body:html});
    });
  }
  await ctx.addInitScript(({ policy, seed, referenceSeed }) => {
    window.FEATURES = policy;
    window.__eventSources = [];
    window.EventSource = class {
      constructor(url) { window.__eventSources.push(url); }
      addEventListener() {} removeEventListener() {} close() {}
    };
    if (referenceSeed) {
      Object.defineProperty(window, 'perfFeatureVisible', {
        configurable:true, get:() => () => true, set:() => {},
      });
    }
    if (seed) {
      Object.defineProperty(window, 'featureOn', {
        configurable: true, get: () => () => true, set: () => {},
      });
    }
  }, { policy, seed: !!options.seed, referenceSeed: !!options.referenceSeed });
  const cacheRow = { turns: 4, fresh: 7, cache_create: 5, cache_read: 9, output: 11,
    eph5: 2, eph1h: 3, hit_rate_pct: 20, cost_total: 3, cost_buckets: {} };
  await ctx.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    requests.push(path);
    if (path === '/api/dashboard' && options.failedDashboard) {
      return route.fulfill({ status: 404, json: { detail: 'feature disabled' } });
    }
    if (path.endsWith('/transcript')) {
      return route.fulfill({ body: JSON.stringify({type:'user', timestamp:'2026-01-01T00:00:00Z',
        message:{role:'user', content:'browser probe'}}) });
    }
    if (path === '/api/cache') return route.fulfill({ json: {
      per_model: [{...cacheRow, model:'probe'}], session_total: cacheRow,
      top_output: [], top_cache_create: [], top_cache_read: [],
    } });
    if (path === '/api/context-growth/agg') return route.fulfill({json:{
      per_turn:{n:4,mean:7,p50:6,p90:9,p99:11,max:12},
      per_session_final:{n:2,mean:9,p50:8,p90:11,p99:12,max:13},
    }});
    if (path === '/api/export') return route.fulfill({ contentType: 'image/png', body: 'probe' });
    if (!API[path]) return route.fulfill({ json: {} });
    const data = JSON.parse(await readFile(join(FIXTURES, API[path]), 'utf8'));
    if (path === '/api/web-metrics') {
      const titles = [...Object.values(GROUPS).flat(), 'Context Chart'];
      for (const title of titles) data.series.push({metric:'layout_shift', region:
        'panel_' + title.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, ''),
        n:1, total:0.01});
      data.series.push({metric:'layout_shift', region:'project_picker', n:1, total:0.01},
        {metric:'layout_shift', region:'summary_strip', n:1, total:0.01});
    }
    if (path === '/api/dashboard') {
      // A hit inside the data envelope establishes the marks/hover oracle.
      data.rate_limit_hits = [{ ts: data.hourly[Math.floor(data.hourly.length / 2)].hour, content: 'probe hit' }];
      if (options.omitCost) for (const row of data.hourly) delete row.cost_usd;
      if (options.sparse) { delete data.hourly; delete data.token_types; }
    }
    return route.fulfill({ json: data });
  });
  const page = await ctx.newPage();
  page.on('pageerror', err => errors.push(err.message));
  const diagnostics = [];
  page.on('console', msg => { if (msg.type() === 'error') diagnostics.push(msg.text()); });
  page.on('requestfailed', req => diagnostics.push(`${req.url()}: ${req.failure()?.errorText}`));
  await page.goto(base + '/', { waitUntil: 'load' });
  try {
    await page.waitForSelector('.dashboard .page-head');
  } catch (error) {
    const body = await page.locator('body').innerText();
    const globals = await page.evaluate(() => ({React:typeof React, Babel:typeof Babel, App:typeof window.App, Dashboard:typeof window.Dashboard, feature:typeof window.featureOn}));
    await ctx.close();
    throw new Error(`feature bootstrap ${JSON.stringify(policy)}: ${errors.join('; ')}; ${JSON.stringify(globals)}; ${diagnostics.join('; ')}; ${body.slice(0, 300)}`, {cause:error});
  }
  await page.waitForFunction(() => !document.querySelector('.dash-summary')?.textContent.includes('loading'));
  await page.waitForTimeout(500);
  return { ctx, page, requests, errors };
}

async function enterShell(page, key) {
  if (key === 'cache') await page.getByRole('button', { name: 'Cache', exact: true }).click();
  if (key === 'sessions' || key === 'inspector') {
    await page.getByRole('button', { name: 'Sessions', exact: true }).click();
    await page.waitForSelector('.sessions-page');
    if (key === 'inspector') {
      await page.locator('.open-btn').first().click();
      await page.waitForSelector('.session-view');
    }
  }
  if (key === 'export_png') await page.getByRole('button', { name: 'Export PNG', exact: true }).click();
  await page.waitForTimeout(100);
}

function telemetryRegionsFor(key) {
  return GROUPS[key]?.map(t => 'panel_' + t.toLowerCase()
        .replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '')) ||
        ({inspector:['inspector', 'panel_context_chart'], project_picker:['project_picker'],
          summary:['summary_strip'], live_updates:['sse_update']}[key] || []);
}

async function checkTelemetryRegions(on, off, key) {
  const regions = telemetryRegionsFor(key);
  for (const region of regions) {
    const label = region.replace(/_/g, ' ');
    assert.ok(await on.getByText(label, {exact:true}).count(), `${key}: ON telemetry ${label}`);
    assert.equal(await off.getByText(label, {exact:true}).count(), 0, `${key}: residual telemetry ${label}`);
  }
  return regions.length;
}

export async function telemetryRegionScenario(browser, base, key, referenceSeed = false) {
  const on = await scenarioPage(browser, base, {});
  const off = await scenarioPage(browser, base, {[key]:false}, {referenceSeed});
  try { await checkTelemetryRegions(on.page, off.page, key); }
  finally { await on.ctx.close(); await off.ctx.close(); }
}

export async function inspectorGuidanceScenario(browser, base) {
  const on = await scenarioPage(browser, base, {});
  const off = await scenarioPage(browser, base, {sessions:false});
  try {
    for (const target of [on, off]) {
      await target.page.getByRole('button', {name:'Inspector', exact:true}).click();
      await target.page.waitForSelector('.drop-sub');
    }
    assert.match(await on.page.locator('.drop-sub').innerText(), /Pick a session from the Sessions list/);
    assert.doesNotMatch(await off.page.locator('.drop-sub').innerText(), /Sessions list/,
      'disabled Sessions remains in Inspector guidance');
    assert.ok((await off.page.locator('.drop-sub').innerText()).length > 0);
    assert.deepEqual(on.errors, []); assert.deepEqual(off.errors, []);
  } finally { await on.ctx.close(); await off.ctx.close(); }
}

export async function inspectorTelemetryScenario(browser, base) {
  const on = await scenarioPage(browser, base, {});
  const off = await scenarioPage(browser, base, {inspector:false});
  try {
    const row = on.page.locator('[data-journey="inspector_open"]');
    assert.equal(await row.count(), 1);
    await row.hover();
    assert.equal(await on.page.getByText('Inspector open', {exact:true}).count(), 2);
    assert.equal(await off.page.getByText('Inspector open', {exact:true}).count(), 0,
      'disabled Inspector remains in performance journey/tooltip');
    assert.match(await on.page.locator('[data-panel="Page performance"] [role="img"]').getAttribute('aria-label'), /3 of 3 journeys/);
    assert.match(await off.page.locator('[data-panel="Page performance"] [role="img"]').getAttribute('aria-label'), /2 of 2 journeys/);
    assert.deepEqual(on.errors, []); assert.deepEqual(off.errors, []);
  } finally { await on.ctx.close(); await off.ctx.close(); }
}

export async function missingSharedCostScenario(browser, base) {
  const policy = {usage_timeseries:false, model_comparison:false, summary:false};
  const complete = await scenarioPage(browser, base, policy);
  const omitted = await scenarioPage(browser, base, policy, {omitCost:true});
  try {
    assert.equal(await complete.page.locator(panel('Cost by Context Size')).count(), 1);
    assert.equal(await omitted.page.locator(panel('Cost by Context Size')).count(), 1,
      'independent context cost lost when shared cost fields are omitted');
    assert.deepEqual(complete.errors, []); assert.deepEqual(omitted.errors, []);
  } finally { await complete.ctx.close(); await omitted.ctx.close(); }
}

export async function featureSurfaces(browser, base) {
  let scenarios = 0, positives = 0;
  const selectorsFor = key => GROUPS[key]?.map(panel) ||
    (key === 'summary' ? ['.dash-summary'] : key === 'rate_limits'
      ? ['.stat-label:text-is("rate-limit hits")', '[data-role="legend"]:has-text("Rate limit hit")']
      : [SHELL[key]]);
  for (const key of FEATURE_KEYS) {
    console.log(`feature-surfaces: checking ${key}`);
    const on = await scenarioPage(browser, base, {});
    const off = await scenarioPage(browser, base, { [key]: false });
    try {
      for (const selector of selectorsFor(key)) {
        assert.ok(await on.page.locator(selector).count() > 0, `${key}: missing ON ${selector}`);
        positives++;
      }
      const checkedRegions = await checkTelemetryRegions(on.page, off.page, key);
      scenarios += checkedRegions; positives += checkedRegions;
      await enterShell(on.page, key);
      if (key === 'live_updates') {
        assert.equal(await on.page.evaluate(() => window.__eventSources.length), 1); positives++;
        assert.equal(await off.page.evaluate(() => window.__eventSources.length), 0);
      } else if (ENDPOINTS[key]) {
        assert.ok(on.requests.some(p => key === 'inspector' ? p.endsWith('/transcript') : p === ENDPOINTS[key]), `${key}: missing ON request`); positives++;
        assert.ok(!off.requests.some(p => key === 'inspector' ? p.endsWith('/transcript') : p === ENDPOINTS[key]), `${key}: disabled request`);
      }
      await requireAbsent(off.page, selectorsFor(key), key);
      if (key === 'rate_limits') {
        const marks = '[data-panel="Session Burn Rate"] line[stroke="#ff3366"][stroke-opacity="0.7"]';
        assert.equal(await on.page.locator(marks).count(), 1); positives++;
        assert.equal(await off.page.locator(marks).count(), 0);
        const description = on.page.locator('.dash-burn .sr-only');
        assert.match(await description.textContent(), /1 rate-limit line marked/); positives++;
        assert.doesNotMatch(await off.page.locator('.dash-burn .sr-only').textContent(), /rate-limit/);
        await on.page.locator(marks).scrollIntoViewIfNeeded();
        const box = await on.page.locator(marks).boundingBox();
        await on.page.mouse.move(box.x, box.y + 10);
        await on.page.getByText('Rate limit hit', {exact:true}).last().waitFor();
        assert.equal(await on.page.getByText('Rate limit hit', {exact:true}).count(), 2); positives++;
        await off.page.locator('[data-panel="Session Burn Rate"]').scrollIntoViewIfNeeded();
        await off.page.mouse.move(box.x, box.y + 10);
        assert.equal(await off.page.getByText('Rate limit hit', {exact:true}).count(), 0);
      }
      if (key === 'context_growth') {
        assert.ok(await on.page.getByText('Per-Session Context Growth', {exact:true}).count()); positives++;
        await requireAbsent(off.page, ['.dash-context'], key);
      }
      if (key === 'cache') {
        assert.ok(await on.page.locator('.cache-view, .ctx-agg').count() === 2); positives++;
        assert.ok(on.requests.includes('/api/context-growth/agg')); positives++;
        assert.ok(!off.requests.includes('/api/context-growth/agg'));
      }
      if (key === 'line_churn') {
        assert.ok(await on.page.getByText('lines added / deleted', {exact:true}).count()); positives++;
        assert.equal(await off.page.getByText('lines added / deleted', {exact:true}).count(), 0);
      }
      if (key === 'inspector') {
        await on.page.getByRole('button', {name:'Sessions', exact:true}).click();
        assert.equal(await on.page.locator('.srow.shead > div').count(), 8); positives++;
        assert.ok(await on.page.locator('.open-btn').count()); positives++;
        assert.equal(await on.page.locator('.page-foot').count(), 1); positives++;
        await off.page.getByRole('button', {name:'Sessions', exact:true}).click();
        assert.equal(await off.page.locator('.open-btn, .page-foot').count(), 0);
        assert.equal(await off.page.locator('.srow.shead > div').count(), 7);
      }
      assert.deepEqual(on.errors, [], `${key}: ON browser errors`);
      assert.deepEqual(off.errors, [], `${key}: OFF browser errors`);
      scenarios++;
    } finally { await on.ctx.close(); await off.ctx.close(); }
  }
  // Restore stale routes through React's real state queue, without a production test hook.
  const shellKeys = Object.keys(SHELL);
  const shellOn = await scenarioPage(browser, base, {});
  const shellOff = await scenarioPage(browser, base, Object.fromEntries(shellKeys.map(k => [k, false])));
  try {
    for (const key of shellKeys) {
      assert.ok(await shellOn.page.locator(SHELL[key]).count()); positives++;
    }
    await enterShell(shellOn.page, 'cache');
    assert.equal(await shellOn.page.locator('.cache-view, .ctx-agg').count(), 2); positives++;
    await enterShell(shellOn.page, 'sessions');
    assert.ok(await shellOn.page.locator('.open-btn').count()); positives++;
    assert.equal(await shellOn.page.locator('.page-foot').count(), 1); positives++;
    await enterShell(shellOn.page, 'inspector');
    assert.equal(await shellOn.page.locator('.session-view').count(), 1); positives++;
    await enterShell(shellOn.page, 'export_png');
    assert.ok(shellOn.requests.includes('/api/export')); positives++;
    assert.equal(await shellOn.page.evaluate(() => window.__eventSources.length), 1); positives++;
    for (const route of ['sessions', 'cache', 'session']) {
      await shellOff.page.evaluate(route => {
        const root = document.getElementById('root');
        const key = Object.keys(root).find(k => k.startsWith('__reactContainer$'));
        const app = root[key].stateNode.current.child;
        app.memoizedState.queue.dispatch(route);
      }, route);
      await shellOff.page.waitForFunction(() => document.querySelector('.navbtn[aria-current="page"]')?.textContent === 'Overview');
      await requireAbsent(shellOff.page, ['.sessions-page', '.cache-view', '.ctx-agg', '.session-view', '.session-empty', '.open-btn', '.page-foot'], 'tabs_and_shell_disabled');
    }
    await requireAbsent(shellOff.page, Object.values(SHELL), 'tabs_and_shell_disabled');
    assert.ok(!shellOff.requests.some(p => p === '/api/projects' || p === '/api/cache' || p.includes('/api/sessions/')));
    assert.equal(await shellOff.page.evaluate(() => window.__eventSources.length), 0);
    assert.deepEqual(shellOff.errors, []); scenarios++;
  } finally { await shellOn.ctx.close(); await shellOff.ctx.close(); }
  const allOff = await scenarioPage(browser, base, Object.fromEntries(FEATURE_KEYS.map(k => [k, false])));
  try {
    assert.equal(await allOff.page.getByRole('heading', {name:'Overview', exact:true}).count(), 1);
    await requireAbsent(allOff.page, [...Object.values(GROUPS).flat().map(panel), ...Object.values(SHELL), '[data-panel]', '.stat', '.dash-summary'], 'all_off');
    assert.deepEqual(allOff.requests.filter(p => p !== '/api/me' && p !== '/api/metrics'), []);
    assert.deepEqual(allOff.errors, []); scenarios++; positives++;
  } finally { await allOff.ctx.close(); }
  const syntheticOn = await scenarioPage(browser, base, {}, {synthetic:true});
  const syntheticOff = await scenarioPage(browser, base, Object.fromEntries(FEATURE_KEYS.map(k => [k, false])), {synthetic:true});
  try {
    assert.ok(await syntheticOn.page.locator(panel('Input Tokens')).count()); positives++;
    assert.ok(await syntheticOn.page.locator(panel('Session Burn Rate')).count()); positives++;
    assert.equal(await syntheticOff.page.getByRole('heading', {name:'Overview', exact:true}).count(), 1);
    await requireAbsent(syntheticOff.page, [...Object.values(GROUPS).flat().map(panel), ...Object.values(SHELL), '[data-panel]', '.stat', '.dash-summary'], 'synthetic_all_off');
    assert.deepEqual(syntheticOff.requests.filter(p => p !== '/api/metrics'), []);
    assert.deepEqual(syntheticOn.errors, []); assert.deepEqual(syntheticOff.errors, []);
    scenarios += 2;
  } finally { await syntheticOn.ctx.close(); await syntheticOff.ctx.close(); }
  const failed = await scenarioPage(browser, base, {}, {failedDashboard:true});
  const sparsePolicy = Object.fromEntries(['usage_timeseries','thinking_output','line_churn','model_comparison',
    'token_breakdown','cache_ttl','burn_rate','summary'].map(k => [k, false]));
  const sparse = await scenarioPage(browser, base, sparsePolicy, {sparse:true});
  const standalone = await scenarioPage(browser, base, Object.fromEntries(
    FEATURE_KEYS.map(k => [k, k === 'tool_usage'])));
  try {
    for (const target of [failed, sparse, standalone]) {
      assert.ok(await target.page.locator(panel('Tool Usage Ratio')).count()); positives++;
      assert.ok(target.requests.includes('/api/tool-usage'));
      assert.deepEqual(target.errors, []);
    }
    assert.ok(!standalone.requests.includes('/api/dashboard')); positives++;
    scenarios += 3;
  } finally { await failed.ctx.close(); await sparse.ctx.close(); await standalone.ctx.close(); }
  await inspectorGuidanceScenario(browser, base); scenarios++; positives++;
  await inspectorTelemetryScenario(browser, base); scenarios++; positives += 2;
  await missingSharedCostScenario(browser, base); scenarios++; positives++;
  const seeded = await scenarioPage(browser, base, {usage_timeseries:false}, {seed:true});
  try {
    await assert.rejects(() => requireAbsent(seeded.page, GROUPS.usage_timeseries.map(panel), 'seed'), /residual/);
    scenarios++; positives++;
  } finally { await seeded.ctx.close(); }
  assert.ok(scenarios >= 70 && positives > 0, 'zero or incomplete browser controls');
  console.log(`feature-surfaces: ${scenarios} scenarios, ${positives} positive assertions; seeded violation detected`);
}
