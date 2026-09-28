// Sessions page: the sortable table of reconstructed sessions. Reads
// window.computeSessions (defined in app.jsx) for the fallback
// clustering, and the window.* helpers for dates, model colors and
// currency. Split out of app.jsx so that file stays under its recorded
// module-size ratchet entry.

const { useState, useMemo } = React;

function SessionsList({ synth, onOpen }) {
  const [sort, setSort] = useState('recent');

  const rows = useMemo(() => {
    let arr;
    // Backend mode: use real per-session rows (with REAL session_ids).
    // The `sessionsOverride` array carries cost/tokens already summed
    // across main + sub-agent files via the deduped CTE, so cost-sort
    // matches the user's mental model.
    if (synth.sessionsOverride && synth.sessionsOverride.length) {
      arr = synth.sessionsOverride.map(s => {
        const ev = (s.events && s.events[0]) || {};
        const total = (ev.input_tokens || 0) + (ev.output_tokens || 0)
                    + (ev.cache_create || 0) + (ev.cache_read || 0);
        return {
          id: s.session_id,
          start: s.start, end: s.end,
          durMin: (s.end - s.start) / 60000,
          reqs: s.requests != null ? s.requests : (ev.requests || 0),
          cost: ev.cost_usd || 0,
          total,
          primary: window.shortModelName ? window.shortModelName(ev.model) : (ev.model || 'unknown'),
        };
      });
    } else {
      // Synth/live fallback: cluster the hourly events as before.
      const { sessions } = window.computeSessions(synth.events);
      arr = sessions.map((s, i) => {
        const sums = { input: 0, output: 0, cc: 0, cr: 0, cost: 0 };
        const models = {};
        for (const e of s.events) {
          sums.input += e.input_tokens; sums.output += e.output_tokens;
          sums.cc += e.cache_create; sums.cr += e.cache_read;
          sums.cost += e.cost_usd;
          models[e.model] = (models[e.model] || 0) + 1;
        }
        let primary = 'opus-4-6', max = 0;
        for (const [m, c] of Object.entries(models)) if (c > max) { max = c; primary = m; }
        return {
          id: 'S' + String(i + 1).padStart(4, '0'),
          start: s.start, end: s.end,
          durMin: (s.end - s.start) / 60000,
          reqs: s.events.length,
          cost: sums.cost,
          total: sums.input + sums.output + sums.cc + sums.cr,
          primary,
        };
      });
    }
    if (sort === 'recent') arr.sort((a, b) => b.start - a.start);
    else if (sort === 'cost') arr.sort((a, b) => b.cost - a.cost);
    else if (sort === 'tokens') arr.sort((a, b) => b.total - a.total);
    return arr;
  }, [synth, sort]);

  return (
    <div className="sessions-page">
      <div className="page-head">
        <h2>Sessions</h2>
        <div className="sort-row">
          <span className="muted">sort:</span>
          {['recent', 'cost', 'tokens'].map(k =>
            <button key={k} className={'chip ' + (sort === k ? 'on' : '')} onClick={() => setSort(k)}>{k}</button>
          )}
          <span className="muted right">showing {rows.length} sessions</span>
        </div>
      </div>
      <div className="sessions-table">
        <div className="srow shead">
          <div>id</div><div>started</div><div>duration</div><div>model</div>
          <div className="num">requests</div><div className="num">tokens</div><div className="num">cost</div><div></div>
        </div>
        {rows.slice(0, 80).map(r => (
          <div key={r.id} className="srow">
            <div className="mono" title={r.id} style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {r.id.length > 10 ? r.id.slice(0, 8) + '…' : r.id}
            </div>
            <div>{window.fmtDate(r.start, { full: true })}</div>
            <div className="mono">{r.durMin < 60 ? r.durMin.toFixed(0)+'m' : (r.durMin/60).toFixed(1)+'h'}</div>
            <div>
              <span className="model-dot" style={{ background: window.modelColors[r.primary] || '#888' }}></span>
              <span className="mono">{r.primary}</span>
            </div>
            <div className="num mono">{r.reqs}</div>
            <div className="num mono">{window.humanFmt(r.total)}</div>
            <div className="num mono">{window.humanCurrency(r.cost)}</div>
            <div className="num"><button className="open-btn" onClick={() => onOpen(r.id)}>open ›</button></div>
          </div>
        ))}
      </div>
      <div className="page-foot muted">List of {rows.length} sessions reconstructed from <code>usage_events</code> via 30-minute gap rule. Click <em>open</em> to drop a real .jsonl into the inspector.</div>
    </div>
  );
}

window.SessionsList = SessionsList;
