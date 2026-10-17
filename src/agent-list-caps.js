// The rows are (re)sorted biggest-value-first: the fold must keep the
// true top of the list, never the first-arriving rows. The endpoint
// sorts by cost, the tokens list arrives pre-filtered, and a caller's
// ordering is not this file's contract.
window.agentListCaps = {
  capAgentRows(rows, maxVisible) {
    const sorted = [...rows].sort((a, b) => b.value - a.value);
    if (sorted.length <= maxVisible) {
      return { rows: sorted, hiddenCount: 0 };
    }
    const kept = sorted.slice(0, maxVisible - 1);
    const hidden = sorted.slice(maxVisible - 1);
    const sum = key => hidden.reduce((a, r) => a + (r[key] || 0), 0);
    const other = {
      label: `other (${hidden.length} roles)`,
      value: sum('value'),
      requests: sum('requests'),
      isOther: true,
    };
    return { rows: kept.concat([other]), hiddenCount: hidden.length };
  },
};
