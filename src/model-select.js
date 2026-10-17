// The shared model-selection rule for the single-chart model panels
// Both panels — Per-Session Context Growth and Tool Error Rate — check
// a default set from one checkbox row and draw every checked entry on
// ONE chart. The default is the top n by the caller's sort order, with
// the caller's overrides layered on top; the override map is keyed by
// entry name and never expires, so unchecking a default keeps it
// unchecked if the data reshuffles beneath it. The tool panel reuses
// the same rule for its per-tool picker, seeding Other checked.
(function () {
  // The checked set for `entries` (sorted best-first, each carrying
  // `model`), the caller's override map, and the default size n. An
  // override of `true` checks a model the default left out; `false`
  // unchecks one the default checked. Overrides may name models that
  // have left the range; they are inert until one returns.
  function topDefaultSelection(entries, overrides, n) {
    const sel = new Set();
    const list = Array.isArray(entries) ? entries : [];
    const cap = n > 0 ? n : 0;
    for (const e of list.slice(0, cap)) {
      const key = e && typeof e === 'object' ? e.model : e;
      if (key != null) sel.add(key);
    }
    for (const [k, on] of Object.entries(overrides || {})) {
      if (on) sel.add(k); else sel.delete(k);
    }
    return sel;
  }

  window.modelSelect = { topDefaultSelection };
})();
