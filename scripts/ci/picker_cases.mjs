// Independent API fixtures for picker states that the dashboard fixture lacks.
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { API, FIXTURES } from './panel_server.mjs';

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
    for (let width = 1; width <= 420; width++) {
      await page.$eval(selector, (el, w) => { el.style.width = w + 'px'; }, width);
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(
        () => requestAnimationFrame(resolve))));
      const observed = await page.evaluate(checks.probe, selector);
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
      await page.goto(base + '/', {waitUntil: 'load'});
      await page.waitForSelector('[data-projects-ready="true"]');
      await page.evaluate(() => document.fonts.ready);
      for (const id of ['project-picker-pages-fit', 'project-picker-selected-fit']) {
        const rule = rules.find((r) => r.id === id);
        const findings = await rule.run({page, width});
        const label = `${id}: fixture ${count} projects at ${width}px`;
        for (const f of findings) failures.push(`${label}: ${f.upper} -> ${f.lower}`);
        if (!findings.length) passed.push(label + ': passed');
      }
      if (count === 101 && width === 80) {
        const sweep = await sweepWidths(page, checks, projects[0].project_id);
        failures.push(...sweep);
        if (!sweep.length) passed.push('picker width sweep: 1..420px, unselected/selected/off-page: passed');
      }
    } finally {
      await context.close();
    }
  }
  return {failures, passed};
}
