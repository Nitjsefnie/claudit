// The rendered interaction guard for the dashboard panels (issue #647).
//
// scripts/ci/panel_layout.mjs answers whether the panels LAY OUT — the
// boxes do not overlap and nothing escapes its panel. It cannot answer
// whether the panels RESPOND: whether a bar highlights when the pointer
// rests on its bottom row, whether the tooltip the hover summons stays
// inside its own box, whether the page still moves around while the user
// looks at it. Those are the defect classes that reached the maintainer
// by eye on 2026-10-05 (#642, #643, #645, #646), and this guard pins the
// CATEGORIES, not the cases:
//
//   cold-cls        the sum of layout-shift scores on a cold load
//                   exceeds 0.1 (web.dev's "poor" threshold);
//   sweep-shift     any layout shift at all during the hover sweep —
//                   an interaction must not rearrange the page;
//   hover-tooltip   hovering a marked target (its centre, and its
//                   bottom pixel row) must summon the panel's tooltip;
//   hover-style     and must change the target's rendered style away
//                   from its resting style, so the response is visible
//                   on the element itself, panel to panel alike (#646);
//   tooltip-overflow a visible tooltip must stay inside the viewport
//                   and must not scroll its own content (#642), and a
//                   rendered long-KEY row must wrap inside the box — the
//                   probe mounts the real primitive with one (#701);
//   other-region    every sweep-time layout shift must resolve to a
//                   named region or panel through src/perf.js (#647
//                   item 5) — `other` means 'outside the panel grid',
//                   a real answer for a filter row and a wrong one for
//                   a shifted bar;
//   height-growth   no panel's height grows with the number of models:
//                   the same payloads with 2 vs 30+ models must render
//                   every panel at the same height within ±1px of
//                   rendering noise (#652's general case, so a third
//                   per-model grid cannot ship; #694's noise band) --
//                   or stay under the absolute ceiling a panel declares
//                   with data-max-h, the bounded treatment a capped
//                   list takes (#651): shorter at 2 roles than at 30+
//                   is the design there, not growth.
//
// Hover targets are enumerated from the DOM, so a new panel is covered
// automatically: every bar, bucket and point a panel makes interactive
// carries `data-hover-target`. A panel that renders with data but none
// is a `no-targets` finding — the sweep FAILS it (#690), so coverage is
// never opt-in again. A panel that carries no interactive surface by
// design (a legend, a stat panel) declares `data-static-panel` — but
// the declaration is listed, not free: STATIC_PANELS below names every
// static panel the guard has reviewed, and a declaration off the list
// is the `unlisted-static` finding (#834) — a data panel cannot exempt
// itself, which is how 'Page performance' hid a data table behind the
// declaration. A panel whose height is a per-entry LIST by design (one
// bar per row: Cost by Model, Tokens by Model) carries
// `data-list-panel` and is exempt from height-growth, printed every
// run like the ledger above.
//
// What WOULD hide a regression is failed loudly instead of passed
// silently:
//
//   * a page that renders no [data-panel] at all;
//   * a page whose sweep finds not one marked target anywhere;
//   * a ledger entry that never fired — the fix landed or the sweep can
//     no longer see the defect; either way the line is stale and the
//     guard is lying about something. Delete the line, or fix the sweep.
//
// Like panel_layout.mjs, this drives the REAL page (panel_server.mjs:
// the same public/index.html, the same src/*.jsx, the same in-browser
// Babel) against the frozen fixture set in fixtures/layout/. It needs no
// database and no R2. The browser driver is a devDependency like every
// other CI tool here; nothing in the shipped app reads package.json.
//
// Bounds: the sweep hovers every marked target twice (centre, bottom
// row) at every width, which is the guard's whole cost.
// PANEL_INTERACTIONS_MAX_TARGETS defaults to 0 — unlimited (#834: a
// cap that silently samples is a guard that checks less than it
// claims). An explicit cap that BITES is the `unhovered-targets`
// finding and fails the run; the knob is for local debugging only.
// A truncation is printed, never silent.
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { API, FIXTURES, serve, WIDTHS } from './panel_server.mjs';

// --- the KNOWN ledger -------------------------------------------------

// Defects this guard FOUND on panels that predate it, each filed as its
// own issue rather than fixed here — the same ledger shape panel_layout
// keeps, and the same tension: the guard must merge green to guard at
// all, and must say so out loud every run. `panel` null matches every
// panel; a regex matches by name. A fix makes its entry dead and the
// stale-entry check fails the run until the line is deleted.
export const FILED = [];

// The static panels the guard has reviewed: a [data-panel] declaring
// data-static-panel whose name is not listed here is the
// `unlisted-static` finding (#834) — the declaration is not a
// self-service exemption. The one listed name is the heatmap's
// aria-hidden gradient legend, which renders no data at all.
export const STATIC_PANELS = ['Activity Heatmap — legend'];

// The failure kinds this guard classifies. Closed: a kind outside this
// table is a bug in the guard itself.
export const KINDS = [
  'cold-cls', 'sweep-shift', 'hover-tooltip', 'hover-style',
  'tooltip-overflow', 'other-region', 'height-growth', 'no-targets',
  'unhovered-targets', 'unlisted-static',
];

// The matched LEDGER ENTRY (or null) — per entry, not per (issue, kind):
// two entries may share both, as #651's two panels do, and a
// liveness set keyed on the pair would call one twin's firing proof of
// the other's. filedFor keeps the issue-only view for callers that do
// not care which twin matched.
export function filedEntry(panel, kind) {
  return FILED.find(f => f.kind === kind
    && (f.panel === null
      || (f.panel instanceof RegExp ? f.panel.test(panel) : f.panel === panel)))
    || null;
}

// The bounded branch's comparison, pure so the node tests can prove it
// fires: the healthy page never breaches its own bound (the #651 card
// renders 338px under its declared 364px), so a dead or flipped
// comparison would green every run while enforcing nothing.
export function breachesBound(h, bound) {
  return h > Number(bound);
}

export function filedFor(panel, kind) {
  const hit = filedEntry(panel, kind);
  return hit ? hit.issue : null;
}

// --- the height comparison (#694) --------------------------------------

// The two height renders run in separate headless contexts, and
// Chromium's sub-pixel box rounding is not reproducible between them:
// the guard reds a one-pixel wobble (run 37427168152 on master —
// Cost/Tokens by Project 339px -> 338px at 1024px, a SHRINK failing a
// growth check). Heights agree within ±HEIGHT_NOISE_PX; a real
// per-entry row growth is many pixels, far outside the band.
export const HEIGHT_NOISE_PX = 1;

export function heightsAgree(twoPx, manyPx) {
  return Math.abs(manyPx - twoPx) <= HEIGHT_NOISE_PX;
}

// --- payload variants: 2 vs 30+ models --------------------------------

// Rewrites the frozen base payloads into the height check's two worlds.
// A top-level array whose first row carries a string `model` (or
// `agent_type`) field is keyed by that identity — `two` keeps the
// list's first two identities' rows, `many` expands it to at least 30
// distinct ones by cycling whole rows under suffixed names, so every
// panel that reads models (or agent roles, the #651 case) reads them
// at both scales. The LAST expanded copy carries a deliberately
// over-long identity: the hover sweep runs over this set, so the
// tooltip-overflow assertion is exercised against the longest label
// the page can render at every run (#642's probe).
//
// PRECONDITION: a rewritten list is UNIFORM — every row carries the
// list's identity field. A totals row without one would be dropped by
// `two` and invented into an `undefined~i` identity by `many`; no
// fixture list is mixed, and one that becomes so must refuse here
// rather than skew the comparison.
//
// Lists with neither identity field (heatmap cells, tool-usage buckets,
// cost-by-context buckets) pass through untouched: their heights must
// not move, and this is what makes the comparison read an identity-
// count change and nothing else.
export function variantsFrom(base) {
  const MANY = 30;
  const LONG = '~a-very-long-identity-name-probing-tooltip-overflow';
  const identity = row => (typeof row.model === 'string' ? 'model'
    : typeof row.agent_type === 'string' ? 'agent_type' : null);
  const rewriteArray = (rows, mode) => {
    const key = rows.length && rows[0] ? identity(rows[0]) : null;
    if (!key) return rows;
    const distinct = [];
    for (const row of rows) {
      if (row && typeof row[key] === 'string'
          && !distinct.includes(row[key])) distinct.push(row[key]);
    }
    if (!distinct.length) return rows;
    if (mode === 'two') {
      const keep = distinct.slice(0, 2);
      return rows.filter(r => keep.includes(r[key]));
    }
    if (distinct.length >= MANY) return rows;
    const copies = Math.ceil(MANY / distinct.length);
    const out = [];
    for (let i = 0; i < copies; i++) {
      for (const row of rows) {
        if (i === 0) { out.push(row); continue; }
        out.push({ ...row,
          [key]: `${row[key]}~${i === copies - 1 ? LONG.slice(1) : i}` });
      }
    }
    return out;
  };
  const rewriteDoc = (doc, mode) => {
    const out = {};
    for (const [file, payload] of Object.entries(doc)) {
      if (Array.isArray(payload)) { out[file] = rewriteArray(payload, mode); continue; }
      if (payload && typeof payload === 'object') {
        const copy = { ...payload };
        for (const [key, value] of Object.entries(payload)) {
          if (Array.isArray(value) && value.length && value[0]
              && typeof value[0] === 'object'
              && (typeof value[0].model === 'string'
                || typeof value[0].agent_type === 'string')) {
            copy[key] = rewriteArray(value, mode);
          }
        }
        out[file] = copy;
      } else out[file] = payload;
    }
    return out;
  };
  return { two: rewriteDoc(base, 'two'), many: rewriteDoc(base, 'many') };
}

// --- the in-page sweep -------------------------------------------------

// The style channels a hover highlight moves. fill-opacity (0.3 -> 0.85
// on a hovered time bar) and stroke (none -> white on a hovered hbar)
// are the two treatments in the panels today; background-color is the
// HTML-table rows' treatment (#834's Page performance rows). A new
// panel that highlights by another channel must add it here — and to
// the restated lists in MARK and READ below — or the guard fails it.
// Residual for row marks: elementFromPoint at a hovered table row
// returns the row's CELL (the mark lives on the row), so a row whose
// highlight dies degrades to OVERLAPPED prints, never a hover-style
// failure — the row's enforced response is its tooltip (hover-tooltip).
const STYLE_PROPS = ['fill', 'fill-opacity', 'opacity', 'stroke',
  'stroke-width', 'stroke-dasharray', 'background-color'];

const MAX_TARGETS = Number(process.env.PANEL_INTERACTIONS_MAX_TARGETS || '0');

// --- the #690 seeded-violation hooks -----------------------------------

// Each Expected-Behavior bullet of #690 has a seed that plants ONE
// violation of its category and must turn the sweep red — proven red
// locally and in panel-layout.yml's seeded step (driven by
// scripts/ci/panel_interactions_seeds.mjs, which asserts exit 1 per
// seed). A seed name outside the table is a setup error, refused.
export const SEEDS = ['no-targets', 'other-region', 'cold-region'];
const SEED = process.env.PANEL_INTERACTIONS_SEED || '';

if (SEED && !SEEDS.includes(SEED)) {
  console.error(`unknown PANEL_INTERACTIONS_SEED '${SEED}' — known: `
    + SEEDS.join(', '));
  process.exit(2);
}

// The no-targets seed strips every mark from ONE rendered non-static
// panel, the way a forgotten mark would leave it; the sweep then re-runs
// MARK on the stripped page, so the finding fires through the count the
// page itself reports (#835). Returns the number of marks it stripped.
const STRIP = panelIdx => {
  const svg = document.querySelector(`[data-sw="${panelIdx}"]`);
  if (!svg) return -1;
  let n = 0;
  for (const el of svg.querySelectorAll('[data-hover-target]')) {
    el.removeAttribute('data-hover-target');
    n += 1;
  }
  return n;
};

// Installed before the page's own scripts, so the observer sees the cold
// load. Shifts are buffered in-page; the sweep splits them into the cold
// load (before the first hover) and the sweep by the time it reads them.
// A region seed plants an extra shift whose node sits outside every
// named region and panel: `cold-region` plants it at document start
// (the cold-load half), `other-region` starts planting once the sweep is
// under way, so each half's attribution is asserted where it lands.
const INSTALL = () => {
  window.__sw = { shifts: [], registry: [] };
  try {
    const po = new PerformanceObserver(list => {
      for (const e of list.getEntries()) {
        window.__sw.shifts.push({
          v: e.value, input: !!e.hadRecentInput, t: performance.now(),
          node: (e.sources && e.sources[0] && e.sources[0].node) || null,
        });
      }
    });
    po.observe({ type: 'layout-shift', buffered: true });
  } catch (_) { /* no layout-shift observer in this browser */ }
  if (typeof window.__swSeed === 'string') {
    const plant = () => window.__sw.shifts.push({
      v: 0.001, input: false, t: performance.now(), node: document.body,
    });
    if (window.__swSeed === 'cold-region') plant();
    else {
      let planted = 0;
      const timer = setInterval(() => {
        plant();
        if (++planted >= 40) clearInterval(timer);
      }, 500);
    }
    window.__swSeed = null;
  }
};

// Enumerates every rendered panel and marks its hover targets, capturing
// each target's RESTING style before anything has been hovered.
const MARK = () => {
  // The same channels READ diffs; in-page functions are serialized
  // without their closure, so the list is restated here on purpose.
  const props = ['fill', 'fill-opacity', 'opacity', 'stroke',
    'stroke-width', 'stroke-dasharray', 'background-color'];
  window.__sw.rest = {};
  const panels = [];
  let next = 0;
  for (const svg of document.querySelectorAll('[data-panel]')) {
    const box = svg.getBoundingClientRect();
    if (box.width < 1 || box.height < 1) continue;
    svg.setAttribute('data-sw', String(panels.length));
    const ids = [];
    for (const el of svg.querySelectorAll('[data-hover-target]')) {
      el.setAttribute('data-sw-id', String(next));
      window.__sw.registry.push(el);
      ids.push(next);
      next += 1;
    }
    for (const id of ids) {
      const cs = getComputedStyle(window.__sw.registry[id]);
      window.__sw.rest[id] = props.map(p => cs.getPropertyValue(p));
    }
    panels.push({
      name: svg.getAttribute('data-panel'),
      list: svg.hasAttribute('data-list-panel'),
      static: svg.hasAttribute('data-static-panel'),
      nTargets: ids.length,
    });
  }
  return panels;
};

// Enumerates one panel's marked targets with their heights — the
// zero-height marks (an empty bin draws a zero-height bar) have no
// hover surface and are dropped node-side, not here, so the target
// count stays the drawn truth.
const AIM = panelIdx => {
  const svg = document.querySelector(`[data-sw="${panelIdx}"]`);
  if (!svg) return null;
  const targets = [...svg.querySelectorAll('[data-hover-target]')]
    .map(el => ({
      id: Number(el.getAttribute('data-sw-id')),
      h: el.getBoundingClientRect().height,
    }));
  return { name: svg.getAttribute('data-panel'), targets };
};

// Scrolls ONE target into view and returns the viewport-relative hover
// points the pointer needs (#834): a mark below the fold is hovered,
// not dropped — its own scrollIntoView centres it first. `out` means
// even its own scroll could not bring both hover points inside the
// viewport (a mark taller than the viewport), which the sweep fails
// on; every mark on the healthy page poses fine.
const POSE = id => {
  const el = window.__sw.registry[id];
  if (!el || !el.isConnected) return { gone: true };
  el.scrollIntoView({ block: 'center', inline: 'center' });
  const r = el.getBoundingClientRect();
  const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
  const bottom = r.y + r.height - 0.5;
  const both = cx > 0 && cx < window.innerWidth
    && cy > 0 && cy < window.innerHeight
    && bottom > 0 && bottom < window.innerHeight;
  return both ? { cx, cy, bottom } : { out: true };
};

// Reads the current response state for one hovered target: is the
// panel's tooltip visible, does it overflow, did the target's own style
// move off its resting snapshot?
// The failing target's own geometry, for the finding line: a FAIL you
// cannot place is a FAIL you cannot fix.
const TARGET = ({ id }) => {
  const el = window.__sw.registry[id];
  if (!el) return 'gone';
  const r = el.getBoundingClientRect();
  return `${el.tagName} @${Math.round(r.x)},${Math.round(r.y)} `
    + `${Math.round(r.width)}x${Math.round(r.height)}`;
};

// The tooltip text the page showed at read time: a FAIL line that
// names what the tooltip said is diagnosable from the log alone.
const TIPTXT = ({ panelIdx }) => {
  const svg = document.querySelector(`[data-sw="${panelIdx}"]`);
  const tipEl = svg && svg.parentElement.querySelector('.chart-tooltip');
  return tipEl ? (tipEl.textContent || '').slice(0, 40) : null;
};

const READ = ({ id, panelIdx }) => {
  // The same channels MARK captured; the list is restated here on
  // purpose (see MARK) — in-page functions are serialized without their
  // closure, so nothing outside the function body is in scope.
  const props = ['fill', 'fill-opacity', 'opacity', 'stroke',
    'stroke-width', 'stroke-dasharray', 'background-color'];
  const el = window.__sw.registry[id];
  if (!el || !el.isConnected) return { gone: true };
  const svg = document.querySelector(`[data-sw="${panelIdx}"]`);
  const tip = svg.parentElement.querySelector('.chart-tooltip');
  const visible = !!(tip && tip.style.visibility === 'visible'
    && tip.offsetParent !== null);
  let overflow = null;
  if (tip && visible) {
    const r = tip.getBoundingClientRect();
    const vw = window.innerWidth, vh = window.innerHeight;
    if (r.left < -0.5 || r.top < -0.5
        || r.right > vw + 0.5 || r.bottom > vh + 0.5) overflow = 'viewport';
    if (tip.scrollWidth > tip.clientWidth + 1) {
      overflow = overflow ? `${overflow}+width` : 'width';
    }
    if (tip.scrollHeight > tip.clientHeight + 1) {
      overflow = overflow ? `${overflow}+height` : 'height';
    }
  }
  const cs = getComputedStyle(el);
  const rest = window.__sw.rest[id];
  const changed = props.filter(
    (p, i) => cs.getPropertyValue(p) !== rest[i]);
  return { visible, overflow, changed };
};

// Hands the buffered shifts back, split into cold-load and sweep halves,
// EVERY entry attributed through the real src/perf.js logic (#690): a
// cold-load shift's attribution is asserted exactly like a sweep
// shift's — the cold load renders shift-free today (#643), so a shift
// there is a defect to name, not a background hum to log.
const SHIFTS = sweepStart => {
  const sweep = [];
  const cold = [];
  let coldSum = 0;
  for (const s of window.__sw.shifts) {
    if (s.input) continue;               // user-caused: not a defect
    const entry = {
      v: s.v,
      region: s.node ? window.perf.region(s.node) : null,
      attached: s.node ? s.node.isConnected : null,
    };
    if (s.t < sweepStart) {
      coldSum += s.v;
      cold.push(entry);
    } else {
      sweep.push(entry);
    }
  }
  return { sweep, cold, coldSum };
};

// The #701 long-key probe: mounts the REAL DashTooltip primitive with a
// row whose key is longer than the 280px tooltip box, inside a panel's
// own positioned wrapper — the rendered case the sweep cannot reach,
// because every key the live panels draw is a short fixed label. The
// containment comparison restates READ's overflow oracle on purpose
// (in-page functions are serialized without their closure).
const LONGKEY = () => {
  const svg = document.querySelector('[data-panel]');
  const host = svg && svg.parentElement;
  if (!host) return { mounted: false };
  svg.scrollIntoView({ block: 'center' });
  const mount = document.createElement('div');
  mount.style.cssText = 'position:absolute;inset:0;pointer-events:none';
  host.appendChild(mount);
  const KEY = 'a-very-long-tooltip-row-key-that-no-row-may-render-unwrapped-0123456789';
  const root = ReactDOM.createRoot(mount);
  let lastFacts = null;
  const read = () => {
    const tip = mount.querySelector('.chart-tooltip');
    if (!tip) return null;
    const r = tip.getBoundingClientRect();
    const vw = window.innerWidth, vh = window.innerHeight;
    let overflow = null;
    if (r.left < -0.5 || r.top < -0.5
        || r.right > vw + 0.5 || r.bottom > vh + 0.5) overflow = 'viewport';
    if (tip.scrollWidth > tip.clientWidth + 1) {
      overflow = overflow ? `${overflow}+width` : 'width';
    }
    if (tip.scrollHeight > tip.clientHeight + 1) {
      overflow = overflow ? `${overflow}+height` : 'height';
    }
    return { visible: tip.style.visibility === 'visible', overflow,
      keyWidth: Math.round(tip.querySelector('.chart-tooltip-key')
        .getBoundingClientRect().width) };
  };
  return (async () => {
    try {
      root.render(React.createElement(window.DashTooltip, {
        tip: {
          x: 12, y: 12, title: KEY,
          lines: [[KEY, '999,999,999,999,999'], ['short', '1']],
        },
      }));
      for (let i = 0; i < 6; i++) {
        await new Promise(r => requestAnimationFrame(r));
        const facts = read();
        if (facts && facts.visible) return { mounted: true, ...facts };
        if (facts) lastFacts = facts;
      }
      return { mounted: true, ...(lastFacts || { visible: false, overflow: null, keyWidth: 0 }) };
    } finally {
      root.unmount();
      mount.remove();
    }
  })();
};

async function main() {
  const { chromium } = await import('playwright');
  const base = {};
  for (const name of new Set(Object.values(API))) {
    base[name] = JSON.parse(await readFile(join(FIXTURES, name), 'utf8'));
  }
  const payloads = { base, ...variantsFrom(base) };

  const server = await serve();
  const browser = await chromium.launch();
  let failures = 0;
  const findings = new Map();   // dedup key -> finding
  const ledgerFired = new Set();   // the ENTRY objects that matched
  const record = (kind, panel, detail, width) => {
    const key = `${kind}|${panel}|${detail}`;
    const f = findings.get(key)
      || { kind, panel, detail, widths: new Set(), issue: null };
    if (!findings.has(key)) {
      const entry = filedEntry(panel, kind);
      f.entry = entry;
      f.issue = entry ? entry.issue : null;
      if (!f.issue) failures += 1;         // KNOWN: named, not failed
    }
    f.widths.add(width);
    findings.set(key, f);
    if (f.entry) ledgerFired.add(f.entry);
    return f;
  };
  const routeTo = set => async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/events') await new Promise(() => {});
    else if (API[path]) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(payloads[set][API[path]]),
      });
    } else await route.fulfill({ status: 200, body: '{}' });
  };

  try {
    // #843: widths run concurrently — the leg's wall time becomes the
    // slowest width, not their sum. The shared browser multiplexes the
    // contexts; the findings ledger and the failure count fold into the
    // same single-threaded maps they always were, and the summary below
    // reads them only after every width has landed.
    const runWidth = async width => {
      // --- the height pass: the same payloads at 2 vs 30+ models ------
      // (skipped under a seed: the seeded run proves the sweep's
      // classifiers, and the two extra renders would double its cost
      // for nothing — #843)
      if (!SEED) {
        const heights = {};
        for (const set of ['two', 'many']) {
          const ctx = await browser.newContext({
            viewport: { width, height: 1000 },
          });
          await ctx.route('**/api/**', routeTo(set));
          const page = await ctx.newPage();
          await page.goto('http://127.0.0.1:' + server.address().port + '/',
            { waitUntil: 'load' });
          await page.waitForSelector('[data-panel]', { timeout: 60_000 })
            .catch(() => { });
          await page.waitForTimeout(1_500);
          heights[set] = await page.evaluate(() =>
            [...document.querySelectorAll('[data-panel]')]
              .filter(s => s.getBoundingClientRect().height > 0)
              .map(s => {
                const host = s.closest('[data-max-h]');
                return [s.getAttribute('data-panel'),
                  Math.round(s.getBoundingClientRect().height),
                  s.hasAttribute('data-list-panel'),
                  host ? host.getAttribute('data-max-h') : null];
              }));
          await ctx.close();
        }
        if (!heights.two.length || !heights.many.length) {
          failures += 1;
          console.log(`NO PANELS  ${width}px height  a variant rendered no `
            + '[data-panel] — the payload rewrite or the fixtures broke');
        }
        const two = new Map(heights.two.map(([n, h]) => [n, h]));
        const many = new Map(heights.many.map(([n, h]) => [n, h]));
        // A panel's declared absolute bound, when it declares one: the
        // nearest [data-max-h] ancestor the page reported. A bounded
        // panel is judged against its ceiling in BOTH worlds and skips
        // the equality check -- a capped list is shorter at 2 roles than
        // at 30+, and that is the design (#651), not growth.
        const boundOf = new Map([...heights.two, ...heights.many]
          .map(([n, , , b]) => [n, b]).filter(([, b]) => b !== null));
        const lists = new Set([...heights.two, ...heights.many]
          .filter(([, , isList]) => isList).map(([n]) => n));
        for (const name of lists) {
          if (width === WIDTHS[0]) {
            console.log(`LIST        ${name}: per-entry bar list, exempt `
              + 'from height-growth (data-list-panel)');
          }
        }
        for (const [name, bound] of boundOf) {
          if (width === WIDTHS[0]) {
            console.log(`BOUNDED     ${name}: declared data-max-h `
              + `${bound}px -- judged against the ceiling in both worlds, `
              + 'equality skipped');
          }
        }
        for (const [name, hMany] of many) {
          if (!two.has(name) || lists.has(name) || boundOf.has(name)) continue;
          if (heightsAgree(two.get(name), hMany)) continue;
          record('height-growth', name,
            `height ${two.get(name)}px at 2 models -> ${hMany}px at 30+`,
            width);
        }
        // The absolute bound, both worlds, every width: the ceiling a
        // bounded panel declares is its own promise (#651). A breach
        // records like any finding; the ledger names no height-growth
        // entry today, so it fails the run.
        for (const [name, bound] of boundOf) {
          for (const set of [two, many]) {
            const h = set.get(name);
            if (h !== undefined && breachesBound(h, bound)) {
              record('height-growth', name,
                `height ${h}px exceeds its ${bound}px data-max-h bound`,
                width);
            }
          }
        }
        for (const name of two.keys()) {
          if (!many.has(name)) {
            failures += 1;
            console.log(`PANEL GONE  ${width}px  ${name} rendered with 2 `
              + 'models but not with 30+ — the panel or its gating broke');
          }
        }

      }

      // --- the sweep pass ---------------------------------------------
      // Swept over the MANY variant, not the base fixtures: it carries
      // the expanded identity list, so the hovers read 30+ roles and —
      // through the last copy's over-long identity (#642's overflow
      // probe) — the longest labels the page can render.
      const ctx = await browser.newContext({
        viewport: { width, height: 1000 },
      });
      await ctx.route('**/api/**', routeTo('many'));
      // The seed marker lands before INSTALL's script, so INSTALL sees
      // it: init scripts evaluate in the order they were added.
      if (SEED === 'other-region' || SEED === 'cold-region') {
        await ctx.addInitScript(
          `window.__swSeed = ${JSON.stringify(SEED)};`);
      }
      await ctx.addInitScript(INSTALL);
      const page = await ctx.newPage();
      await page.goto('http://127.0.0.1:' + server.address().port + '/',
        { waitUntil: 'load' });
      await page.waitForSelector('[data-panel]', { timeout: 60_000 })
        .catch(() => { });
      await page.waitForTimeout(1_500);
      const t0 = await page.evaluate(() => performance.now());
      let panels = await page.evaluate(MARK);
      if (SEED === 'no-targets') {
        const pi = panels.findIndex(p => !p.static && p.nTargets > 0);
        if (pi < 0 || await page.evaluate(STRIP, pi) < 1) {
          failures += 1;
          console.log(`SEED GONE   ${width}px  the no-targets seed found `
            + 'no rendered non-static panel with marks to strip');
        } else {
          // #835: the count must come from the PAGE, not this node-side
          // copy: re-run the mark pass on the stripped page, so the
          // no-targets finding fires through the same count an unseeded
          // run reads. The registry resets first — a second MARK appends
          // to it while ids restart at 0.
          await page.evaluate(() => { window.__sw.registry = []; });
          panels = await page.evaluate(MARK);
        }
      }
      if (!panels.length) {
        failures += 1;
        console.log(`NO PANELS  ${width}px sweep  no [data-panel] rendered `
          + '— the fixture payloads or the panel hooks changed');
      }
      let targetsTotal = 0;
      for (let pi = 0; pi < panels.length; pi++) {
        const panel = panels[pi];
        const listed = STATIC_PANELS.includes(panel.name);
        if (panel.static && !listed) {
          record('unlisted-static', panel.name,
            'declares data-static-panel — the guard lists: '
            + STATIC_PANELS.join(' | '), width);
        } else if (panel.static && width === WIDTHS[0]) {
          console.log(`STATIC      ${panel.name}: no interactive surface `
            + 'by design (listed) — no hover check');
        }
        if (!panel.static && !panel.nTargets) {
          record('no-targets', panel.name,
            'renders with data but carries no [data-hover-target]', width);
        }
        if (panel.static && panel.nTargets) {
          failures += 1;
          console.log(`BAD STATIC  ${width}px  ${panel.name} declares `
            + 'data-static-panel but renders marked targets — the '
            + 'declaration is a guard defect at this panel');
        }
        targetsTotal += panel.nTargets;
        if (panel.static) continue;
        const aimed = await page.evaluate(AIM, pi);
        const hoverable = aimed.targets.filter(t => t.h >= 1);
        const flat = aimed.targets.length - hoverable.length;
        if (flat && width === WIDTHS[0]) {
          console.log(`FLAT        ${panel.name}: ${flat} zero-height `
            + 'mark(s) — no hover surface, nothing to hover');
        }
        let targets = hoverable;
        // An explicit cap that BITES is a finding, never a silent sample
        // (#834); the knob is for local debugging only. Even sampling
        // with both ends kept, so a debugging cap still narrows the
        // sweep without biasing it toward one end.
        if (MAX_TARGETS > 0 && targets.length > MAX_TARGETS) {
          const step = (targets.length - 1) / (MAX_TARGETS - 1);
          const picked = new Set();
          for (let i = 0; i < MAX_TARGETS; i++) {
            picked.add(Math.round(i * step));
          }
          targets = [...picked].map(i => targets[i]);
          record('unhovered-targets', panel.name,
            `swept ${targets.length} of ${hoverable.length} targets `
            + `(PANEL_INTERACTIONS_MAX_TARGETS=${MAX_TARGETS})`, width);
          if (width === WIDTHS[0]) {
            console.log(`CAPPED      ${panel.name}: swept `
              + `${targets.length} of ${hoverable.length} targets `
              + `(PANEL_INTERACTIONS_MAX_TARGETS=${MAX_TARGETS}; 0 lifts it)`);
          }
        }
        for (const t of targets) {
          // #834: every mark is hovered — its own scrollIntoView brings
          // it into the viewport first; `out` (a mark taller than the
          // viewport) fails rather than passes.
          const pose = await page.evaluate(POSE, t.id);
          if (!pose || pose.out || pose.gone) {
            record('unhovered-targets', panel.name,
              `target #${t.id} outside the viewport after its own `
              + 'scrollIntoView', width);
            continue;
          }
          for (const point of [['centre', pose.cx, pose.cy],
            ['bottom', pose.cx, pose.bottom]]) {
            await page.mouse.move(point[1], point[2], { steps: 3 });
            // Settle before reading: the page's response is a React
            // commit (the lit target, the tooltip's second positioning
            // commit) and a READ that races it measures the PREVIOUS
            // hover — a stale tooltip reads visible and an unlit target
            // reads hover-style. Two animation frames bound the commit;
            // rAF is the page's own clock, not a sleep.
            await page.evaluate(() => new Promise(r => requestAnimationFrame(
              () => requestAnimationFrame(r))));
            const got = await page.evaluate(
              READ, { id: t.id, panelIdx: pi });
            if (got.gone) break;
            if (!got.visible || !got.changed.length || got.overflow) {
              // The diagnostics are failure-path work: two page
              // round-trips per HOVER were half the sweep's per-target
              // cost (#843); the healthy hover reads one.
              const where = `target #${t.id} ${point[0]} <${
                await page.evaluate(TARGET, { id: t.id })}>${
                await page.evaluate(TIPTXT, { panelIdx: pi }) || ''}`;
              if (!got.visible) {
                record('hover-tooltip', panel.name, where, width);
              }
              if (!got.changed.length) {
                // The style assertion applies where the pointer is ON the
                // target. Overlapping marks — coincident scatter dots, a
                // neighbour's disc under the probe point — answer through
                // the winner (the nearest datum wins, and the tooltip
                // above asserted it); the pointer not being on THIS mark
                // is printed, not failed.
                const onTarget = await page.evaluate(([x, y, id]) => {
                  const el = document.elementFromPoint(x, y);
                  const at = el && el.getAttribute('data-sw-id');
                  return at !== null && Number(at) === id;
                }, [point[1], point[2], t.id]);
                if (onTarget) {
                  record('hover-style', panel.name, where, width);
                } else if (width === WIDTHS[0]) {
                  console.log(`OVERLAPPED  ${panel.name}: ${where} `
                    + '— another mark owns the pointer here; the tooltip '
                    + 'check carried the pixel');
                }
              }
              if (got.overflow) {
                record('tooltip-overflow', panel.name,
                  `${where}: ${got.overflow}`, width);
              }
            }
          }
        }
        await page.mouse.move(1, 1, { steps: 3 });
      }
      // The static list is a ledger: a listed name that rendered no
      // panel is a dead entry the guard would carry forever (#834) —
      // the same liveness the FILED ledger's stale check enforces.
      if (width === WIDTHS[0]) {
        const seen = new Set(panels.map(p => p.name));
        for (const s of STATIC_PANELS) {
          if (!seen.has(s)) {
            failures += 1;
            console.log(`STALE STATIC  ${s} is listed but rendered no `
              + '[data-panel] — the rename or removal left a dead list '
              + 'entry. Fix the list.');
          }
        }
      }
      if (!targetsTotal) {
        failures += 1;
        console.log(`NO TARGETS  ${width}px sweep: not one [data-hover-`
          + 'target] on the page — the marks or the panels are gone, and '
          + 'this guard would be checking nothing');
      }
      const { sweep, cold, coldSum } = await page.evaluate(SHIFTS, t0);
      if (coldSum > 0.1) {
        record('cold-cls', '(page)', `cold-load CLS ${coldSum.toFixed(3)}`,
          width);
      }
      // #690: cold-load attribution is asserted, not logged — a shift the
      // cold load cannot name is the same blindness a sweep shift's
      // would be. The FILED ledger carries no catch-all for `other`
      // (#642's entry left with its fix, and a pinned test bans the
      // shape's return), so this finding fails the run.
      const coldOthers = cold.filter(s => s.region === 'other'
        || s.region === null);
      if (coldOthers.length) {
        const att = coldOthers.filter(s => s.attached).length;
        record('other-region', '(cold load)',
          `${coldOthers.length} cold-load shift(s) resolve to `
          + `${coldOthers[0].region ?? 'no-source'} (${att} source(s) `
          + 'still attached)', width);
      }
      if (sweep.length) {
        // One finding per width: the shifts are one defect's symptoms
        // (#642), and a line per shift value buries the run.
        const total = sweep.reduce((a, s) => a + s.v, 0);
        const attached = sweep.filter(s => s.attached).length;
        record('sweep-shift', '(sweep)',
          `${sweep.length} shifts totalling ${total.toFixed(3)}, `
          + `${attached} source(s) still attached`, width);
        const others = sweep.filter(s => s.region === 'other'
          || s.region === null);
        if (others.length) {
          const att = others.filter(s => s.attached).length;
          record('other-region', '(sweep)',
            `${others.length} of ${sweep.length} shifts resolve to `
            + `${others[0].region ?? 'no-source'} (${att} source(s) `
            + 'still attached)', width);
        }
      }

      // The rendered long-key case (#701): the sweep's real tooltips all
      // draw short fixed keys, so the probe mounts the real primitive
      // with a key longer than the box — the row must wrap inside it
      // under the same containment oracle the sweep reads. Run after the
      // shifts are read, so the probe's own mount and unmount can add
      // nothing to the sweep's ledger.
      if (panels.length) {
        const probe = await page.evaluate(LONGKEY);
        if (!probe.mounted) {
          failures += 1;
          console.log(`PROBE GONE  ${width}px  the long-key probe could `
            + 'not mount window.DashTooltip — the page renders no '
            + 'positioned panel wrapper to host it');
        } else if (!probe.visible) {
          failures += 1;
          console.log(`PROBE HIDDEN  ${width}px  the long-key probe's `
            + 'tooltip never became visible — DashTooltip\'s ready '
            + 'effect never ran inside the probe window');
        } else if (probe.overflow) {
          record('tooltip-overflow', '(long-key probe)',
            `long-key row: ${probe.overflow} (key ${probe.keyWidth}px)`,
            width);
        }
      }
      await ctx.close();
    };
    // #843: widths run concurrently, TWO at a time — the leg's wall time
    // approaches two widths, not their four-wide sum, while every page
    // load still gets the CPU an in-browser Babel compile needs: at full
    // four-wide parallelism the starved renders blew the selector wait
    // and rendered no panels at all. The findings ledger and the failure
    // count fold into the same single-threaded maps they always were,
    // and the summary below reads them only after every width landed.
    const queue = [...WIDTHS];
    await Promise.all(Array.from(
      { length: Math.min(2, queue.length) }, async () => {
        while (queue.length) await runWidth(queue.shift());
      }));
  } finally {
    await browser.close();
    server.close();
  }

  for (const f of [...findings.values()].sort(
    (a, b) => a.kind.localeCompare(b.kind))) {
    const tag = f.issue ? 'KNOWN' : 'FAIL';
    console.log(`${tag} ${f.kind.padEnd(16)} ${f.panel}: ${f.detail}`
      + `  [${[...f.widths].sort((a, b) => a - b).join(', ')}px]`
      + (f.issue ? ` (filed as #${f.issue})` : ''));
  }
  const stale = FILED.filter(f => !ledgerFired.has(f));
  for (const f of stale) {
    failures += 1;
    console.log(`STALE LEDGER  #${f.issue} ${f.kind} on ${f.panel ?? 'every '
      + 'panel'} never fired — the fix landed or the sweep cannot see the `
      + 'defect any more. Delete the line, or fix the sweep.');
  }
  const known = [...new Set([...findings.values()]
    .filter(f => f.issue).map(f => f.issue))].sort((a, b) => a - b);
  console.log(`panel-interactions: ${failures} new violation(s), `
    + `${known.length ? `known defects #${known.join(', #')}` : 'no known '
      + 'defect'}${stale.length ? `, ${stale.length} STALE ledger entrie(s)`
      : ''}`);
  process.exit(failures ? 1 : 0);
}

// Imported for the pure exports (FILED, KINDS, filedFor, variantsFrom):
// run the sweep only when executed directly, never when the pytest
// wiring tests import the module.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(err => { console.error(err); process.exit(2); });
}
