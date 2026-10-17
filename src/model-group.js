// One row per model for the Overview's Cost/Tokens by Model panels.
// Both panels are fed a {label: value} map keyed the way app.jsx's
// modelProviderLabel builds labels — "model · host" where a record named
// its serving host, the bare model where it named none — and leave with
// the rows HBar draws.
// By default rows sharing a model id collapse to ONE row: the summed
// value, today's descending sort on that sum, today's share-of-panel
// labels. The collapsed row carries the host rows it stands for (`parts`),
// which is both what the panel's single checkbox swaps it for and what
// its hover breaks down. A model with one surviving row is not a group:
// it comes back exactly as it arrived — bare id or "model · host", no
// parts, so nothing on that row changes.
(function () {
  const HOST_SEP = ' · ';

  function hostOf(label) {
    const i = label.indexOf(HOST_SEP);
    return i < 0 ? null : label.slice(i + HOST_SEP.length);
  }

  // {label: value} -> HBar rows, one per model, desc by summed value.
  // Non-positive rows drop FIRST, as the panels do today: a zero host
  // must neither conjure a group nor inflate one.
  function group(byLabel) {
    const groups = new Map();
    for (const [label, value] of Object.entries(byLabel || {})) {
      if (!(value > 0)) continue;
      const model = label.split(HOST_SEP)[0];
      let g = groups.get(model);
      if (!g) {
        g = { model, value: 0, parts: [] };
        groups.set(model, g);
      }
      g.value += value;
      g.parts.push({ label, value, provider: hostOf(label) });
    }
    return Array.from(groups.values())
      .sort((a, b) => b.value - a.value)
      .map(g => {
        const row = {
          label: g.parts.length > 1 ? g.model : g.parts[0].label,
          value: g.value,
          // A host-split row has no fixedColors entry of its own, so each
          // row carries its MODEL's colour.
          color: (window.modelColors || {})[g.model],
        };
        if (g.parts.length > 1) {
          row.parts = g.parts.slice().sort((a, b) => b.value - a.value);
        }
        return row;
      });
  }

  // Collapsed model rows are the default. Checking the panel's box
  // flattens per-host rows and re-sorts globally by value, keeping
  // exact ties stable rather than stacking each model's hosts.
  function shown(rows, open) {
    if (!open) return rows;
    const flat = [];
    for (const r of rows) {
      if (r.parts) {
        for (const p of r.parts) {
          flat.push({ label: p.label, value: p.value, color: r.color });
        }
      } else {
        flat.push(r);
      }
    }
    return flat.sort((a, b) => b.value - a.value);
  }

  // The collapsed row's hover: one line per host — `DeepInfra 928.03M
  // (75%)` — as a share of that MODEL's total. The bar label already
  // states the model's share of the panel, so these lines say only how
  // the model splits. `valueFmt` formats a VALUE (HBar's `partFmt`, the
  // mount's `v => humanCurrency(v)`), not a row like `fmt` does — the
  // share is computed here. A row without parts (every other HBar mount,
  // and every single-entry row) gets no lines at all.
  function hostLines(row, valueFmt) {
    if (!row.parts || !row.parts.length) return [];
    return row.parts.map(p => [
      p.provider || p.label,
      `${valueFmt(p.value)} (${(p.value / row.value * 100).toFixed(1)}%)`,
    ]);
  }

  window.modelGroup = { group, shown, hostLines };
})();