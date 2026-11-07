// The interaction guard's height snapshot and its final log.
import { fontFamilies } from './cdn_route.mjs';
import { settleDashboard, trackLayoutRequests } from './layout_waits.mjs';

// Runs in the page through page.evaluate, so it may reference nothing outside.
export function capturePanels() {
  const panels = [...document.querySelectorAll('[data-panel]')]
    .filter(s => s.getBoundingClientRect().height > 0);
  // Every height is read before any innerText, which forces a layout.
  const measured = panels.map(s => {
    const host = s.closest('[data-max-h]');
    return [s.getAttribute('data-panel'),
      Math.round(s.getBoundingClientRect().height),
      s.hasAttribute('data-list-panel'),
      host ? host.getAttribute('data-max-h') : null];
  });
  return measured.map((entry, i) => {
    const s = panels[i], html = s instanceof HTMLElement;
    // An svg root has no innerText, and its children are drawing primitives.
    const rows = html ? s.innerText.split('\n')
      : [...s.querySelectorAll('text')].map(t => t.textContent);
    const regions = (html ? [...s.children] : []).map((el, j) => {
      const role = el.getAttribute('data-role');
      const cls = el.getAttribute('class')?.trim();
      const suffix = role ? `[data-role=${role}]`
        : cls ? `.${cls.split(/\s+/).join('.')}` : '';
      return { label: `${j + 1}:${el.localName}${suffix}`,
        h: Math.round(el.getBoundingClientRect().height) };
    });
    return [...entry, rows.map(row => row.trim()).filter(Boolean), regions];
  });
}

export function listFontFaces() {
  return [...document.fonts].map(face => ({ family: face.family, status: face.status }));
}

export function fontsNotLoaded(faces, families) {
  return families.filter(family => {
    const own = faces.filter(face => face.family === family);
    return !own.some(face => face.status === 'loaded')
      || own.some(face => face.status === 'error');
  });
}

export async function snapshotWorld(context, url, world) {
  const page = await context.newPage();
  trackLayoutRequests(page);
  await page.goto(url, { waitUntil: 'load' });
  await settleDashboard(page, {timeoutMs: 60_000}).catch(error => {
    throw new Error(`${world}: ${error.message}`, { cause: error });
  });
  // A failed stylesheet leaves document.fonts empty and ready, so ask the faces.
  const missing = fontsNotLoaded(await page.evaluate(listFontFaces), fontFamilies);
  if (missing.length) {
    throw new Error(`infrastructure: ${world}: webfont ${missing.join(', ')} did not load, `
      + 'so its heights would measure a fallback font, not a height-growth verdict');
  }
  const panels = await page.evaluate(capturePanels);
  if (!panels.length) {
    throw new Error(`${world}: rendered no [data-panel]; the payload rewrite or the fixtures broke`);
  }
  return panels;
}

const MAX_ROWS = 40, MAX_REGIONS = 40, MAX_ROW_CHARS = 240, MAX_LABEL_CHARS = 100;
// Two panels at every width; a wider red is a broken fixture, not a flake.
const MAX_BLOCKS = 8;
const clip = (text, max) => text.length > max ? `${text.slice(0, max - 1)}…` : text;

export function describeHeightGrowth(two, many) {
  const lines = [];
  for (const [world, entry, mark, other] of [
    ['2 models', two, '-', many], ['30+ models', many, '+', two],
  ]) {
    if (!entry) { lines.push(`  ${world} (not rendered)`); continue; }
    const [, height, , , rows, regions] = entry;
    const otherRows = new Set(other ? other[4] : []);
    lines.push(`  ${world} (${height}px)`,
      regions.length ? '    regions:' : '    regions: none');
    for (const region of regions.slice(0, MAX_REGIONS)) {
      lines.push(`      ${clip(region.label, MAX_LABEL_CHARS)}: ${region.h}px`);
    }
    if (regions.length > MAX_REGIONS) {
      lines.push(`      ... ${regions.length - MAX_REGIONS} region(s) omitted`);
    }
    lines.push('    rows (marked when absent from the other world):');
    for (const row of rows.slice(0, MAX_ROWS)) {
      lines.push(`    ${otherRows.has(row) ? ' ' : mark} ${clip(row, MAX_ROW_CHARS)}`);
    }
    if (rows.length > MAX_ROWS) {
      lines.push(`      ... ${rows.length - MAX_ROWS} row(s) omitted`);
    }
  }
  return lines;
}

// Verdicts and the summary come first, so a wide red keeps them readable.
export function reportLines(findings, stale, failures, snapshots) {
  const lines = [], blocks = [];
  for (const f of [...findings].sort((a, b) => a.kind.localeCompare(b.kind))) {
    const widths = [...f.widths].sort((a, b) => a - b);
    lines.push(`${f.issue ? 'KNOWN' : 'FAIL'} ${f.kind.padEnd(16)} ${f.panel}: `
      + `${f.detail}  [${widths.join(', ')}px]`
      + (f.issue ? ` (filed as #${f.issue})` : ''));
    if (f.kind !== 'height-growth') continue;
    for (const width of widths) {
      const pick = set => snapshots.get(width)?.[set]
        .findLast(([n]) => n === f.panel);
      let block;
      try { block = describeHeightGrowth(pick('two'), pick('many')); }
      catch (error) { block = [`  snapshot unusable: ${error.message}`]; }
      blocks.push([`HEIGHT      ${f.panel} at ${width}px`, ...block]);
    }
  }
  for (const f of stale) {
    lines.push(`STALE LEDGER  #${f.issue} ${f.kind} on ${f.panel ?? 'every '
      + 'panel'} never fired — the fix landed or the sweep cannot see the `
      + 'defect any more. Delete the line, or fix the sweep.');
  }
  const known = [...new Set(findings.filter(f => f.issue).map(f => f.issue))]
    .sort((a, b) => a - b);
  lines.push(`panel-interactions: ${failures} new violation(s), `
    + `${known.length ? `known defects #${known.join(', #')}` : 'no known '
      + 'defect'}${stale.length ? `, ${stale.length} STALE ledger entrie(s)`
      : ''}`);
  for (const block of blocks.slice(0, MAX_BLOCKS)) lines.push(...block);
  if (blocks.length > MAX_BLOCKS) {
    lines.push(`... ${blocks.length - MAX_BLOCKS} height-growth block(s) omitted`);
  }
  return lines;
}
