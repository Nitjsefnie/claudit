// The project picker's fit arithmetic and strip read (#774).
//
// The picker shows only the chips that fit the strip's current width and
// pages the rest, so the page size is measured, not fixed. This module
// holds the numbers: `fitCount` is the pure arithmetic, `computeFit`
// reads the rendered strip and maps it onto those inputs. The React glue
// lives in app.jsx's ProjectPicker; the rendered guard
// (scripts/ci/panel_layout_rules.mjs) proves the behavior in a browser.
//
// Plain JS, no React: node drives this file (tests/test_picker_fit_js.py).
(function (global) {
  'use strict';

  // How many of `widths` fit into `avail`, charging `gap` between chips
  // and consuming `reserve` first. A chip that lands exactly on the
  // remaining room fits; one pixel over does not (`>`, never `>=` — a
  // `>=` here drops exact fits and leaves a dead margin every load).
  function fitCount(avail, widths, gap, reserve) {
    let used = reserve;
    let n = 0;
    for (let i = 0; i < widths.length; i++) {
      const step = (n > 0 ? gap : 0) + widths[i];
      if (used + step > avail) break;
      used += step;
      n++;
    }
    return n;
  }

  // Read the strip and return its page size: how many project chips fit
  // beside the All chip and the reserves. Returns 0..len; the component
  // applies the number as its page size.
  //
  // Chip widths come from the hidden measure row (.pp-measure), not the
  // real chips: the real strip renders only the fitted slice, so its own
  // widths would shrink the fit to itself — a resize wider could never
  // grow the page back.
  //
  // The pager reserve is the RENDERED pager's nav buttons + count, jump
  // excluded. The jump is reserved from the measure row's replica
  // whenever the replica exists (a project is selected): if the reserve
  // swung with whether the current page shows the selected project, the
  // fit would oscillate between jump-shown and jump-hidden states.
  function computeFit(strip) {
    if (!strip || !strip.clientWidth) return null;
    const cs = document.getComputedStyle(strip);
    const padL = parseFloat(cs.paddingLeft) || 0;
    const padR = parseFloat(cs.paddingRight) || 0;
    const gap = parseFloat(cs.columnGap) || 0;
    const inner = strip.clientWidth - padL - padR;

    const layer = strip.querySelector('.pp-measure');
    const widths = layer
      ? Array.from(layer.querySelectorAll('.pp-proj')).map(el => el.offsetWidth)
      : [];
    const len = widths.length;
    const jumpEl = layer ? layer.querySelector('.pp-jump') : null;
    const jumpW = jumpEl ? jumpEl.offsetWidth + gap : 0;

    const allChip = strip.querySelector('.pp-all');
    const allW = allChip ? allChip.offsetWidth : 0;

    const room0 = Math.max(0, inner - allW - gap);
    if (fitCount(room0, widths, gap, 0) >= len) return len; // no pager ever

    // A pager will render; reserve its core as rendered (jump excluded).
    const pager = strip.querySelector('.pp-pager');
    let core = 0;
    if (pager) {
      pager.querySelectorAll('.pp-nav, .pp-count').forEach(el => { core += el.offsetWidth; });
      core += 2 * gap;
    }
    return fitCount(room0, widths, gap, core + jumpW);
  }

  const api = { fitCount, computeFit };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  /* eslint-disable-next-line no-param-reassign */
  global.pickerFit = api;
})(typeof window !== 'undefined' ? window : globalThis);
