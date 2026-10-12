// Independent API fixtures for picker states that the dashboard fixture lacks.
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { API, FIXTURES } from './panel_server.mjs';
import { trackLayoutRequests, settleDashboard, layoutTiming } from './layout_waits.mjs';

export async function readPickerWidths(page, probe, selector, widths) {
  const samples = await page.evaluate(probe, {selector, widths});
  if (samples.length !== widths.length
    || samples.some((sample, i) => sample.width !== widths[i])) {
    throw new Error('width sweep sample sequence differs');
  }
  return samples;
}

async function sweepWidths(page, checks, target) {
  const failures = [];
  const selector = '.project-picker[data-picker="projects"]';
  await page.setViewportSize({width: 1440, height: 900});
  for (const state of ['unselected', 'selected', 'off-page']) {
    await page.$eval(selector, (el) => { el.style.width = '320px'; });
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(
      () => requestAnimationFrame(resolve))));
    if (state === 'selected') await page.click(selector + ' > .pp-proj');
    if (state === 'off-page') await page.click(selector + ' [title="Next page"]');
    const widths = Array.from({length: 420}, (_, i) => i + 1);
    const samples = await readPickerWidths(page, checks.probe, selector, widths);
    for (const {width, observed} of samples) {
      const findings = checks.fit([observed]);
      if (!observed.ids.length) findings.push({upper: 'one-chip floor',
        lower: 'no nonzero chip box'});
      if (state !== 'unselected') findings.push(...checks.selection([observed], target));
      for (const f of findings) failures.push(
        `picker width sweep ${state} ${width}px: ${f.upper} -> ${f.lower}`);
    }
  }
  return failures;
}

export async function runPickerCases(browser, base, rules, checks) {
  const template = JSON.parse(await readFile(join(FIXTURES, 'projects.json'), 'utf8'))
    .projects[4];
  const failures = [], passed = [];
  let executed = 0;
  for (const [count, width] of [[0, 1440], [1, 80], [2, 1440],
    [16, 220], [101, 261], [101, 80]]) {
    const projects = Array.from({length: count}, (_, i) => ({
      ...template,
      project_id: `project-${i}-with-a-long-selected-identifier`,
      display_name: i < 4 ? `p${i}` : `project-${i}-with-a-wide-display-name`,
    }));
    const context = await browser.newContext({viewport: {width, height: 900}});
    try {
      await context.route('**/api/**', async (route) => {
        const path = new URL(route.request().url()).pathname;
        if (path === '/api/events') return;
        const body = path === '/api/projects' ? JSON.stringify({projects})
          : API[path] ? await readFile(join(FIXTURES, API[path]), 'utf8') : '{}';
        await route.fulfill({status: 200, contentType: 'application/json', body});
      });
      const page = await context.newPage();
      trackLayoutRequests(page);
      await page.goto(base + '/', {waitUntil: 'load'});
      await page.waitForSelector('[data-projects-ready="true"]');
      await settleDashboard(page);
      for (const id of ['project-picker-pages-fit', 'project-picker-selected-fit']) {
        const rule = rules.find((r) => r.id === id);
        const findings = await layoutTiming(`fixture ${count}@${width} ${id}`,
          () => rule.run({page, width}));
        executed++;
        const label = `${id}: fixture ${count} projects at ${width}px`;
        for (const f of findings) failures.push(`${label}: ${f.upper} -> ${f.lower}`);
        if (!findings.length) passed.push(label + ': passed');
      }
      if (count === 101 && width === 80) {
        const sweep = await layoutTiming('width sweep',
          () => sweepWidths(page, checks, projects[0].project_id));
        executed++;
        failures.push(...sweep);
        if (!sweep.length) passed.push('picker width sweep: 1..420px, unselected/selected/off-page: passed');
      }
    } finally {
      await context.close();
    }
  }
  return {failures, passed, executed};
}
