(function (global) {
  'use strict';

  function plan(width, n) {
    const room = Math.max(0, width - Math.min(1, width / 2));
    const gaps = n.core ? 2 : n.chip ? 1 : 0;
    const jumpMin = Math.min(n.jump, n.jumpMin);
    const fixed = n.all + n.core + n.chip + gaps * n.gap;
    const minimum = fixed + (n.jump ? jumpMin + n.gap : 0);
    const scale = Math.min(1, room / Math.max(minimum, 1));
    const padding = Math.min(n.padding, Math.max(0, (room - minimum) / 2));
    const jump = n.jump ? Math.min(n.jump,
      Math.max(jumpMin, (room - 2 * padding) / scale - fixed - n.gap)) : 0;
    return { scale, padding, gap: n.gap * scale, jump };
  }

  const widthOf = (el) => el ? el.getBoundingClientRect().width : 0;
  const number = (style, key) => parseFloat(style[key]) || 0;

  function fit(strip) {
    const set = (key, value) => strip.style.setProperty('--pp-' + key, value);
    // Reset and restore synchronously: natural metrics never inherit the
    // previous width's compaction, and no intermediate layout is painted.
    set('scale', '1');
    set('padding', '0px');
    strip.removeAttribute('data-pp-compact');
    set('gap', '6px');
    set('jump-width', '260px');
    const style = window.getComputedStyle(strip);
    const gap = number(style, 'columnGap');
    const padding = parseFloat(style.getPropertyValue('--pp-padding-max')) || 0;
    const layer = strip.querySelector('.pp-measure');
    const chips = [...layer.querySelectorAll('.pp-proj')];
    const widths = chips.map(widthOf);
    const allEl = strip.querySelector('.pp-all');
    const all = widthOf(allEl);
    const paged = widths.length > 1 && all + widths.reduce((a, b) => a + b, 0)
      + gap * widths.length > strip.clientWidth - 2 * padding - 1;
    const coreEl = layer.querySelector('.pp-core');
    const core = paged ? widthOf(coreEl) : 0;
    const jumpEl = paged && layer.querySelector('.pp-jump');
    const jumpStyle = jumpEl ? window.getComputedStyle(jumpEl) : {};
    const chipStyle = chips.length ? window.getComputedStyle(chips[0]) : {};
    const sizes = plan(strip.clientWidth, {
      all, core, gap, padding, jump: widthOf(jumpEl),
      jumpMin: number(jumpStyle, 'paddingLeft') + number(jumpStyle, 'paddingRight')
        + number(jumpStyle, 'borderLeftWidth') + number(jumpStyle, 'borderRightWidth')
        + number(jumpStyle, 'fontSize'),
      chip: number(chipStyle, 'fontSize') + number(chipStyle, 'borderLeftWidth')
        + number(chipStyle, 'borderRightWidth'),
    });
    set('padding', sizes.padding + 'px');
    set('jump-width', sizes.jump + 'px');
    const applyScale = (scale) => {
      set('scale', String(scale));
      set('gap', gap * scale + 'px');
      strip.toggleAttribute('data-pp-compact', scale < 1);
    };
    applyScale(sizes.scale);
    // Zoomed text/borders round nonlinearly at very small sizes. Verify
    // the real boxes, using only page-independent measurement replicas.
    const room = strip.clientWidth - 2 * sizes.padding
      - Math.min(1, strip.clientWidth / 2);
    const minimum = (scale) => widthOf(allEl)
      + (paged ? widthOf(coreEl) + 2 * gap * scale : chips.length ? gap * scale : 0)
      + (jumpEl ? widthOf(jumpEl) + gap * scale : 0)
      + (chips.length ? number(chipStyle, 'fontSize') * scale : 0);
    if (minimum(sizes.scale) > room) {
      let lo = 0, hi = sizes.scale;
      for (let i = 0; i < 16; i++) {
        const mid = (lo + hi) / 2;
        applyScale(mid);
        if (minimum(mid) <= room) lo = mid;
        else hi = mid;
      }
      applyScale(lo);
    }
    return { paged };
  }

  global.pickerSpace = { plan, fit };
})(typeof window !== 'undefined' ? window : globalThis);
