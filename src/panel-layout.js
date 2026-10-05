// Panel layout maths, shared by the chart panels and read by nothing else.
//
// Two problems, both of which a panel gets wrong by drawing at offsets
// derived from anything but the text it is drawing:
//
//   * a legend laid out on a FIXED cluster pitch overruns its own entry
//     as soon as one label is wider than the pitch assumed, and its
//     wrapped last row falls off the bottom of the svg, which clips it;
//   * a title laid out at a fixed x runs off the panel edge at phone
//     width, because the panel width is measured and the title's is not.
//
// Both are here as pure functions of (text, advance, available width) so
// the arithmetic is testable from node without a browser: the suite
// cannot render, and a pure function is the part that can be pinned.
//
// The advance is a MEASURED input, never a predicted one. These panels
// are monospace and the rendered advance is wider than fontSize * 0.6 —
// issue #630 measured 6.38px at fontSize 9.5 — so the caller measures it
// once from a real rendered label and every width here is that times a
// character count.
(function () {
  const ELLIPSIS = '…';

  // The longest prefix of `text` that fits `avail` px at `adv` px per
  // character, with an ellipsis when anything was dropped. Returns the
  // text unchanged when it already fits — the common case, and the one
  // that must never gain an ellipsis.
  function fitText(text, adv, avail) {
    const str = String(text == null ? '' : text);
    if (!(adv > 0) || !(avail > 0)) return str;
    if (str.length * adv <= avail) return str;
    // One character is spent on the ellipsis, so the prefix gets the rest.
    const keep = Math.floor(avail / adv) - 1;
    if (keep < 1) return ELLIPSIS;
    return str.slice(0, keep) + ELLIPSIS;
  }

  // One legend cluster: swatch, label, rule, count — all offsets from the
  // cluster's own origin. Derived from the label's width, not fixed: at
  // the fixed offsets (rule at 86, count at 108) a 17-character model name
  // rendered 108.5px wide and ran 9.5px into the count label beside it and
  // straight through the rule, so the entry read as one run-together word.
  //
  // `maxWidth` shortens the two texts until the cluster fits the width it
  // has to live in, label first — it is the wider of the two and the one
  // whose tail carries the least. A cluster that still will not fit keeps
  // its own row (see packLegend) rather than being dropped: an entry the
  // reader cannot see is worse than one they can.
  function clusterAt(label, count, adv, maxWidth) {
    const head = 9 + 8;          // label x, then the gap before the rule
    const tail = 16 + 6;          // the rule, then the gap before the count
    // head/tail are the FIXED parts of a cluster's width; everything
    // between them is the label's own measured length.
    //
    // `label` comes back UNCHANGED and `text` holds what is drawn. The two
    // are not interchangeable: the panel keys its React children and looks
    // the model's colour up by `label`, so a cluster that returned the
    // shortened string as its identity would take a #888 fallback swatch
    // beside its own model-coloured median line, and two sibling models
    // that shorten alike would collide as duplicate keys (issue #630).
    let text = String(label == null ? '' : label);
    let countText = `median (${Number(count).toLocaleString()} files)`;
    const limit = maxWidth > 0 ? maxWidth : Infinity;
    if (head + text.length * adv + tail + countText.length * adv > limit) {
      // Characters, not px: `adv` is a WIDTH per character, so a budget in
      // px has to be divided by it before it can be compared with a
      // string's length. Taking the two as like quantities sizes the
      // cluster off the room available rather than off the font.
      const spare = limit - head - tail;
      const countChars = Math.max(1, Math.floor(spare / adv / 2));
      countText = fitText(countText, adv, countChars * adv);
      text = fitText(text, adv,
        Math.max(1, Math.floor(spare / adv) - countText.length) * adv);
    }
    const ruleX = 9 + text.length * adv + 8;
    const countX = ruleX + tail;
    return {
      label, text, count, countText, ruleX, countX,
      width: countX + countText.length * adv,
    };
  }

  // Lay `items` ([{model, count}]) out left to right across `avail` px,
  // wrapping to a new row when the next cluster does not fit. Returns
  // {rows, width, height}: `rows` is an array of arrays of placed clusters
  // carrying x, y relative to the legend's own origin, the cluster's width
  // and the offsets clusterAt computed for it.
  //
  // The row count drives the svg's height, which is the point: the wrapped
  // row is what the fixed height used to clip away.
  function packLegend(items, adv, avail, rowH, gap) {
    const step = rowH > 0 ? rowH : 16;
    const space = gap >= 0 ? gap : 18;
    const rows = [[]];
    let x = 0, row = 0, widest = 0;
    for (const item of items || []) {
      const c = clusterAt(item.model, item.count, adv, avail);
      if (x > 0 && x + c.width > avail) { rows.push([]); row += 1; x = 0; }
      rows[row].push(Object.assign({}, c, { x, y: row * step }));
      x += c.width + space;
      widest = Math.max(widest, x - space);
    }
    if (!rows[0].length) return { rows: [], width: 0, height: 0 };
    return { rows, width: widest, height: rows.length * step };
  }

  window.panelLayout = { fitText, clusterAt, packLegend };
})();