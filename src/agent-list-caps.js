// The bounded row cap behind the Cost by Agent Type panel (#651).
//
// Plain JS, no React: the panel (src/cost-by-agent-panel.jsx) feeds each
// bar list through capAgentRows before handing it to HBar, and
// tests/test_agent_list_caps_js.py drives this file through node (node
// cannot parse JSX). The cap is what keeps the panel's height a function
// of MAX_VISIBLE instead of the role count -- #651's subject.
//
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
