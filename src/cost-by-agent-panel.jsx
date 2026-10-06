// ──────────────────────────────────────────────────────────────────────
// Cost by Agent Type — bounded, side by side (#651).
//
// The pre-#651 panel stacked two full-width embedded HBars under one
// header and grew one row per distinct agent role — 1662×1860px at 25
// roles, taller with every new role. The pair now sits side by side
// like its Cost/Tokens by Model siblings (a dash-grid-2 inside the
// card; it stacks under 768px like every other dash-grid-2), and each
// list is capped: the roles past the fold fold into one aggregate
// `other` row, and the header's toggle expands the full list (click
// again to collapse).
//
// The card declares data-max-h: the #647 interaction guard checks a
// panel that declares a bound against that ABSOLUTE ceiling in both of
// its payload worlds (2 roles vs 30+), instead of the equal-heights
// equality every unbounded panel owes — a capped list is shorter at 2
// roles than at 30+, and that is the design, not a regression.
//
// The `general-purpose` bar is NOT a claim that those sessions were
// dispatched as general-purpose agents. It is where every transcript
// with no role recorded lands (see parse.resolve_agent_type) — a plain
// lead, a session started with an explicit --agent flag, and any
// subagent transcript predating Claude Code 2.1.126 all read the same
// in the file. The subtitle says so, because a bar this large silently
// meaning "unattributed" would be read as a measurement.
// ──────────────────────────────────────────────────────────────────────
const humanFmt_X = window.humanFmt;
// 8 visible rows: 7 kept + the aggregate `other` row whenever the
// roster outgrows the list.
const MAX_VISIBLE = 8;
// HBar's svg stands 32 + rows*36 + 18 tall — 338px at the cap, and
// that svg height is what the guard measures against this attribute.
// The declared figure adds headroom for the header strip; the card as
// a whole is not measured by anything.
const MAX_PANEL_H = 50 + MAX_VISIBLE * 36 + 26;

function CostByAgentPanel({ models, project, range, nonce }) {
  const [rows, setRows] = React.useState([]);
  const [total, setTotal] = React.useState(0);
  const [totalTokens, setTotalTokens] = React.useState(0);
  // Per-panel model filter, same convention as ToolUsagePanel and
  // CostByContextPanel: drill into one model without disturbing the
  // other panels.
  const [activeModel, setActiveModel] = React.useState('');
  const [expanded, setExpanded] = React.useState(false);

  React.useEffect(() => {
    const q = (project ? `&project=${encodeURIComponent(project)}` : '')
            + (activeModel ? `&model=${encodeURIComponent(activeModel)}` : '');
    fetch(`/api/cost-by-agent?range=${range || 'all'}${q}`, { credentials: 'same-origin' })
      .then(r => r.json())
      .then(b => {
        setRows(b.agents || []);
        setTotal(b.total_cost_usd || 0);
        setTotalTokens(b.total_tokens || 0);
      })
      .catch(err => console.error('cost-by-agent fetch failed', err));
  }, [project, range, activeModel, nonce]);

  const modelOpts = React.useMemo(() => {
    const grouped = {};
    for (const m of models || []) {
      const key = window.shortModelName ? window.shortModelName(m.model) : m.model;
      if (key === '<synthetic>' || key === 'synthetic') continue;
      grouped[key] = (grouped[key] || 0) + (m.n || 0);
    }
    return Object.entries(grouped)
      .sort((a, b) => b[1] - a[1]).map(([k, n]) => ({ key: k, n }));
  }, [models]);

  const bars = React.useMemo(() => rows.map(a => ({
    label: a.agent_type,
    value: a.cost_usd || 0,
    color: window.toolColor(a.agent_type),
    requests: a.requests,
  })), [rows]);

  // The same roles measured in tokens, biggest first — the endpoint
  // orders by cost, which a free lane leaves arbitrary. Zero-token
  // roles drop out here, BEFORE the cap, so they never occupy a visible
  // slot nor inflate the aggregate; the cap itself re-sorts.
  const tokenBars = React.useMemo(() => rows.map(a => ({
    label: a.agent_type,
    value: a.total_tokens || 0,
    color: window.toolColor(a.agent_type),
    requests: a.requests,
  })).filter(b => b.value > 0), [rows]);

  const maxVisible = expanded ? Infinity : MAX_VISIBLE;
  const costCap = window.agentListCaps.capAgentRows(bars, maxVisible);
  const tokenCap = window.agentListCaps.capAgentRows(tokenBars, maxVisible);
  // The toggle's gate reads the fold AT THE CAP, independent of
  // expanded: an expanded cap hides nothing, so a gate on the current
  // fold would unmount the button the moment it is used — nothing
  // could ever collapse the list again. Both lists share one expanded
  // state, so one click opens both.
  const collapsedHidden = Math.max(
    window.agentListCaps.capAgentRows(bars, MAX_VISIBLE).hiddenCount,
    window.agentListCaps.capAgentRows(tokenBars, MAX_VISIBLE).hiddenCount);

  return (
    <div data-max-h={MAX_PANEL_H} style={{
      background: 'var(--bg-card)', border: '1px solid var(--border)',
      borderRadius: 4, display: 'flex', flexDirection: 'column',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        gap: 8, padding: '8px 14px 0',
        fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)',
      }}>
        <span>general-purpose = no role recorded in the transcript</span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {collapsedHidden > 0 && (
            <button onClick={() => setExpanded(x => !x)} style={{
              background: 'var(--panel-2)', color: 'var(--fg)',
              border: '1px solid var(--border)', borderRadius: 4,
              padding: '3px 6px', fontFamily: 'var(--mono)', fontSize: 11,
              cursor: 'pointer',
            }}>
              {expanded ? 'show fewer' : `all ${rows.length} roles`}
            </button>
          )}
          <span>model:</span>
          <select aria-label="Agent Type model filter"
            value={activeModel}
            onChange={e => setActiveModel(e.target.value)}
            style={{
              background: 'var(--panel-2)', color: 'var(--fg)',
              border: '1px solid var(--border)', borderRadius: 4,
              padding: '3px 6px', fontFamily: 'var(--mono)', fontSize: 11,
              cursor: 'pointer',
            }}>
            <option value="">All</option>
            {modelOpts.map(m => <option key={m.key} value={m.key}>{m.key}</option>)}
          </select>
        </span>
      </div>
      <div className="dash-grid-2" style={{ padding: '0 8px 8px' }}>
        {total > 0 && (
        <window.HBar
          embedded
          title="Cost by Agent Type"
          rows={costCap.rows}
          fmt={r => `${window.humanCurrency(r.value)} (${(r.value / total * 100).toFixed(1)}%)`} />
        )}
        <window.HBar
          embedded
          title="Tokens by Agent Type"
          rows={tokenCap.rows}
          fmt={r => `${humanFmt_X(r.value)} (${totalTokens > 0 ? (r.value / totalTokens * 100).toFixed(1) : '0.0'}%)`} />
      </div>
    </div>
  );
}

window.CostByAgentPanel = CostByAgentPanel;
