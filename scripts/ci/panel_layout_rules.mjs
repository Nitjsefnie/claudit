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
// containing the other — as { upper, lower, gap }, whatever the gap.
// Side-by-side grid columns, interleaved (overlapping) boxes and
// contained cards never pair, so the gap rules read stacked sections and
// nothing else. gapViolations thresholds the pairs; gapConsistencyViolations
// compares ADJACENT pairs against the expected container distance.

// The stacking primitives both enumerations share. `boxContains` is the
// containment skip (their edges may disagree by CONTAIN_TOL and the two
// are still one section holding the other); `stackedGap` orients a pair
// — upper above lower, real horizontal overlap — and reports its gap,
// or null when the boxes interleave or barely overlap.
const boxContains = (outer, inner) =>
  inner.x >= outer.x - CONTAIN_TOL
  && inner.y >= outer.y - CONTAIN_TOL
  && inner.x + inner.w <= outer.x + outer.w + CONTAIN_TOL
  && inner.y + inner.h <= outer.y + outer.h + CONTAIN_TOL;

const stackedGap = (a, b) => {
  let upper;
  let lower;
  // Stacked, in one order or the other: the lower must start at or
  // below the upper's bottom (within NOISE), or the boxes interleave.
  if (b.y >= a.y + a.h - NOISE) [upper, lower] = [a, b];
  else if (a.y >= b.y + b.h - NOISE) [upper, lower] = [b, a];
  else return null;
  // Vertically adjacent requires real horizontal overlap: grid
  // columns side by side share a row but never an x-range.
  const overlap = Math.min(upper.x + upper.w, lower.x + lower.w)
    - Math.max(upper.x, lower.x);
  if (overlap <= 0.5 * Math.min(upper.w, lower.w)) return null;
  return {
    upper,
    lower,
    gap: Math.round((lower.y - (upper.y + upper.h)) * 10) / 10,
  };
};

export function stackedPairs(boxes) {
  const live = boxes.filter(b => b.w > 0 && b.h > 0);
  const sorted = [...live].sort((a, b) => a.y - b.y || a.x - b.x);
  const out = [];
  for (let i = 0; i < sorted.length; i++) {
    for (let j = i + 1; j < sorted.length; j++) {
      if (boxContains(sorted[i], sorted[j])
        || boxContains(sorted[j], sorted[i])) continue;
      const g = stackedGap(sorted[i], sorted[j]);
      if (g) {
        out.push({
          upper: g.upper.name,
          lower: g.lower.name,
          gap: g.gap,
          upperMb: g.upper.mb || 0,
          lowerMt: g.lower.mt || 0,
        });
      }
    }
  }
  return out;
}

// ADJACENT stacked pairs only (#796): the FIRST stacked, non-contained
// successor of each box in y-order, and the scan stops there. For the
// sections this rule reads — boxes many pixels tall, sibling gaps at
// or above the GAP_MIN floor — sorted-by-y nearest emission IS
// adjacency: any box stacked between a pair sorts before the further
// member and pairs with the earlier box first. The claim is scoped:
// a box at most CONTAIN_TOL + NOISE tall nested inside its neighbour's
// edge — its predecessor's bottom edge (e.g. y=0 h100, y=99.5 h1,
// y=116) or the top edge of the box below at a 1px gap — is
// containment-skipped against that neighbour, so nearest emission
// pairs across it where a between-box search would have suppressed
// the pair. An equality rule over EVERY stacked pair would fire on
// sections pages apart — the flex gap is a property of neighbours,
// so neighbours are what the rule reads.
export function adjacentStackedPairs(boxes) {
  const live = boxes.filter(b => b.w > 0 && b.h > 0);
  const sorted = [...live].sort((a, b) => a.y - b.y || a.x - b.x);
  const out = [];
  for (let i = 0; i < sorted.length; i++) {
    for (let j = i + 1; j < sorted.length; j++) {
      const a = sorted[i];
      const b = sorted[j];
      if (boxContains(a, b) || boxContains(b, a)) continue;
      const g = stackedGap(a, b);
      if (!g) continue;
      out.push({
        upper: g.upper.name,
        lower: g.lower.name,
        gap: g.gap,
        upperMb: g.upper.mb || 0,
        lowerMt: g.lower.mt || 0,
      });
      break;
    }
  }
  return out;
}

export function gapViolations(boxes, minGap = GAP_MIN) {
  return stackedPairs(boxes).filter((p) => p.gap < minGap);
}

// The gap-consistency classifier (#796): every ADJACENT stacked pair of
// a flex-column stack sits at `expected` (the dashboard's own row gap)
// plus the pair's own margins — flex adds the gap to whatever the
// margins contribute, so the header's margin-bottom is legitimate while
// a drifted or vanished container gap is not. Violations carry the
// expected distance so the printout names what the page owed.
export function gapConsistencyViolations(boxes, expected, tol = NOISE) {
  return adjacentStackedPairs(boxes)
    .map((p) => ({
      ...p,
      expected: Math.round((expected
        + p.upperMb + p.lowerMt) * 10) / 10,
    }))
    .filter((p) => Math.abs(p.gap - p.expected) > tol);
}

// The picker-underfill classifier (#808): fewer chips rendered than
// the measure row holds, WITH room for the next off-page chip, is the
// fixed-small-page regression. `freeRoom` is pager-left minus the last
// rendered chip's right edge; the chip column gap is subtracted first;
// the boundary is inclusive — a chip that exactly fits is being denied
// a slot. Returns the free room for the violation payload, or null.
export function underfillViolation(freeRoom, minOffPage, rendered,
  measured, gap = 6) {
  if (rendered >= measured) return null;
  if (minOffPage == null) return null;
  const room = freeRoom - gap;
  return room >= minOffPage ? freeRoom : null;
}

// The panel-label rule's classifier (#826): a marked label text whose
// box escapes its panel's box on either side is an overflow. One axis
// of slack for rounding, like every geometry rule here. Violations
// name the side and the distance.
export function labelOverflowViolations(labels, eps = NOISE) {
  const out = [];
  for (const l of labels) {
    const sides = [];
    if (l.label.x < l.box.x - eps) sides.push('left');
    if (l.label.x + l.label.w > l.box.x + l.box.w + eps) sides.push('right');
    for (const side of sides) {
      out.push({
        panel: l.panel,
        side,
        over: Math.round((side === 'left'
          ? l.box.x - l.label.x
          : l.label.x + l.label.w - l.box.x - l.box.w) * 10) / 10,
      });
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
//   section — an effective flex item of .dashboard: a boxed direct
//             child. The self-fetch wrapper is one since the #772 fix
//             gave it display:flex column with its own 14px gap, so the
//             wrapper is the section and the panels inside it are its
//             cards;
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
// effective section of .dashboard holding the mark (a boxed direct
// child; the wrapper is boxed since the #772 fix, so the panels inside
// it stop their climb there): the chart svgs are display:block,
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

// In-page (#796): the flex-column stacks of the page and their effective
// boxed children, with the children's own vertical margins — the raw
// material for gapConsistencyViolations. .dashboard is always the first
// group; every boxed direct child that computes to flex column (the
// self-fetch wrapper since #772) adds one more. The expected distance
// lives on the rule side: it is the dashboard's own row gap for EVERY
// group, whatever the group's own gap says — a wrapper whose gap drifted
// from the dashboard's is exactly the finding (#772's regression shape).
const GAP_CONTAINERS_PROBE = () => {
  const dash = document.querySelector('.dashboard');
  if (!dash) return { error: 'no .dashboard on the page' };
  const isBoxed = (el) => {
    const d = getComputedStyle(el).display;
    return d !== 'inline' && d !== 'contents';
  };
  const effective = (parent) => [...parent.children].flatMap(
    (c) => (isBoxed(c) ? [c] : effective(c)),
  );
  const nm = (el) => (el.className && String(el.className).trim())
    || el.tagName.toLowerCase();
  const boxOf = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return {
      name: nm(el),
      x: r.x, y: r.y, w: r.width, h: r.height,
      mt: parseFloat(s.marginTop) || 0,
      mb: parseFloat(s.marginBottom) || 0,
    };
  };
  const dashGap = parseFloat(getComputedStyle(dash).rowGap) || 0;
  const groups = [{ name: nm(dash), boxes: effective(dash).map(boxOf) }];
  for (const c of effective(dash)) {
    const s = getComputedStyle(c);
    if (s.display === 'flex' && s.flexDirection === 'column') {
      groups.push({ name: nm(c), boxes: effective(c).map(boxOf) });
    }
  }
  return { dashboardGap: dashGap, groups };
};

// Seed for the gap-consistency rule's red proof (#796): shrink the
// flex-column section's own gap to 4px — still above the vertical-gap
// rule's 2px floor, so ONLY the consistency rule reads it — the drift
// shape #772 shipped and #796 pins. A DOM injection on the fresh
// reloaded page; no app change, no route.
async function seedWrapperGap(page) {
  return page.evaluate(() => {
    const dash = document.querySelector('.dashboard');
    if (!dash) return null;
    const isBoxed = (el) => {
      const d = getComputedStyle(el).display;
      return d !== 'inline' && d !== 'contents';
    };
    const wrap = [...dash.children].filter(isBoxed).find((c) => {
      const s = getComputedStyle(c);
      return s.display === 'flex' && s.flexDirection === 'column';
    });
    if (!wrap) return null;
    wrap.style.gap = '4px';
    return {
      wrapper: (wrap.className && String(wrap.className).trim())
        || wrap.tagName.toLowerCase(),
      gap: '4px',
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

// The topmost plot of a multi-chart section (#797): the box with the
// least y (ties keep the first). A section legend pairs against THIS
// plot only — the convention the multichart rule holds is "not above
// the section's charts", and a strip sitting under chart 1 of a stacked
// grid must not fire against chart 2 below it (a union box would).
export function topPlotBox(plots) {
  let top = null;
  for (const p of plots) {
    if (!top || p.y < top.y) top = p;
  }
  return top;
}

// The multichart rule's wiring decision, extracted so the node tests
// can pin it (#797): a SECTION group's legends pair against the
// section's TOPMOST plot only — a strip under chart 1 of a stacked grid
// must not fire against chart 2 below it, which a union-of-plots box
// would make it do. Mark-owned groups pass through unchanged. This is
// the decision a union refactor would silently flip; the between-charts
// pin below fails on exactly that mutant.
export function pairSectionLegendsTop(groups) {
  return groups.map((g) => (g.section
    ? { ...g, plots: [topPlotBox(g.plots)].filter(Boolean) }
    : g));
}

// In-page: group every marked legend and plot under the chart it
// belongs to. A role inside a marked svg belongs to that svg; an html
// legend strip belongs to the nearest ancestor holding EXACTLY ONE
// marked svg — the chart card. An ancestor with two or more marked svgs
// (a grid section of chart cards) owns the strip itself (#797): the
// group is flagged `section` with ALL its plots, and the shared run
// pairs a section legend against the section's TOPMOST plot
// (topPlotBox, on the node side — a function handed to page.evaluate
// closes over nothing from this module), so a legend above the
// section's charts fires while one under chart 1 of a stacked grid
// does not pair against chart 2. A card holding several marked svgs
// keeps its strips unowned (the climb never reaches such an ancestor
// from inside a single-mark card), and a legend with no marked
// ancestor at all pairs with nothing. The browser legs prove
// attribution where it exists (the seeded strips fire) and no finding
// where it does not (the unseeded page is green).
const LEGEND_GROUPS_PROBE = () => {
  const boxOf = (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, w: r.width, h: r.height };
  };
  const SEL = '[data-panel]';
  const nm = (el) => (el.className && String(el.className).trim())
    || el.tagName.toLowerCase();
  const ownerOf = (el) => {
    const inner = el.closest(SEL);
    if (inner) return inner;
    for (let a = el.parentElement; a; a = a.parentElement) {
      const marks = a.querySelectorAll(SEL).length;
      if (marks === 1) return a.querySelector(SEL);
      if (marks > 1) return a;
    }
    return null;
  };
  const groups = new Map();
  const groupOf = (owner) => {
    if (!groups.has(owner)) {
      const isMark = owner.hasAttribute('data-panel');
      const raw = isMark
        ? (owner.matches('[data-role="plot"]')
          ? [owner]
          : [...owner.querySelectorAll('[data-role="plot"]')])
        : [...owner.querySelectorAll('[data-role="plot"]')];
      const plots = raw
        .map((el) => boxOf(el))
        .filter((b) => b.w > 0 && b.h > 0);
      groups.set(owner, {
        panel: isMark
          ? owner.getAttribute('data-panel')
          : `${nm(owner)} (multi-chart section)`,
        section: !isMark,
        plots,
        legends: [],
      });
    }
    return groups.get(owner);
  };
  for (const mark of document.querySelectorAll(SEL)) groupOf(mark);
  for (const el of document.querySelectorAll('[data-role="legend"]')) {
    const owner = ownerOf(el);
    const group = owner && groupOf(owner);
    if (!group) continue;
    const box = boxOf(el);
    if (box.w < 1 || box.h < 1) continue;
    group.legends.push({
      label: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40),
      ...box,
    });
  }
  return [...groups.values()].map((g) => ({
    panel: g.panel, plots: g.plots, legends: g.legends,
    section: g.section || false,
  }));
};

// The shared run of both legend-below rules: the probe is serialized
// into the page, where module bindings do not travel, so the
// topmost-plot pairing for section groups comes from the node side,
// through pairSectionLegendsTop.
const legendBelowRun = async (ctx) => {
  const groups = await ctx.page.evaluate(LEGEND_GROUPS_PROBE);
  return legendBelowViolations(pairSectionLegendsTop(groups));
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

// In-page (#797): every element in the dashboard that is shaped like the
// legend strips but carries no tag — two or more direct-child labels
// holding checkbox colour keys (the one shape every strip in src/ shares
// since #474 made the checkbox the colour key) — with the tagged strips
// and everything inside them excluded. A cluster anywhere in the
// dashboard is legend-shaped enough to measure: the dashboard is the
// chart surface, and the page chrome around it holds no checkboxes.
const UNTAGGED_LEGENDS_PROBE = () => {
  const dash = document.querySelector('.dashboard');
  if (!dash) return { error: 'no .dashboard on the page' };
  const untagged = [];
  for (const el of dash.querySelectorAll('*')) {
    if (el.closest('[data-role="legend"]')) continue;
    const rows = [...el.children]
      .filter((c) => c.tagName === 'LABEL'
        && c.querySelector('input[type="checkbox"]'));
    if (rows.length >= 2) {
      untagged.push({
        name: (el.className && String(el.className).trim())
          || el.tagName.toLowerCase(),
        label: (el.textContent || '').trim().replace(/\s+/g, ' ')
          .slice(0, 40),
      });
    }
  }
  return { untagged };
};

// Seed for the untagged rule's red proof (#797): inject a strip shaped
// like the real ones — checkbox colour-key rows — WITHOUT the tag, into
// a single-chart card. The below-plot rules never see it (no data-role),
// so this seed fires the tag rule alone.
async function seedUntaggedLegend(page) {
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
      for (let i = 0; i < 3; i++) {
        const row = document.createElement('label');
        const key = document.createElement('input');
        key.type = 'checkbox';
        row.appendChild(key);
        row.appendChild(document.createTextNode(`seeded series ${i}`));
        strip.appendChild(row);
      }
      card.insertBefore(strip, card.firstChild);
      return { seeded: mark.getAttribute('data-panel') };
    }
    return null;
  });
}

// Seed for the multichart rule's red proof (#797): inject a TAGGED
// legend strip as the first child of a section holding two or more
// marked panels — above the section's charts, the shape the ownership
// gap left unmeasured. The tag keeps the untagged rule quiet, so this
// seed fires the multichart rule alone.
async function seedMultichartLegendAbovePlot(page) {
  return page.evaluate(() => {
    const dash = document.querySelector('.dashboard');
    if (!dash) return null;
    const isBoxed = (el) => {
      const d = getComputedStyle(el).display;
      return d !== 'inline' && d !== 'contents';
    };
    for (const c of [...dash.children].filter(isBoxed)) {
      const marks = c.querySelectorAll('[data-panel]').length;
      if (marks < 2) continue;
      const strip = document.createElement('div');
      strip.setAttribute('data-role', 'legend');
      strip.textContent = 'seeded section legend';
      strip.style.padding = '6px 14px';
      c.insertBefore(strip, c.firstChild);
      return {
        seeded: (c.className && String(c.className).trim())
          || c.tagName.toLowerCase(),
        marks,
      };
    }
    return null;
  });
}

// --- the project picker's rules (#774) ----------------------------------

// The project picker strip: the one .project-picker that pages projects
// (the range strip shares the class and is #755's). The marker is on the
// component's root; see src/picker.jsx.
const PICKER_SEL = '.project-picker[data-picker="projects"]';

// Seed for the overflow rule's red proof: inject twelve wide chips into
// the strip, the DOM shape of more projects than fit — a DOM injection,
// not an app change. The pager keeps its slot; the injected chips sit
// before it, so the strip holds more content than its width.
async function seedPickerOverflow(page) {
  return page.evaluate((sel) => {
    const strip = document.querySelector(sel);
    if (!strip) return null;
    const pager = strip.querySelector('.pp-pager');
    for (let i = 0; i < 12; i++) {
      const b = document.createElement('button');
      b.className = 'pp-btn pp-proj';
      b.textContent = `seeded-chip-with-a-deliberately-long-name-${i}`;
      strip.insertBefore(b, pager);
    }
    return { injected: 12 };
  }, PICKER_SEL);
}

// Seed for the scrollbar rule's red proof: flip the strip's overflow-x
// to scroll — the DOM change of a picker that permits scrolling again.
// Headless Chromium hides scrollbars (--hide-scrollbars), so no gutter
// ever appears to measure; the property itself is what permits the
// scrolling the ruling forbids, and it is honest to assert directly.
async function seedPickerScrollbar(page) {
  return page.evaluate((sel) => {
    const strip = document.querySelector(sel);
    if (!strip) return null;
    strip.style.overflowX = 'scroll';
    return { overflowX: 'scroll' };
  }, PICKER_SEL);
}

// Seed for the paging-shift rule's red proof: arm a one-shot click
// listener on the next control that grows the strip's padding — the
// DOM shape of a page turn that moves the row. The rule's own page
// turn detonates it.
async function seedPickerPagingShift(page) {
  return page.evaluate((sel) => {
    // The pager only exists where chips do not all fit, and the rule
    // journeys to such a width before it clicks — so the detonator is a
    // capture-phase document listener scoped to THIS strip, armed at
    // whatever width the seed runs at.
    const onTurn = (e) => {
      const btn = e.target.closest('.pp-nav[title="Next page"]');
      if (!btn || !btn.closest('[data-picker="projects"]')) return;
      const strip = document.querySelector(sel);
      // The strip's box includes its padding, so top padding grows the
      // row without wrapping.
      strip.style.paddingTop = '32px';
      document.removeEventListener('click', onTurn, true);
    };
    document.addEventListener('click', onTurn, true);
    return { armed: 'strip grows 32px on next page turn' };
  }, PICKER_SEL);
}

// Seed for the no-shift rule: arm a one-shot resize listener that
// pushes the content below the strip down on the next width change — a
// DOM injection standing in for whatever would wedge space under the
// row. The strip's own box stays constant (a margin is outside it), so
// the SEAT limb of the comparison gets this proof alone. With one seed
// per rule, the no-shift rule's height limb and the paging rule's seat
// limb carry no isolating red proof: the paging seed's padding growth
// proves the PAGING rule's height comparison, not this rule's. The
// rule's interaction (its own resize) detonates it.
async function seedPickerShift(page) {
  return page.evaluate((sel) => {
    const strip = document.querySelector(sel);
    if (!strip) return null;
    const onResize = () => {
      strip.style.marginBottom = '64px';
      window.removeEventListener('resize', onResize);
    };
    window.addEventListener('resize', onResize);
    return { armed: 'content below moves 64px down on next resize' };
  }, PICKER_SEL);
}

// Seed for the after-paging overflow limb (#808): inject nothing up
// front — arm a click listener that pushes two wide chips into the
// strip after every page turn, the shape of a pager whose later pages
// overflow. The rule's own click detonates it.
async function seedPickerOverflowAfterPaging(page) {
  return page.evaluate((sel) => {
    const inject = () => {
      const strip = document.querySelector(sel);
      if (!strip) return;
      const pager = strip.querySelector('.pp-pager');
      for (let i = 0; i < 2; i++) {
        const b = document.createElement('button');
        b.className = 'pp-btn pp-proj';
        b.textContent = `seeded-wide-chip-${i}`;
        if (pager) strip.insertBefore(b, pager);
      }
    };
    const onTurn = (e) => {
      if (!e.target.closest('.pp-nav[title="Next page"]')) return;
      setTimeout(inject, 50);
    };
    document.addEventListener('click', onTurn, true);
    return { armed: 'two wide chips injected after every page turn' };
  }, PICKER_SEL);
}

// Seed for the label rule's red proof (#826): rewrite the first
// marked label with the maintainer's reported label — the exact
// string that overflows its box on the live dashboard. A DOM
// injection; no app change, no route.
async function seedLabelOverflow(page) {
  return page.evaluate(() => {
    const t = document.querySelector('text[data-hbar-label]');
    if (!t) return null;
    t.textContent = 'thinkingmachines/inkling-small:free'
      + ' · Thinking Machines';
    return { seeded: 'long label on the first marked label' };
  });
}

// Seed for the picker's no-shift HEIGHT limb (#808): arm a one-shot
// resize listener that grows the strip's top padding — the padding
// lives INSIDE the strip's box, so the row's height changes while the
// seat below it (measured from the strip's bottom edge) does not.
async function seedPickerShiftHeight(page) {
  return page.evaluate((sel) => {
    const strip = document.querySelector(sel);
    if (!strip) return null;
    const onResize = () => {
      strip.style.paddingTop = '40px';
      window.removeEventListener('resize', onResize);
    };
    window.addEventListener('resize', onResize);
    return { armed: 'strip grows 40px tall on next resize' };
  }, PICKER_SEL);
}

// Seed for the paging SEAT limb (#808): arm a one-shot click listener
// on the next control that grows the strip's margin — the margin is
// OUTSIDE the strip's box, so the content below moves while the row's
// height does not.
async function seedPickerPagingSeat(page) {
  return page.evaluate((sel) => {
    const onTurn = (e) => {
      const btn = e.target.closest('.pp-nav[title="Next page"]');
      if (!btn || !btn.closest('[data-picker="projects"]')) return;
      const strip = document.querySelector(sel);
      strip.style.marginBottom = '48px';
      document.removeEventListener('click', onTurn, true);
    };
    document.addEventListener('click', onTurn, true);
    return { armed: 'content below moves 48px down on next page turn' };
  }, PICKER_SEL);
}

// Seed for the paging POSITION limb (#808): arm a one-shot click
// listener that shifts the strip down. The strip and everything below
// it move by the same amount, so the height and seat comparisons stay
// equal — only the strip's own top catches this shape.
async function seedPickerPagingPosition(page) {
  return page.evaluate((sel) => {
    const onTurn = (e) => {
      const btn = e.target.closest('.pp-nav[title="Next page"]');
      if (!btn || !btn.closest('[data-picker="projects"]')) return;
      const strip = document.querySelector(sel);
      strip.style.position = 'relative';
      strip.style.top = '24px';
      document.removeEventListener('click', onTurn, true);
    };
    document.addEventListener('click', onTurn, true);
    return { armed: 'strip shifts 24px down on next page turn' };
  }, PICKER_SEL);
}

// Seed for the underfill rule's red proof (#808): delete the last
// rendered chip — the fixed-small-page shape, fewer chips than the
// strip has room for — and keep it deleted across refits: the fit
// pass re-renders the slice on resize, so a MutationObserver deletes
// the regrown chip again.
async function seedPickerUnderfill(page) {
  return page.evaluate((sel) => {
    const strip = document.querySelector(sel);
    if (!strip) return null;
    const chipsOf = (s) => [...s.children]
      .filter((c) => c.classList.contains('pp-proj'));
    let keep = chipsOf(strip).length - 1;
    const drop = () => {
      const chips = chipsOf(strip);
      while (chips.length > keep && chips.length) {
        chips[chips.length - 1].remove();
        chips.pop();
      }
    };
    drop();
    new MutationObserver(drop).observe(strip, { childList: true });
    return { deleted: 'one rendered chip, held across refits' };
  }, PICKER_SEL);
}

// Seed for the pinched rules' red proofs (#821): a stylesheet that
// hides the strip's chips survives the re-render a refit causes (the
// rules journey to a pinched width), which a per-node style mutation
// would not.
async function seedPinchedHiddenChips(page) {
  return page.evaluate(() => {
    const style = document.createElement('style');
    style.textContent = '[data-picker="projects"] .pp-proj'
      + ' { display: none !important; }';
    document.head.appendChild(style);
    return { injected: 'chips hidden stylesheet-wide' };
  });
}

// Seed for the pager-present limb (#821): remove the count span and
// keep it removed — the seed runs at the seat width, where every chip
// fits and no pager renders at all, so an observer re-removes the
// count wherever the refit re-renders it, and the pinched journey's
// pager is born missing.
async function seedPinchedPagerMissing(page) {
  return page.evaluate((sel) => {
    const strip = document.querySelector(sel);
    if (!strip) return null;
    const drop = () => {
      for (const c of strip.querySelectorAll('.pp-count')) c.remove();
    };
    drop();
    new MutationObserver(drop).observe(strip,
      { childList: true, subtree: true });
    return { removed: '.pp-count, held across refits' };
  }, PICKER_SEL);
}

// Seed for the pager-sane limb (#821): rewrite the count to NaN after
// every click — the shape a raw-perPage pager would render. The
// rewrite lands after React's own re-render (setTimeout), and the
// rule's page turn detonates it.
async function seedPinchedPagerNaN(page) {
  return page.evaluate((sel) => {
    const rewrite = () => {
      const count = document.querySelector(sel + ' .pp-count');
      if (count) setTimeout(() => {
        count.textContent = 'NaN / NaN';
      }, 50);
    };
    document.addEventListener('click', rewrite, true);
    return { armed: 'count rewritten to NaN after every click' };
  }, PICKER_SEL);
}

// Seed for the pages-complete limb (#821): rewrite the LAST measure
// row chip's text — the expected last project becomes one no page
// shows, so the completeness comparison fails.
async function seedPinchedAbsentLast(page) {
  return page.evaluate((sel) => {
    const chips = document.querySelectorAll(sel + ' .pp-measure .pp-proj');
    if (!chips.length) return null;
    chips[chips.length - 1].textContent = 'seeded-absent-project';
    return { last: 'seeded-absent-project' };
  }, PICKER_SEL);
}

// The in-page underfill probe (#808): rendered chips, the measure
// row's total, the room before the pager, and the narrowest off-page
// chip's width.
const UNDERFILL_PROBE = (sel) => {
  const strip = document.querySelector(sel);
  if (!strip) return { error: 'no picker strip' };
  const widthOf = (el) => el.getBoundingClientRect().width;
  const rendered = [...strip.children]
    .filter((c) => c.classList.contains('pp-proj'));
  const measure = [...strip.querySelectorAll('.pp-measure .pp-proj')];
  const pager = strip.querySelector('.pp-pager');
  const last = rendered[rendered.length - 1];
  let room = null;
  if (last) {
    const lastR = last.getBoundingClientRect();
    room = pager
      ? pager.getBoundingClientRect().left - lastR.right
      : strip.getBoundingClientRect().right - lastR.right;
  }
  // The fit is order-preserving: the chip being denied a slot is the
  // NEXT one in list order, not the narrowest off-page chip.
  const next = measure[rendered.length];
  return {
    rendered: rendered.length,
    measured: measure.length,
    room: room == null ? null : Math.round(room * 10) / 10,
    nextOff: next ? Math.round(widthOf(next) * 10) / 10 : null,
  };
};

// The in-page label-overflow probe (#826): every marked label text of
// every panel svg, beside its svg's own box.
const LABEL_FITS_PROBE = () => {
  const labels = [];
  for (const svg of document.querySelectorAll('svg[data-panel]')) {
    const b = svg.getBoundingClientRect();
    const panel = svg.getAttribute('data-panel');
    for (const t of svg.querySelectorAll('text[data-hbar-label]')) {
      const r = t.getBoundingClientRect();
      if (r.width < 1) continue;
      labels.push({
        panel,
        label: { x: r.x, y: r.y, w: r.width, h: r.height },
        box: { x: b.x, y: b.y, w: b.width, h: b.height },
      });
    }
  }
  return { labels };
};

// The shift rules' shared geometry probe: the strip's height, its top,
// and its seat (the next sibling's top minus the strip's bottom).
const PICKER_GEOMETRY_PROBE = (sel) => {
  const strip = document.querySelector(sel);
  if (!strip) return null;
  const s = strip.getBoundingClientRect();
  const b = strip.nextElementSibling
    ? strip.nextElementSibling.getBoundingClientRect()
    : null;
  return {
    h: Math.round(s.height * 10) / 10,
    top: Math.round(s.y * 10) / 10,
    below: !!b,
    seat: b ? Math.round((b.y - (s.y + s.height)) * 10) / 10 : 0,
    count: strip.querySelector('.pp-count')
      ? strip.querySelector('.pp-count').textContent : null,
  };
};

// The shift rules' journey: to a width the chips cannot all fit (the
// pager renders; a same-width resize fires nothing) and back.
const journeyNarrow = async (ctx) => {
  const narrowW = Math.max(320, Math.round(ctx.width * 0.6));
  const target = narrowW === ctx.width ? ctx.width + 160 : narrowW;
  await ctx.page.setViewportSize({ width: target, height: 900 });
  await ctx.page.waitForTimeout(400);
};
const journeyBack = async (ctx) => {
  await ctx.page.setViewportSize({ width: ctx.width, height: 900 });
  await ctx.page.waitForTimeout(400);
};

// The pinched rules' journey: to a width no chip fits (the fit floors
// at one), where the picker pages one project at a time (#821).
const journeyPinched = async (ctx) => {
  await ctx.page.setViewportSize({ width: 220, height: 900 });
  await ctx.page.waitForTimeout(500);
};

// A pinched run measures at 220px, and the viewport is SHARED runner
// state: every pinched run restores the rule's own width on the way
// out, or every later rule measures the wrong width under its own
// name (delta-review blocker on 456b467e).
const pinchedRun = (run) => async (ctx) => {
  try {
    return await run(ctx);
  } finally {
    await journeyBack(ctx);
  }
};

const NEXT_SEL = '.pp-nav[title="Next page"]';

const PREV_SEL = '.pp-nav[title="Previous page"]';

// The paging rules' shared click, guarded: a click on a disabled Next
// hangs Playwright for its whole 30s timeout, and the shared unseeded
// page accumulates paging state across rules — so every paging rule
// pages back to 1 on the way out (backToFirst).
const clickNextGuarded = async (page, width) => {
  const nextSel = 'main .project-picker[data-picker="projects"]'
    + ' ' + NEXT_SEL;
  const enabled = await page.$eval(nextSel,
    (el) => !el.disabled).catch(() => false);
  if (!enabled) {
    return [{ upper: 'a pager with pages at the journey width',
      lower: 'next is disabled — nothing pages', gap: 0 }];
  }
  await page.click(nextSel);
  await page.waitForTimeout(300);
  return [];
};

const backToFirst = async (page) => {
  const prevSel = 'main .project-picker[data-picker="projects"]'
    + ' ' + PREV_SEL;
  for (let i = 0; i < 10; i++) {
    const enabled = await page.$eval(prevSel,
      (el) => !el.disabled).catch(() => false);
    if (!enabled) return;
    await page.click(prevSel);
    await page.waitForTimeout(200);
  }
};

// The unseeded loop runs every rule on ONE shared page per width:
// a rule that changes runner state (viewport size, the picker's page)
// restores it on the way out — the shift rules page back to first,
// the pinched rules restore the width, and nothing relies on rule
// ORDER to be safe (the old keep-LAST comment did; order-dependent
// safety is what the #808 split broke).
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
    id: 'section-gap-consistency',
    description: 'every flex-column stack (the dashboard itself and its'
      + ' flex-column sections) sits at the dashboard\'s own row gap,'
      + ' plus each pair\'s own margins — a section gap that drifts'
      + ' from it is a finding (issue #796)',
    seed: seedWrapperGap,
    run: async (ctx) => {
      const { dashboardGap, groups } = await ctx.page
        .evaluate(GAP_CONTAINERS_PROBE);
      const out = [];
      for (const g of groups) {
        out.push(...gapConsistencyViolations(g.boxes, dashboardGap));
      }
      return out;
    },
  },
  {
    id: 'legend-below-plot',
    description: 'every legend renders below every plot of its own chart'
      + ' — the dashboard-wide legend convention (issue #773)',
    seed: seedLegendAbovePlot,
    run: legendBelowRun,
  },
  {
    id: 'legend-below-plot-multichart',
    description: 'a legend in a multi-chart section renders below the'
      + ' section\'s topmost plot — the same convention, measured where'
      + ' single-chart attribution now finds an owner (issue #797)',
    seed: seedMultichartLegendAbovePlot,
    run: legendBelowRun,
  },
  {
    id: 'legend-strips-tagged',
    description: 'every legend-shaped strip (checkbox colour-key rows)'
      + ' inside the dashboard carries data-role="legend", so the'
      + ' below-plot rules can measure it (issue #797)',
    seed: seedUntaggedLegend,
    run: async (ctx) => {
      const { untagged } = await ctx.page.evaluate(UNTAGGED_LEGENDS_PROBE);
      return untagged.map((u) => ({
        upper: `untagged legend "${u.label}"`,
        lower: 'legend strip carries no data-role="legend" tag',
        gap: 0,
      }));
    },
  },
  {
    id: 'project-picker-overflow',
    description: 'the project picker holds no horizontal overflow and'
      + ' no scrollbar: only chips that fit the strip render (#774)',
    seed: seedPickerOverflow,
    run: async (ctx) => {
      const over = await ctx.page.evaluate((sel) => {
        const strip = document.querySelector(sel);
        return strip ? strip.scrollWidth - strip.clientWidth : null;
      }, PICKER_SEL);
      if (over === null) {
        return [{ upper: 'project picker present', lower: 'strip missing',
          gap: 0 }];
      }
      if (over > 0) {
        return [{ upper: 'picker strip fits its width',
          lower: 'rendered content wider than the strip', gap: over }];
      }
      return [];
    },
  },
  {
    id: 'project-picker-overflow-after-paging',
    description: 'the picker holds no overflow on later pages either:'
      + ' a page turn may not push rendered content past the strip'
      + ' (#808)',
    seed: seedPickerOverflowAfterPaging,
    run: async (ctx) => {
      await journeyNarrow(ctx);
      const clickOut = await clickNextGuarded(ctx.page, ctx.width);
      if (clickOut.length) return clickOut;
      const over = await ctx.page.evaluate((sel) => {
        const strip = document.querySelector(sel);
        return strip ? strip.scrollWidth - strip.clientWidth : null;
      }, PICKER_SEL);
      if (over === null) {
        return [{ upper: 'project picker present', lower: 'strip missing',
          gap: 0 }];
      }
      if (over > 0) {
        await backToFirst(ctx.page);
        return [{ upper: 'picker strip fits its width after paging',
          lower: 'rendered content wider than the strip on page 2+',
          gap: over }];
      }
      await backToFirst(ctx.page);
      return [];
    },
  },
  {
    id: 'project-picker-scrollbar',
    description: 'the project picker cuts overflow off: overflow-x is'
      + ' neither auto nor scroll, so nothing may scroll it (#774)',
    seed: seedPickerScrollbar,
    run: async (ctx) => {
      const v = await ctx.page.evaluate((sel) => {
        const strip = document.querySelector(sel);
        return strip ? getComputedStyle(strip).overflowX : null;
      }, PICKER_SEL);
      if (v === null) {
        return [{ upper: 'project picker present', lower: 'strip missing',
          gap: 0 }];
      }
      if (v === 'auto' || v === 'scroll') {
        return [{ upper: 'picker strip with overflow-x cut off',
          lower: `overflow-x: ${v} permits scrolling`, gap: 0 }];
      }
      return [];
    },
  },
  {
    id: 'project-picker-no-shift-height',
    description: "the picker's row keeps its height while the width"
      + ' re-fits — the height limb, proven by its own seed (#774,'
      + ' #808)',
    seed: seedPickerShiftHeight,
    run: async (ctx) => {
      const base = await ctx.page.evaluate(
        PICKER_GEOMETRY_PROBE, PICKER_SEL);
      if (!base) {
        return [{ upper: 'project picker present', lower: 'strip missing',
          gap: 0 }];
      }
      const out = [];
      await journeyNarrow(ctx);
      const now = await ctx.page.evaluate(
        PICKER_GEOMETRY_PROBE, PICKER_SEL);
      if (now && now.h !== base.h) {
        out.push({ upper: `picker row height ${base.h}px`,
          lower: `narrow-width height ${now.h}px`, gap: now.h - base.h });
      }
      await journeyBack(ctx);
      const back = await ctx.page.evaluate(
        PICKER_GEOMETRY_PROBE, PICKER_SEL);
      if (back && back.h !== base.h) {
        out.push({ upper: `picker row height ${base.h}px`,
          lower: `after the refit round-trip height ${back.h}px`,
          gap: back.h - base.h });
      }
      return out;
    },
  },
  {
    id: 'project-picker-no-shift-seat',
    description: "the content below the picker keeps its seat while the"
      + ' width re-fits — the seat limb, proven by its own seed (#774,'
      + ' #808)',
    seed: seedPickerShift,
    run: async (ctx) => {
      const base = await ctx.page.evaluate(
        PICKER_GEOMETRY_PROBE, PICKER_SEL);
      if (!base) {
        return [{ upper: 'project picker present', lower: 'strip missing',
          gap: 0 }];
      }
      if (!base.below) {
        return [{ upper: 'content below the picker',
          lower: 'nothing below the strip to seat', gap: 0 }];
      }
      const out = [];
      await journeyNarrow(ctx);
      const now = await ctx.page.evaluate(
        PICKER_GEOMETRY_PROBE, PICKER_SEL);
      if (now && now.below && now.seat !== base.seat) {
        out.push({ upper: `seat gap below the picker ${base.seat}px`,
          lower: `narrow-width seat gap ${now.seat}px`,
          gap: now.seat - base.seat });
      }
      await journeyBack(ctx);
      const back = await ctx.page.evaluate(
        PICKER_GEOMETRY_PROBE, PICKER_SEL);
      if (back && back.below && back.seat !== base.seat) {
        out.push({ upper: `seat gap below the picker ${base.seat}px`,
          lower: `after the refit round-trip seat gap ${back.seat}px`,
          gap: back.seat - base.seat });
      }
      return out;
    },
  },
  {
    id: 'project-picker-no-shift-paging-height',
    description: "the page really turns and the picker's row keeps its"
      + ' height while paging — the height limb, proven by its own'
      + ' seed (#774, #808)',
    seed: seedPickerPagingShift,
    run: async (ctx) => {
      await journeyNarrow(ctx);
      const seated = await ctx.page.evaluate(
        PICKER_GEOMETRY_PROBE, PICKER_SEL);
      if (!seated) {
        return [{ upper: 'project picker present', lower: 'strip missing',
          gap: 0 }];
      }
      if (!seated.count) {
        return [{ upper: 'a pager at a width chips do not all fit',
          lower: 'no pager ever renders — nothing pages', gap: 0 }];
      }
      const clickOut = await clickNextGuarded(ctx.page, ctx.width);
      if (clickOut.length) return clickOut;
      const turned = await ctx.page.evaluate(
        PICKER_GEOMETRY_PROBE, PICKER_SEL);
      const out = [];
      if (turned.count === seated.count) {
        out.push({ upper: 'the page turns on next',
          lower: `counter stuck at ${turned.count}`, gap: 0 });
      }
      if (turned.h !== seated.h) {
        out.push({ upper: `picker row height ${seated.h}px`,
          lower: `after paging height ${turned.h}px`,
          gap: turned.h - seated.h });
      }
      await backToFirst(ctx.page);
      return out;
    },
  },
  {
    id: 'project-picker-no-shift-paging-seat',
    description: "the content below the picker keeps its seat while the"
      + ' pager turns a page — the seat limb, proven by its own seed'
      + ' (#774, #808)',
    seed: seedPickerPagingSeat,
    run: async (ctx) => {
      await journeyNarrow(ctx);
      const seated = await ctx.page.evaluate(
        PICKER_GEOMETRY_PROBE, PICKER_SEL);
      if (!seated || !seated.below) {
        return [{ upper: 'content below the picker',
          lower: 'nothing below the strip to seat', gap: 0 }];
      }
      const clickOut = await clickNextGuarded(ctx.page, ctx.width);
      if (clickOut.length) return clickOut;
      const turned = await ctx.page.evaluate(
        PICKER_GEOMETRY_PROBE, PICKER_SEL);
      const out = [];
      if (turned.below && turned.seat !== seated.seat) {
        out.push({ upper: `seat gap below the picker ${seated.seat}px`,
          lower: `after paging seat gap ${turned.seat}px`,
          gap: turned.seat - seated.seat });
      }
      await backToFirst(ctx.page);
      return out;
    },
  },
  {
    id: 'project-picker-no-shift-paging-position',
    description: "the strip keeps its position while the pager turns a"
      + ' page: a move that drags everything below it by the same'
      + ' amount is still a move (#808)',
    seed: seedPickerPagingPosition,
    run: async (ctx) => {
      await journeyNarrow(ctx);
      const seated = await ctx.page.evaluate(
        PICKER_GEOMETRY_PROBE, PICKER_SEL);
      if (!seated) {
        return [{ upper: 'project picker present', lower: 'strip missing',
          gap: 0 }];
      }
      const clickOut = await clickNextGuarded(ctx.page, ctx.width);
      if (clickOut.length) return clickOut;
      const turned = await ctx.page.evaluate(
        PICKER_GEOMETRY_PROBE, PICKER_SEL);
      const out = [];
      if (turned.top !== seated.top) {
        out.push({ upper: `strip top ${seated.top}px before the turn`,
          lower: `strip top ${turned.top}px after the turn`,
          gap: turned.top - seated.top });
      }
      await backToFirst(ctx.page);
      return out;
    },
  },
  {
    id: 'project-picker-underfill',
    description: 'the picker shows every chip that fits: fewer chips'
      + ' than the strip has room for — at load or after a widening'
      + ' re-fit — is the fixed-small-page regression (#808)',
    seed: seedPickerUnderfill,
    run: async (ctx) => {
      const out = [];
      const probeOnce = async (tag) => {
        const p = await ctx.page.evaluate(UNDERFILL_PROBE, PICKER_SEL);
        if (p.error) {
          out.push({ upper: 'project picker present',
            lower: 'strip missing', gap: 0 });
          return;
        }
        if (p.rendered === 0 && p.measured > 0) {
          out.push({ upper: `picker fills its width (${tag})`,
            lower: 'no chip renders though the measure row holds'
              + ` ${p.measured}`, gap: 0 });
          return;
        }
        const v = underfillViolation(
          p.room, p.nextOff, p.rendered, p.measured, 6);
        if (v != null) {
          out.push({ upper: `picker fills its width (${tag})`,
            lower: `room for the next chip (${p.nextOff}px) left`
              + ` unused (${v}px free)`, gap: v });
        }
      };
      await probeOnce('at load');
      await ctx.page.setViewportSize(
        { width: ctx.width + 240, height: 900 });
      await ctx.page.waitForTimeout(500);
      await probeOnce('after widening');
      await ctx.page.setViewportSize(
        { width: ctx.width, height: 900 });
      await ctx.page.waitForTimeout(400);
      return out;
    },
  },
  {
    id: 'project-picker-pinched-nonempty',
    description: 'at a width no chip fits, the one-chip page floor'
      + ' renders a chip on every page: no page is empty (#821)',
    seed: seedPinchedHiddenChips,
    run: pinchedRun(async (ctx) => {
      await journeyPinched(ctx);
      const out = [];
      const probe = () => ctx.page.evaluate((sel) => {
        const strip = document.querySelector(sel);
        if (!strip) return null;
        const chips = [...strip.children]
          .filter((c) => c.classList.contains('pp-proj'))
          .filter((c) => {
            const r = c.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          });
        const count = strip.querySelector('.pp-count');
        return { chips: chips.length,
          count: count ? count.textContent : null };
      }, PICKER_SEL);
      for (let page = 1; page <= 40; page++) {
        const p = await probe();
        if (!p) {
          out.push({ upper: 'project picker present',
            lower: 'strip missing', gap: 0 });
          break;
        }
        if (p.chips < 1) {
          out.push({ upper: `page ${page} of the pinched picker`,
            lower: 'renders no chip — the one-chip floor is not'
              + ' holding', gap: 0 });
          break;
        }
        const nextSel = 'main .project-picker[data-picker="projects"]'
          + ' ' + NEXT_SEL;
        const enabled = await ctx.page.$eval(nextSel,
          (el) => !el.disabled).catch(() => false);
        if (!enabled) break;
        await ctx.page.click(nextSel);
        await ctx.page.waitForTimeout(250);
      }
      return out;
    }),
  },
  {
    id: 'project-picker-pinched-pager-present',
    description: 'the pinched picker still shows its pager: the count'
      + ' span renders a page/total reading (#821)',
    seed: seedPinchedPagerMissing,
    run: pinchedRun(async (ctx) => {
      await journeyPinched(ctx);
      const text = await ctx.page.evaluate((sel) => {
        const strip = document.querySelector(sel);
        if (!strip) return null;
        const count = strip.querySelector('.pp-count');
        return count ? count.textContent : null;
      }, PICKER_SEL);
      if (text === null || !/^\d+ \/ \d+$/.test(text)) {
        return [{ upper: 'the pinched picker pager',
          lower: `count missing or unreadable (${JSON.stringify(text)})`,
          gap: 0 }];
      }
      return [];
    }),
  },
  {
    id: 'project-picker-pinched-pager-sane',
    description: 'the pager count stays two integers through a page'
      + ' turn: a raw-perPage pager renders NaN (#821)',
    seed: seedPinchedPagerNaN,
    run: pinchedRun(async (ctx) => {
      await journeyPinched(ctx);
      const read = () => ctx.page.evaluate((sel) => {
        const strip = document.querySelector(sel);
        if (!strip) return null;
        const count = strip.querySelector('.pp-count');
        return count ? count.textContent : null;
      }, PICKER_SEL);
      const out = [];
      const first = await read();
      if (first === null || !/^\d+ \/ \d+$/.test(first)) {
        out.push({ upper: 'the pinched picker pager before the turn',
          lower: `count not two integers (${JSON.stringify(first)})`,
          gap: 0 });
      }
      const nextSel = 'main .project-picker[data-picker="projects"]'
        + ' ' + NEXT_SEL;
      const enabled = await ctx.page.$eval(nextSel,
        (el) => !el.disabled).catch(() => false);
      if (enabled) {
        await ctx.page.click(nextSel);
        await ctx.page.waitForTimeout(400);
        const after = await read();
        if (after === null || !/^\d+ \/ \d+$/.test(after)) {
          out.push({ upper: 'the pinched picker pager after a turn',
            lower: `count not two integers (${JSON.stringify(after)})`,
            gap: 0 });
        }
      }
      return out;
    }),
  },
  {
    id: 'project-picker-pinched-complete',
    description: 'the pinched pages together show every project: the'
      + ' last page renders the last project (#821)',
    seed: seedPinchedAbsentLast,
    run: pinchedRun(async (ctx) => {
      await journeyPinched(ctx);
      const expected = await ctx.page.evaluate((sel) => {
        const chips = document.querySelectorAll(
          sel + ' .pp-measure .pp-proj');
        return chips.length
          ? chips[chips.length - 1].textContent : null;
      }, PICKER_SEL);
      if (!expected) {
        return [{ upper: 'the measure row', lower: 'holds no chips',
          gap: 0 }];
      }
      const nextSel = 'main .project-picker[data-picker="projects"]'
        + ' ' + NEXT_SEL;
      for (let i = 0; i < 40; i++) {
        const enabled = await ctx.page.$eval(nextSel,
          (el) => !el.disabled).catch(() => false);
        if (!enabled) break;
        await ctx.page.click(nextSel);
        await ctx.page.waitForTimeout(250);
      }
      const shown = await ctx.page.evaluate((sel) => {
        const strip = document.querySelector(sel);
        if (!strip) return null;
        return [...strip.children]
          .filter((c) => c.classList.contains('pp-proj'))
          .map((c) => c.textContent);
      }, PICKER_SEL);
      if (!shown || !shown.includes(expected)) {
        return [{ upper: 'the last pinched page',
          lower: `does not show the last project (${JSON.stringify(expected)})`,
          gap: 0 }];
      }
      return [];
    }),
  },
  {
    id: 'panel-label-fits',
    description: 'every marked panel label text stays inside its'
      + " panel's box, either by fitting or by the panel clipping it"
      + ' with the full text on record (#826)',
    seed: seedLabelOverflow,
    run: async (ctx) => {
      const { labels } = await ctx.page.evaluate(LABEL_FITS_PROBE);
      return labelOverflowViolations(labels).map((v) => ({
        upper: `${v.panel}: label escapes ${v.side} by ${v.over}px`,
        lower: 'label text must fit or clip inside its panel',
        gap: v.over,
      }));
    },
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
