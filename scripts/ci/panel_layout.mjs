// Headless-Chromium layout guard for the dashboard panels (issue #631).
//
// Nothing else in this suite renders the dashboard in a browser: the
// tests are Python plus node runs of the parser JavaScript, so nothing
// ever asks the browser where it put anything. That is how issue #630 —
// the comparison panel's legend drawn over its own chart — reached a user
// with every check green.
//
// So this drives the REAL page: the same public/index.html, the same
// src/*.jsx, the same in-browser Babel, against a frozen fixture dataset
// (fixtures/layout/, captured from the backend over a generated mirror —
// never a live meter, never the box's Postgres).
//
// ONE generic check over every panel, not a suite per panel. Each panel
// marks its regions with `data-role` (title / legend / axis / plot); for
// every marked region this asserts:
//
//   1. containment — the region lies inside its own panel's box, so
//      nothing the panel drew is clipped away by the svg edge or spills
//      into the neighbouring panel;
//   2. pairwise non-intersection — no two marked regions of the same
//      panel overlap, so a legend never sits on the plot it explains and
//      an axis caption never runs into a tick label.
//
// A role marked on an element that CONTAINS another marked element is
// skipped as a pair: an element's box includes its descendants, so a
// wrapper and its own child always "intersect".
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { dirname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FIXTURES = join(REPO, 'fixtures', 'layout');

// Four widths: a desktop the panels were designed at, a laptop where the
// grid reflows, and two phone widths where every label is longest
// relative to the space it has. 320 is the viewport issue #630 was
// reported at, where the panel's title ran off the right edge and the
// legend's wrapped row fell off the bottom. Override locally with
// PANEL_LAYOUT_WIDTHS=1440,375.
const WIDTHS = (process.env.PANEL_LAYOUT_WIDTHS || '1440,1024,375,320')
  .split(',').map(Number).filter(n => n > 0);

// Every /api path the frontend fetches, and the fixture that answers it.
const API = {
  '/api/dashboard': 'dashboard.json',
  '/api/projects': 'projects.json',
  '/api/models': 'models.json',
  '/api/me': 'me.json',
  '/api/tool-usage': 'tool_usage.json',
  '/api/tool-error-rate': 'tool_error_rate.json',
  '/api/reply-latency': 'reply_latency.json',
  '/api/activity-heatmap': 'activity_heatmap.json',
  '/api/cost-by-context': 'cost_by_context.json',
  '/api/cost-by-agent': 'cost_by_agent.json',
  '/api/web-metrics': 'web_metrics.json',
};

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.jsx': 'text/babel; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.ico': 'image/x-icon',
};

// The one route that draws charts. Every panel on it lives here; the
// Cache route renders tables and stat rows, not a plot, so there is
// nothing on it for a bounding-box assertion to read.
const ROUTES = [{ name: 'overview', nav: null }];

// Defects the guard FOUND on panels that predate it, each filed as its own
// issue rather than fixed here. They are named, not skipped: every one is
// printed on every run with its issue number, so the ledger is visible in
// the job log and cannot rot into a silent pass, and anything the guard
// finds that is NOT here fails the leg.
//
// This is the one place the assertion is narrowed, and it is a real
// tension: a strict guard would be red on these six forever and could never
// merge, and a guard that never merges protects nothing. The ledger is the
// narrowest shape that both merges and still reds on the next regression.
// Deleting a line here is how a filed issue stops being an exemption: the
// fix makes the violation stop happening, and the line becomes dead.
//
// `roles` is the sorted role set of the violation: one entry for OVERLAP
// (two regions), one for OVERFLOW (one region escaping its panel).
const FILED = [
  { issue: 636, panel: 'Activity Heatmap', roles: ['axis'] },
  { issue: 636, panel: 'Activity Heatmap', roles: ['plot'] },
  { issue: 637, panel: 'Activity Heatmap — legend', roles: ['legend'] },
];

function filedFor(panel, roles) {
  const key = [...roles].sort().join('+');
  const hit = FILED.find(f => (f.panel === panel
    || (f.panel instanceof RegExp && f.panel.test(panel)))
    && f.roles.join('+') === key);
  return hit ? hit.issue : null;
}

function serve() {
  const server = createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const rel = decodeURIComponent(url.pathname);
    let file;
    if (rel === '/' || rel === '/index.html') {
      // The backend injects this line per request; the guard supplies the
      // same globals so the very first React render already knows them.
      const html = (await readFile(join(REPO, 'public', 'index.html'), 'utf8'))
        .replace(
          "<script>window.BACKEND_URL = window.BACKEND_URL || '';</script>",
          "<script>window.BACKEND_URL = '/'; window.IS_GUEST = false; "
          + 'window.IS_OPERATOR = true; window.BRAND = '
          + '{"name":"claudit","title":"claudit","description":"guard"};'
          + '</script>');
      res.writeHead(200, { 'content-type': TYPES['.html'] });
      res.end(html);
      return;
    }
    if (rel.startsWith('/src/')) file = join(REPO, normalize(rel));
    else if (rel === '/app.css' || rel === '/favicon.ico') {
      file = join(REPO, 'public', rel.slice(1));
    } else {
      res.writeHead(404).end();
      return;
    }
    // The repository root is the only tree this server reads from.
    if (!file.startsWith(REPO) || !existsSync(file)) {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, {
      'content-type': TYPES[rel.slice(rel.lastIndexOf('.'))]
        || 'application/octet-stream',
    });
    res.end(await readFile(file));
  });
  return new Promise(resolve => {
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}

// Runs inside the page. Reports, per panel, every marked region that
// escapes the panel box and every pair of marked regions that intersect.
const PROBE = () => {
  const EPS = 0.5;
  const round = n => Math.round(n * 10) / 10;
  const out = [];
  for (const panel of document.querySelectorAll('[data-panel]')) {
    const pb = panel.getBoundingClientRect();
    if (pb.width < 1 || pb.height < 1) continue;
    const roles = [...panel.querySelectorAll('[data-role]')].map(el => {
      const r = el.getBoundingClientRect();
      return {
        el,
        role: el.getAttribute('data-role'),
        label: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40),
        x: r.x, y: r.y, w: r.width, h: r.height,
      };
    }).filter(r => r.w > 0 && r.h > 0);

    // INSIDE each legend entry. Region-vs-region alone would miss #630's
    // own shape: the model name, the swatch rule and the count label are
    // three parts of ONE marked entry, so their boxes live inside one
    // marked element and the pairwise pass skips them as an
    // ancestor/descendant pair. That is the defect the maintainer saw —
    // a 17-character model name running through the rule and into the
    // count beside it — so it is asserted here, where it is visible.
    const within = [];
    for (const role of roles) {
      if (role.role !== 'legend') continue;
      const parts = [...role.el.querySelectorAll('text, line, rect, path, polyline')]
        .map(el => {
          const r = el.getBoundingClientRect();
          return { el, r };
        }).filter(p => p.r.width > 0 && p.r.height > 0);
      for (let i = 0; i < parts.length; i++) {
        for (let j = i + 1; j < parts.length; j++) {
          const a = parts[i].r, b = parts[j].r;
          if (parts[i].el.contains(parts[j].el)) continue;
          const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
          const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
          if (ox > EPS && oy > EPS) {
            within.push({ part: `${parts[i].el.tagName} and `
              + `${parts[j].el.tagName} inside "${role.label}"`,
              ox: round(ox), oy: round(oy) });
          }
        }
      }
    }

    const outside = roles.filter(r =>
      r.x < pb.x - EPS || r.y < pb.y - EPS
      || r.x + r.w > pb.x + pb.width + EPS
      || r.y + r.h > pb.y + pb.height + EPS).map(r => ({
        role: r.role, label: r.label,
        over: {
          left: round(pb.x - r.x), top: round(pb.y - r.y),
          right: round(r.x + r.w - (pb.x + pb.width)),
          bottom: round(r.y + r.h - (pb.y + pb.height)),
        },
      }));

    const hits = [];
    for (let i = 0; i < roles.length; i++) {
      for (let j = i + 1; j < roles.length; j++) {
        const a = roles[i], b = roles[j];
        // An element's box contains its descendants', so a marked wrapper
        // and its own marked child always "intersect". Skip that pair; it
        // is an instrumentation shape problem, not a layout one.
        if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
        // Two AXES are allowed to abut. The y-axis's bottom tick and the
        // x-axis's left tick meet at the plot's corner with their glyphs
        // 11px apart, but each label's LAYOUT box is a full line tall, so
        // the boxes touch and the ink does not. Every axis-vs-plot,
        // axis-vs-legend and axis-vs-title overlap is still asserted —
        // only the axis/axis corner case is dropped, because a box
        // comparison cannot tell it from a real collision.
        if (a.role === 'axis' && b.role === 'axis') continue;
        const ox = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
        const oy = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
        if (ox > EPS && oy > EPS) {
          hits.push({ a: a.role, al: a.label, b: b.role, bl: b.label,
            ox: round(ox), oy: round(oy) });
        }
      }
    }
    out.push({
      name: panel.getAttribute('data-panel'),
      roles: roles.length, outside, hits, within,
    });
  }
  return out;
};

async function main() {
  const server = await serve();
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch();
  let failures = 0;
  let panels = 0;
  let unmarked = 0;
  const known = [];
  try {
    for (const width of WIDTHS) {
      const ctx = await browser.newContext({
        viewport: { width, height: 1000 },
      });
      await ctx.route('**/api/**', async route => {
        const path = new URL(route.request().url()).pathname;
        if (path === '/api/events') {
          // A live SSE stream never resolves; neither does this one, so
          // the page does not sit in a reconnect loop.
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
      for (const route of ROUTES) {
        await page.goto(base + '/', { waitUntil: 'load' });
        if (route.nav) {
          await page.getByRole('button', { name: route.nav, exact: true }).click();
        }
        await page.waitForSelector('[data-panel]', { timeout: 30_000 })
          .catch(() => { });
        // The self-fetching panels mount in parallel with /api/dashboard
        // and re-render when their own response lands; wait for the
        // layout to settle before reading any box.
        await page.waitForTimeout(1_500);
        const found = await page.evaluate(PROBE);
        if (!found.length) {
          // A route that renders no marked panel is a guard that checks
          // nothing, which is worse than no guard: it reads as a pass.
          failures += 1;
          console.log(`NO PANELS  ${width}px ${route.name}  no [data-panel] `
            + 'rendered — the fixture payloads or the panel hooks changed');
        }
        for (const p of found) {
          panels += 1;
          if (!p.roles) unmarked += 1;
          for (const o of p.outside) {
            const issue = filedFor(p.name, [o.role]);
            if (issue) known.push(issue);
            else failures += 1;
            console.log(`${issue ? 'KNOWN' : 'OVERFLOW'}   ${width}px ${route.name}`
              + `  ${p.name}\n            ${o.role} "${o.label}" escapes `
              + `the panel by left ${o.over.left} top ${o.over.top} `
              + `right ${o.over.right} bottom ${o.over.bottom} px`
              + `${issue ? `  (filed as #${issue})` : ''}`);
          }
          for (const h of p.hits) {
            const issue = filedFor(p.name, [h.a, h.b]);
            if (issue) known.push(issue);
            else failures += 1;
            console.log(`${issue ? 'KNOWN' : 'OVERLAP'}    ${width}px ${route.name}`
              + `  ${p.name}\n            ${h.a} "${h.al}" x ${h.b} "${h.bl}"  `
              + `${h.ox}x${h.oy}px${issue ? `  (filed as #${issue})` : ''}`);
          }
          for (const w of p.within) {
            failures += 1;
            console.log(`COLLIDED  ${width}px ${route.name}  ${p.name}\n`
              + `            ${w.part}  ${w.ox}x${w.oy}px`);
          }
          if (process.env.PANEL_LAYOUT_VERBOSE) {
            console.log(`  panel ${p.name} roles=${p.roles}`);
          }
        }
      }
      await ctx.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  const filed = [...new Set(known)].sort((a, b) => a - b);
  console.log(`panel-layout: ${panels} panel instances over ${WIDTHS.length} `
    + `width(s), ${unmarked} without a data-role region, `
    + `${known.length} violation(s) already filed`
    + `${filed.length ? ` (#${filed.join(', #')})` : ''}, `
    + `${failures} new violation(s)`);
  process.exit(failures ? 1 : 0);
}

main().catch(err => { console.error(err); process.exit(2); });