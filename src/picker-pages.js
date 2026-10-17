(function (global) {
  'use strict';

  const SLACK = 1;
  const widthOf = (el) => el.getBoundingClientRect().width;

  function packPages(widths, available, gap, reserve, paged = false) {
    const fitCount = global.pickerFit.fitCount;
    if (!widths.length) return [];
    if (!paged && fitCount(available, widths, gap, 0) >= widths.length) {
      return [widths.length];
    }
    const ends = [];
    for (let start = 0; start < widths.length;) {
      const count = fitCount(available, widths.slice(start), gap, reserve);
      start += Math.max(1, count);
      ends.push(count ? start : -start);
    }
    return ends;
  }

  function pageForIndex(ends, index) {
    return ends
      ? ends.findIndex((end) => index < Math.abs(end))
      : 0;
  }

  function computePages(strip) {
    if (!strip || !strip.clientWidth) return null;
    const space = global.pickerSpace?.fit(strip);
    const cs = window.getComputedStyle(strip);
    const padL = parseFloat(cs.paddingLeft) || 0;
    const padR = parseFloat(cs.paddingRight) || 0;
    const gap = parseFloat(cs.columnGap) || 0;
    const inner = strip.clientWidth - padL - padR - SLACK;
    const layer = strip.querySelector('.pp-measure');
    const widths = layer
      ? Array.from(layer.querySelectorAll('.pp-proj')).map(widthOf)
      : [];
    const all = strip.querySelector('.pp-all');
    const room = Math.max(0, inner - (all ? widthOf(all) : 0) - gap);
    const pager = strip.querySelector('.pp-pager')
      || (space?.paged && layer.querySelector('.pp-core'));
    let reserve = 0;
    if (pager) {
      pager.querySelectorAll('.pp-nav, .pp-count, .pp-core-count')
        .forEach((el) => { reserve += widthOf(el); });
      reserve += 3 * gap;
    }
    const jump = layer && layer.querySelector('.pp-jump');
    if (jump) reserve += widthOf(jump) + gap;
    return packPages(widths, room, gap, reserve, space?.paged);
  }

  global.pickerPages = { packPages, pageForIndex, computePages };
})(typeof window !== 'undefined' ? window : globalThis);
