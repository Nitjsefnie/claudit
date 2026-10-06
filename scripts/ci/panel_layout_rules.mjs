// The rendered layout-consistency harness for the dashboard (issue #772).
//
// panel_layout.mjs answers whether each panel's regions fit inside it;
// panel_interactions.mjs answers whether the panels respond. Neither asks
// how the SECTIONS sit against each other, which is how issue #772
// shipped: a class-less wrapper around the self-fetching panels took them
// out of .dashboard's flex-gap context, and every section from Tool Usage
// Ratio over Time downward rendered flush against its neighbour — with
// every other check green, because nothing measured the gaps.
//
// This harness is the generic home for rules over the RENDERED page
// layout: ONE page load per viewport width, every rule measured on that
// same page, each rule a separate assertion with its own seeded red case.
// Rules are entries in the RULES array below; each carries
//
//   id          a unique kebab-case name, printed with every finding;
//   description what invariant the rule holds, printed in the header;
//   seed        the rule's red case, applied to a FRESH reloaded page:
//               a DOM injection (never an app change, never a route)
//               that must make the rule fire — a seed that fires nothing
//               fails the run, because a guard whose red proof is dead
//               is lying about something;
//   run(ctx)    measures and returns violations. ctx = { page, width,
//               collect, collectPanels }: the live page (a rule may
//               drive an interaction — resize, click — before it
//               measures) and the box collectors.
//
// The measured universe is generic by construction: every [data-panel]
// and [data-list-panel] card plus every direct child section of
// .dashboard. A new panel is covered without being listed anywhere.
//
// Like its sibling guards this drives the REAL page (panel_server.mjs:
// the same public/index.html, the same src/*.jsx, the same in-browser
// Babel) against the frozen fixture set in fixtures/layout/. The browser
// driver is a devDependency; nothing in the shipped app reads
// package.json.
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { API, FIXTURES, serve, WIDTHS } from './panel_server.mjs';

// The narrowest legitimate gap in the dashboard is the 12px grid gap;
// the widest rendering noise Chromium's sub-pixel rounding produces on a
// real gap is well under 1px. Anything under 2px between vertically
// adjacent sections is a missing gap, with 6x headroom to the smallest
// legitimate value.
export const GAP_MIN = 2;

// One axis of slack for rounding: a box starting at most NOISE above the
// upper's bottom edge still reads as "starts at or below". Past that the
// boxes interleave, which is overlap — panel_layout.mjs's domain.
const NOISE = 1;

// Containment tolerance for the same rounding, when deciding that one
// box merely CONTAINS another (a card inside its wrapper): their edges
// may disagree by this many px without the two being separate sections.
const CONTAIN_TOL = 2;

// What the harness measures: panel cards, list-panel cards, and every
// direct child section of .dashboard. Zero-box elements (display:contents
// wrappers report an all-zero rect) are dropped by the collector.
export const COLLECT_SELECTOR =
  '[data-panel], [data-list-panel], .dashboard > *';

// --- the vertical-gap classifier (pure; node tests pin it) -------------

// Over `boxes` ([{ name, x, y, w, h }], viewport coords), return every
// vertically adjacent pair — stacked, horizontally overlapping, neither
// containing the other — whose gap is under `minGap`. Side-by-side grid
// columns, interleaved (overlapping) boxes and contained cards never
// pair, so the rule reads stacked sections and nothing else.
export function gapViolations(boxes, minGap = GAP_MIN) {
  const live = boxes.filter(b => b.w > 0 && b.h > 0);
  const sorted = [...live].sort((a, b) => a.y - b.y || a.x - b.x);
  const contains = (outer, inner) =>
    inner.x >= outer.x - CONTAIN_TOL
    && inner.y >= outer.y - CONTAIN_TOL
    && inner.x + inner.w <= outer.x + outer.w + CONTAIN_TOL
    && inner.y + inner.h <= outer.y + outer.h + CONTAIN_TOL;
  const out = [];
  for (let i = 0; i < sorted.length; i++) {
    for (let j = i + 1; j < sorted.length; j++) {
      const a = sorted[i];
      const b = sorted[j];
      if (contains(a, b) || contains(b, a)) continue;
      // Stacked, in one order or the other: the lower must start at or
      // below the upper's bottom (within NOISE), or the boxes interleave.
      let upper;
      let lower;
      if (b.y >= a.y + a.h - NOISE) [upper, lower] = [a, b];
      else if (a.y >= b.y + b.h - NOISE) [upper, lower] = [b, a];
      else continue;
      // Vertically adjacent requires real horizontal overlap: grid
      // columns side by side share a row but never an x-range.
      const overlap = Math.min(upper.x + upper.w, lower.x + lower.w)
        - Math.max(upper.x, lower.x);
      if (overlap <= 0.5 * Math.min(upper.w, lower.w)) continue;
      const gap = lower.y - (upper.y + upper.h);
      if (gap < minGap) {
        out.push({
          upper: upper.name,
          lower: lower.name,
          gap: Math.round(gap * 10) / 10,
        });
      }
    }
  }
  return out;
}

// --- the in-page collectors --------------------------------------------

// Every section and card box, zero-box elements (display:contents
// wrappers report an all-zero rect) dropped. A function handed to
// page.evaluate closes over nothing from this module, so the selector
// travels as an argument.
//
// Two notions, both structural and list-free:
//   section — an effective flex item of .dashboard: a boxed child, or a
//             descendant promoted through display:contents ancestors
//             (the self-fetch wrapper becomes display:contents in the
//             fix for #772, promoting its children to sections);
//   card    — the topmost element below its section on a marked
//             element's ancestor chain. data-panel sits on the chart
//             svg, and inner wrappers (the tooltip's position:relative
//             div) would otherwise steal the climb; the card is the
//             unit the section gap acts on.
export const COLLECT_PROBE = (selector) => {
  const dash = document.querySelector('.dashboard');
  if (!dash) return { error: 'no .dashboard on the page' };
  const isBoxed = (el) => {
    const d = getComputedStyle(el).display;
    return d !== 'inline' && d !== 'contents';
  };
  const effective = (parent) => [...parent.children].flatMap(
    (c) => (isBoxed(c) ? [c] : effective(c)),
  );
  const sections = effective(dash);
  const sectionSet = new Set(sections);
  const cardOf = (el) => {
    let card = el;
    for (let a = el.parentElement; a && !sectionSet.has(a); a = a.parentElement) {
      card = a;
    }
    return card;
  };
  const seen = new Set();
  const boxes = [];
  const take = (el, forcedName) => {
    if (seen.has(el)) return;
    seen.add(el);
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;
    const name = forcedName
      || el.getAttribute('data-panel')
      || el.getAttribute('data-list-panel')
      || (el.className && String(el.className).trim())
      || el.tagName.toLowerCase();
    boxes.push({ name, x: r.x, y: r.y, w: r.width, h: r.height });
  };
  for (const el of sections) take(el);
  for (const el of document.querySelectorAll(selector)) {
    const mark = el.getAttribute('data-panel')
      || el.getAttribute('data-list-panel');
    take(cardOf(el), mark || undefined);
  }
  return { boxes };
};

// Per [data-panel] element: the panel's own box, its CARD box, and
// every [data-role] region scoped to that card. The card is the
// effective section of .dashboard holding the mark (boxed children,
// promoted through display:contents): the chart svgs are display:block,
// so a boxed ancestor climb stops at the svg and never sees the legend
// strips that are html siblings of it (#773). Sections only exist
// inside .dashboard; without one, fall back to the nearest boxed
// ancestor-or-self.
export const PANELS_PROBE = () => {
  const isBoxed = (el) => {
    const d = getComputedStyle(el).display;
    return d !== 'inline' && d !== 'contents';
  };
  const dash = document.querySelector('.dashboard');
  const effective = (parent) => [...parent.children].flatMap(
    (c) => (isBoxed(c) ? [c] : effective(c)),
  );
  const sectionSet = new Set(dash ? effective(dash) : []);
  const cardOf = (el) => {
    if (!dash) {
      if (isBoxed(el)) return el;
      for (let a = el.parentElement; a; a = a.parentElement) {
        if (isBoxed(a)) return a;
      }
      return el;
    }
    for (let a = el; a; a = a.parentElement) {
      if (sectionSet.has(a)) return a;
    }
    return el;
  };
  const cards = [];
  for (const p of document.querySelectorAll('[data-panel]')) {
    const pb = p.getBoundingClientRect();
    const card = cardOf(p);
    const cb = card.getBoundingClientRect();
    const roles = [];
    for (const r of card.querySelectorAll('[data-role]')) {
      const rb = r.getBoundingClientRect();
      if (rb.width < 1 || rb.height < 1) continue;
      roles.push({
        role: r.getAttribute('data-role'),
        label: (r.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40),
        x: rb.x, y: rb.y, w: rb.width, h: rb.height,
      });
    }
    cards.push({
      panel: p.getAttribute('data-panel'),
      box: { x: pb.x, y: pb.y, w: pb.width, h: pb.height },
      card: { x: cb.x, y: cb.y, w: cb.width, h: cb.height },
      roles,
    });
  }
  return { cards };
};

// --- the rules ----------------------------------------------------------

// Seed for the vertical-gap rule's red proof: cancel the flex gap
// between the LAST two direct sections of .dashboard that contain no
// panel of their own (a containing wrapper is containment-skipped by
// the classifier, so a gap closed against it could never fire). The
// lower of the pair is pulled up by exactly the container's own row
// gap, seeding one genuinely zero-gap pair on the live page — no app
// change, no fixture — and the pair pulled is returned so the proof is
// precise.
async function seedZeroGap(page) {
  return page.evaluate(() => {
    const dash = document.querySelector('.dashboard');
    const marks = '[data-panel], [data-list-panel]';
    const leaves = [...dash.children].filter((el) => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && !el.querySelector(marks);
    });
    if (leaves.length < 2) return null;
    const lower = leaves[leaves.length - 1];
    const upper = leaves[leaves.length - 2];
    // The visual space between two flex items is the container's gap
    // PLUS the pair's own margins; cancel all of it so the pair lands
    // genuinely flush whatever the stylesheet gives them.
    const gapPx = parseFloat(getComputedStyle(dash).rowGap) || 0;
    const marginOf = (el, side) => {
      const m = parseFloat(getComputedStyle(el)[side]);
      return Number.isFinite(m) ? m : 0;
    };
    const pull = gapPx
      + marginOf(lower, 'marginTop')
      + marginOf(upper, 'marginBottom');
    lower.style.marginTop = `${marginOf(lower, 'marginTop') - pull}px`;
    return {
      upper: upper.className || upper.tagName.toLowerCase(),
      lower: lower.className || lower.tagName.toLowerCase(),
      pulled: gapPx,
    };
  });
}

// --- the legend-placement classifier (pure; node tests pin it) ---------
//
// Over owned chart groups — PANELS_OWNERSHIP_PROBE's output, one entry
// per marked svg: { panel, plots: [box], legends: [{ label, ...box }] } —
// return every legend that does not sit BELOW every plot of its own
// chart. The dashboard convention is legend-under-chart (the comparison
// panel's legend moved there for #630, the Tool Error Rate strips for
// #773); a legend above its plot — or beside it, which the vertical test
// also fails — is the outlier. A chart with no legend, or a legend with
// no plot, checks nothing: conforming by having nothing to compare.
export function legendBelowViolations(groups, eps = NOISE) {
  const out = [];
  for (const group of groups) {
    for (const leg of group.legends) {
      for (const plot of group.plots) {
        const plotBottom = plot.y + plot.h;
        if (leg.y < plotBottom - eps) {
          out.push({
            upper: `${group.panel}: legend "${leg.label}"`,
            lower: 'plot',
            gap: Math.round((leg.y - plotBottom) * 10) / 10,
          });
        }
      }
    }
  }
  return out;
}

// In-page: group every marked legend and plot under the chart it
// belongs to. A role inside a marked svg belongs to that svg; an html
// legend strip belongs to the nearest ancestor holding EXACTLY ONE
// marked svg — the chart card. An ancestor with two or more marked svgs
// (a grid section of chart cards) owns nothing: a legend there is
// unattributable, so it pairs with no plot rather than firing against a
// neighbouring card's chart, and a card holding several marked svgs
// keeps its strips unowned too. That is the conservative reading: the
// browser legs prove attribution where it exists (the seeded strip
// fires) and no finding where it does not (the unseeded page is green).
const LEGEND_GROUPS_PROBE = () => {
  const boxOf = (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, w: r.width, h: r.height };
  };
  const SEL = '[data-panel]';
  const ownerOf = (el) => {
    const inner = el.closest(SEL);
    if (inner) return inner;
    for (let a = el.parentElement; a; a = a.parentElement) {
      const marks = a.querySelectorAll(SEL).length;
      if (marks === 1) return a.querySelector(SEL);
      if (marks > 1) return null;
    }
    return null;
  };
  const groups = new Map();
  for (const mark of document.querySelectorAll(SEL)) {
    const plots = mark.matches('[data-role="plot"]')
      ? [mark]
      : [...mark.querySelectorAll('[data-role="plot"]')];
    groups.set(mark, {
      panel: mark.getAttribute('data-panel'),
      plots: plots
        .map((el) => boxOf(el))
        .filter((b) => b.w > 0 && b.h > 0),
      legends: [],
    });
  }
  for (const el of document.querySelectorAll('[data-role="legend"]')) {
    const owner = ownerOf(el);
    const group = owner && groups.get(owner);
    if (!group) continue;
    const box = boxOf(el);
    if (box.w < 1 || box.h < 1) continue;
    group.legends.push({
      label: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40),
      ...box,
    });
  }
  return [...groups.values()].map((g) => ({
    panel: g.panel,
    plots: g.plots,
    legends: g.legends,
  }));
};

// Seed for the legend rule's red proof: inject a marked legend strip as
// the FIRST child of the first SINGLE-chart card — a section holding
// exactly one marked panel — above its plot, the shape #773 shipped.
// A multi-chart section (the TimeSeries grid) owns no legend: its
// strips are unattributable and the rule skips them, so the seed must
// land where ownership attributes the strip to a chart. DOM injection
// on the fresh reloaded page the runner hands every seed: no app
// change, no route.
async function seedLegendAbovePlot(page) {
  return page.evaluate(() => {
    const dash = document.querySelector('.dashboard');
    if (!dash) return null;
    const isBoxed = (el) => {
      const d = getComputedStyle(el).display;
      return d !== 'inline' && d !== 'contents';
    };
    const effective = (parent) => [...parent.children].flatMap(
      (c) => (isBoxed(c) ? [c] : effective(c)),
    );
    const sections = new Set(effective(dash));
    for (const mark of document.querySelectorAll('[data-panel]')) {
      let card = null;
      for (let a = mark; a; a = a.parentElement) {
        if (sections.has(a)) {
          card = a;
          break;
        }
      }
      if (!card) continue;
      if (card.querySelectorAll('[data-panel]').length !== 1) continue;
      const strip = document.createElement('div');
      strip.setAttribute('data-role', 'legend');
      strip.textContent = 'seeded outlier legend';
      strip.style.padding = '6px 14px';
      card.insertBefore(strip, card.firstChild);
      return { seeded: mark.getAttribute('data-panel') };
    }
    return null;
  });
}

export const RULES = [
  {
    id: 'vertical-gap',
    description: 'no two vertically adjacent sections or charts render'
      + ` with less than a ${GAP_MIN}px vertical gap (issue #772)`,
    seed: seedZeroGap,
    run: async (ctx) => {
      const { boxes } = await ctx.collect();
      return gapViolations(boxes);
    },
  },
  {
    id: 'legend-below-plot',
    description: 'every legend renders below every plot of its own chart'
      + ' — the dashboard-wide legend convention (issue #773)',
    seed: seedLegendAbovePlot,
    run: async (ctx) => legendBelowViolations(
      await ctx.page.evaluate(LEGEND_GROUPS_PROBE),
    ),
  },
];

// --- the runner ---------------------------------------------------------

async function settle(page) {
  await page.waitForSelector('[data-panel]', { timeout: 30_000 });
  // The self-fetching panels fill after their own responses land; the
  // sibling guards settle the same way.
  await page.waitForTimeout(1_500);
}

export async function runRules(ruleFilter) {
  // Dynamic, like panel_interactions.mjs: importing this module (the
  // node-driven tests do) must not resolve playwright — the runner's
  // tests job ships no node_modules; only the browser leg installs it.
  const { chromium } = await import('playwright');
  const server = await serve();
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch();
  const failures = [];
  const fired = [];
  try {
    for (const width of WIDTHS) {
      const ctx = await browser.newContext({
        viewport: { width, height: 900 },
      });
      // Fulfil the API from the frozen fixtures, exactly as
      // panel_layout.mjs does — serve() alone 404s every /api path, and
      // a page fetching nothing renders only the self-fetch shells: the
      // rule would measure synthetic-preview chrome and call it the
      // dashboard (#773).
      await ctx.route('**/api/**', async (route) => {
        const path = new URL(route.request().url()).pathname;
        if (path === '/api/events') {
          await new Promise(() => {});
          return;
        }
        const name = API[path];
        if (!name) {
          await route.fulfill({ status: 200, body: '{}' });
          return;
        }
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: await readFile(join(FIXTURES, name), 'utf8'),
        });
      });
      const page = await ctx.newPage();
      await page.goto(`${base}/`, { waitUntil: 'load' });
      await settle(page);

      const ruleCtx = {
        page,
        width,
        collect: () => page.evaluate(COLLECT_PROBE, COLLECT_SELECTOR),
        collectPanels: () => page.evaluate(PANELS_PROBE),
      };
      const rules = ruleFilter
        ? RULES.filter((r) => r.id === ruleFilter)
        : RULES;
      if (!rules.length) throw new Error(`no rule named ${ruleFilter}`);

      // Unseeded: every rule must pass on the real page.
      for (const rule of rules) {
        const violations = await rule.run(ruleCtx);
        for (const v of violations) {
          failures.push(`${rule.id}: width ${width}: ${v.upper} ->`
            + ` ${v.lower} gap ${v.gap}px`);
        }
      }

      // Seeded, per rule on a fresh page: the seed must make the rule
      // fire, or the rule's red proof is dead and the guard lies.
      for (const rule of rules) {
        await page.reload({ waitUntil: 'load' });
        await settle(page);
        const seeded = await rule.seed(page);
        await page.waitForTimeout(300);
        const violations = await rule.run(ruleCtx);
        if (!violations.length) {
          failures.push(`${rule.id}: width ${width}: seeded red case`
            + ` (${JSON.stringify(seeded)}) fired nothing — the guard is`
            + ' lying about something; fix the seed or delete the rule');
        } else {
          fired.push(`${rule.id}: width ${width}: red case fired on`
            + ` ${violations[0].upper} -> ${violations[0].lower}`);
        }
      }
      await ctx.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  return { failures, fired };
}

export async function main() {
  const filterIdx = process.argv.indexOf('--rule');
  const ruleFilter = filterIdx > -1 ? process.argv[filterIdx + 1] : null;
  console.log(`layout rules: ${RULES.map((r) => r.id).join(', ')}`);
  const { failures, fired } = await runRules(ruleFilter);
  for (const f of fired) console.log(`  ${f}`);
  if (failures.length) {
    console.error(`FAIL (${failures.length}):`);
    for (const f of failures) console.error(`  ${f}`);
    process.exitCode = 1;
    return;
  }
  console.log('PASS: every rule green unseeded and its seeded red case'
    + ' fired');
}

// Launch only on direct execution: the node-driven tests import this
// module for its pure halves, and an import that opened Chromium would
// never return.
const INVOKED_DIRECTLY = process.argv[1]
  && import.meta.url === pathToFileURL(process.argv[1]).href;
if (INVOKED_DIRECTLY) {
  await main();
}
