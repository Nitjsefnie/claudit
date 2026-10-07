// ---------------------------------------------------------------------------
// Activity Heatmap — weekday × hour grid in Europe/Prague local time.
// DST handling lives in the backend (Postgres AT TIME ZONE); this panel
// only renders the dow/hour cells it is given.
//
// Moved out of dashboard-charts-extra.jsx into its own module: that file
// sits at its measured size entry, and an outgrown rework moves code
// into a new module (the #652/#659 pattern). #690's marks live here:
// every cell, Σ margin and the corner total is a `data-hover-target`
// whose hovered member takes a white stroke, so the interaction sweep
// hovers the grid's buckets like any bar panel's bars.
// ---------------------------------------------------------------------------
const TH_X = window.dashboardTheme;
const COL_X = window.dashboardCol;
const humanFmt_X = window.humanFmt;

const _HEAT_DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const _HEAT_METRICS = [
  { key: 'requests', label: 'requests', color: 'oklch(0.78 0.14 245)', fmt: v => v.toLocaleString() },
  { key: 'output_tokens', label: 'output tok', color: COL_X.outputTokens, fmt: v => humanFmt_X(v) },
  { key: 'cost_usd', label: 'cost', color: COL_X.costUSD, fmt: v => window.humanCurrency(v) },
];

function ActivityHeatmapPanel({ models, project, range, nonce }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(1200);
  const [tip, setTip] = React.useState(null);
  const [cells, setCells] = React.useState([]);
  const [activeModel, setActiveModel] = React.useState('');
  const [metric, setMetric] = React.useState('cost_usd');

  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  React.useEffect(() => {
    const q = (project ? `&project=${encodeURIComponent(project)}` : '')
            + (activeModel ? `&model=${encodeURIComponent(activeModel)}` : '');
    fetch(`/api/activity-heatmap?range=${range || 'all'}${q}`, { credentials: 'same-origin' })
      .then(r => r.json())
      .then(b => setCells(b.cells || []))
      .catch(err => console.error('activity-heatmap fetch failed', err));
  }, [project, range, activeModel, nonce]);

  // Dedup model list by short name for the select (same as ToolUsagePanel).
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

  // A free lane (bonsai-2-27b at $0) paints an empty grid under the
  // cost metric, so the button is dropped and a selection stuck on it
  // falls back to the first metric that does have data.
  const hasCost = cells.some(c => (c.cost_usd || 0) > 0);
  const metricOpts = _HEAT_METRICS.filter(m => m.key !== 'cost_usd' || hasCost);
  const activeMetric = (metric === 'cost_usd' && !hasCost) ? metricOpts[0].key : metric;
  const mspec = _HEAT_METRICS.find(m => m.key === activeMetric) || _HEAT_METRICS[0];

  const { byCell, maxVal, rowTotals, colTotals, grand, rowMax, colMax } = React.useMemo(() => {
    const byCell = new Map();               // dow*100+hour -> cell
    let maxVal = 0;
    const zero = () => ({ requests: 0, output_tokens: 0, cost_usd: 0 });
    const rowTotals = Array.from({ length: 7 }, zero);
    const colTotals = Array.from({ length: 24 }, zero);
    const grand = zero();
    for (const c of cells || []) {
      byCell.set(c.dow * 100 + c.hour, c);
      maxVal = Math.max(maxVal, c[activeMetric] || 0);
      const r = rowTotals[c.dow - 1];
      const col = colTotals[c.hour];
      for (const k of ['requests', 'output_tokens', 'cost_usd']) {
        const v = c[k] || 0;
        r[k] += v; col[k] += v; grand[k] += v;
      }
    }
    const rowMax = Math.max(...rowTotals.map(r => r[activeMetric]), 0);
    const colMax = Math.max(...colTotals.map(c => c[activeMetric]), 0);
    return { byCell, maxVal, rowTotals, colTotals, grand, rowMax, colMax };
  }, [cells, activeMetric]);

  // Geometry — 25 columns × 8 rows (24 hours + Σ, 7 days + Σ), label gutters left + top.
  // cellW floors at 2, not 8 (#636): 8 engaged at a 320px viewport; 2
  // engages only below the ~162px panel where pads+gaps outrun the grid.
  const padL = 44, padR = 14, padT = 24, padB = 10, gap = 2;
  const SUM_GAP = 8;
  const cellW = Math.max(2, (w - padL - padR - SUM_GAP - 23 * gap) / 25);
  const cellH = Math.min(34, Math.max(18, cellW * 0.8));
  const h = padT + 7 * cellH + 6 * gap + SUM_GAP + cellH + padB;
  const sumColX = padL + 23 * (cellW + gap) + cellW + SUM_GAP;
  const sumRowY = padT + 6 * (cellH + gap) + cellH + SUM_GAP;

  // Sequential single-hue ramp on the dark surface: intensity = opacity
  // of the metric hue; sqrt keeps the heavy-tailed mid-range readable.
  function fillFor(v, scaleMax) {
    if (!v || scaleMax <= 0) return { color: TH_X.bgDark, opacity: 1 };
    const t = Math.sqrt(v / scaleMax);
    return { color: mspec.color, opacity: Math.max(0.08, t) };
  }

  function cellRect(dow, hour) {
    return {
      x: padL + hour * (cellW + gap),
      y: padT + (dow - 1) * (cellH + gap),
    };
  }

  function onCellEnter(e, dow, hour) {
    const rect = ref.current.getBoundingClientRect();
    const c = byCell.get(dow * 100 + hour);
    setTip({
      x: e.clientX - rect.left, y: e.clientY - rect.top,
      cell: dow * 100 + hour,
      title: `${_HEAT_DOW[dow - 1]} ${String(hour).padStart(2, '0')}:00–${String((hour + 1) % 24).padStart(2, '0')}:00`,
      accent: mspec.color,
      lines: c ? [
        ['requests',   c.requests.toLocaleString()],
        ['output tok', humanFmt_X(c.output_tokens)],
        ['cost',       window.humanCurrency(c.cost_usd)],
      ] : [['activity', 'none']],
    });
  }

  function onSumColEnter(e, dow) {
    const rect = ref.current.getBoundingClientRect();
    const r = rowTotals[dow - 1];
    const v = r[activeMetric];
    const pct = grand[activeMetric] > 0 ? (v / grand[activeMetric] * 100).toFixed(1) : '0.0';
    setTip({
      x: e.clientX - rect.left, y: e.clientY - rect.top,
      cell: 'sumcol-' + dow,
      title: `${_HEAT_DOW[dow - 1]} · all hours`,
      accent: mspec.color,
      lines: [
        ['requests',   r.requests.toLocaleString()],
        ['output tok', humanFmt_X(r.output_tokens)],
        ['cost',       window.humanCurrency(r.cost_usd)],
        ['share',      pct + '% of total'],
      ],
    });
  }

  function onSumRowEnter(e, hour) {
    const rect = ref.current.getBoundingClientRect();
    const c = colTotals[hour];
    const v = c[activeMetric];
    const pct = grand[activeMetric] > 0 ? (v / grand[activeMetric] * 100).toFixed(1) : '0.0';
    setTip({
      x: e.clientX - rect.left, y: e.clientY - rect.top,
      cell: 'sumrow-' + hour,
      title: `${String(hour).padStart(2, '0')}:00–${String((hour + 1) % 24).padStart(2, '0')}:00 · all days`,
      accent: mspec.color,
      lines: [
        ['requests',   c.requests.toLocaleString()],
        ['output tok', humanFmt_X(c.output_tokens)],
        ['cost',       window.humanCurrency(c.cost_usd)],
        ['share',      pct + '% of total'],
      ],
    });
  }

  function onCornerEnter(e) {
    const rect = ref.current.getBoundingClientRect();
    setTip({
      x: e.clientX - rect.left, y: e.clientY - rect.top,
      cell: 'corner',
      title: 'Total',
      accent: mspec.color,
      lines: [
        ['requests',   grand.requests.toLocaleString()],
        ['output tok', humanFmt_X(grand.output_tokens)],
        ['cost',       window.humanCurrency(grand.cost_usd)],
      ],
    });
  }

  const legendW = 120;
  const a11y = window.useChartA11y('Activity Heatmap',
    `${mspec.label} by weekday and hour, peak `
      + `${maxVal > 0 ? mspec.fmt(maxVal) : 'no data'}`,
    `Grid of ${mspec.label} by weekday and hour in Europe/Prague local `
      + 'time, with weekday, hour and grand totals in the margins.');
  return (
    <div ref={ref} style={{
      background: TH_X.bgAxes, border: `1px solid ${TH_X.border}`,
      borderRadius: 4, padding: 0, position: 'relative',
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ padding: '10px 14px 4px', borderBottom: `1px solid ${TH_X.border}`, display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
        <div style={{ flex: 1 }}>
          <div style={{ color: TH_X.text, fontFamily: 'monospace', fontWeight: 700, fontSize: 14 }}>
            Activity Heatmap
          </div>
          <div style={{ color: TH_X.textDim, fontFamily: 'monospace', fontSize: 10, marginTop: 2 }}>
            {mspec.label} by weekday × hour · Europe/Prague (CET/CEST, DST-aware)
          </div>
        </div>
        <div style={{ display: 'inline-flex', flexWrap: 'wrap', alignItems: 'center', gap: 6, fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim }}>
          {metricOpts.map(m => (
            <button key={m.key} type="button" onClick={() => setMetric(m.key)}
              style={{
                background: 'transparent',
                color: activeMetric === m.key ? TH_X.text : TH_X.textDim,
                border: `1px solid ${activeMetric === m.key ? m.color : TH_X.border}`,
                borderRadius: 3, padding: '2px 8px',
                fontFamily: 'monospace', fontSize: 11, cursor: 'pointer',
              }}
            >{m.label}</button>
          ))}
          <span style={{ marginLeft: 8 }}>model:</span>
          <select aria-label="Activity Heatmap model filter"
            value={activeModel}
            onChange={e => setActiveModel(e.target.value)}
            style={{
              background: '#16172e', color: TH_X.text,
              border: `1px solid ${TH_X.border}`, borderRadius: 4,
              padding: '3px 6px', fontFamily: 'monospace', fontSize: 11,
              cursor: 'pointer',
            }}
          >
            <option value="">All</option>
            {modelOpts.map(o => (
              <option key={o.key} value={o.key}>{o.key}</option>
            ))}
          </select>
        </div>
      </div>

      <svg role="img" aria-label={a11y.label} aria-describedby={a11y.descId}
        data-panel="Activity Heatmap" width="100%" height={h} style={{ display: 'block' }}
           onMouseLeave={() => setTip(null)}>
        {/* Separator lines in the SUM_GAP bands — margins read as distinct. */}
        <line x1={padL + 23 * (cellW + gap) + cellW + SUM_GAP / 2}
              x2={padL + 23 * (cellW + gap) + cellW + SUM_GAP / 2}
              y1={padT} y2={sumRowY + cellH}
              stroke="#fff" strokeWidth="1" strokeOpacity={0.85} />
        <line x1={padL}
              x2={sumColX + cellW}
              y1={padT + 6 * (cellH + gap) + cellH + SUM_GAP / 2}
              y2={padT + 6 * (cellH + gap) + cellH + SUM_GAP / 2}
              stroke="#fff" strokeWidth="1" strokeOpacity={0.85} />

        {/* hour labels every 3h */}
        <g data-role="axis">{[0, 3, 6, 9, 12, 15, 18, 21].map(hr => (
          <text key={hr} x={cellRect(1, hr).x + cellW / 2} y={padT - 8}
                textAnchor="middle" fill={TH_X.textDim}
                fontFamily="monospace" fontSize="9">{hr}</text>
        ))}</g>
        {/* Σ column header */}
        <text data-role="axis" x={sumColX + cellW / 2} y={padT - 8}
              textAnchor="middle" fill={TH_X.textDim}
              fontFamily="monospace" fontSize="9">Σ</text>

        {/* weekday labels */}
        <g data-role="axis">{_HEAT_DOW.map((d, i) => (
          <text key={d} x={padL - 8} y={padT + i * (cellH + gap) + cellH / 2 + 3}
                textAnchor="end" fill={TH_X.textDim}
                fontFamily="monospace" fontSize="9">{d}</text>
        ))}</g>
        {/* Σ row label */}
        <text data-role="axis" x={padL - 8} y={sumRowY + cellH / 2 + 3}
              textAnchor="end" fill={TH_X.textDim}
              fontFamily="monospace" fontSize="9">Σ</text>

        {/* cells */}
        <rect data-role="plot" x={padL} y={padT} width={sumColX + cellW - padL} height={sumRowY + cellH - padT} fill="none" />{Array.from({ length: 7 }, (_, di) => di + 1).map(dow =>
          Array.from({ length: 24 }, (_, hour) => {
            const { x, y } = cellRect(dow, hour);
            const c = byCell.get(dow * 100 + hour);
            const v = c ? (c[activeMetric] || 0) : 0;
            const f = fillFor(v, maxVal);
            const hovered = tip && tip.cell === dow * 100 + hour;
            return (
              <rect key={`${dow}-${hour}`} data-hover-target="" x={x} y={y}
                    width={cellW} height={cellH} rx="2"
                    fill={f.color} fillOpacity={f.opacity}
                    stroke={hovered ? '#fff' : (v > 0 ? 'none' : TH_X.border)}
                    strokeWidth={hovered ? 1 : (v > 0 ? 0 : 0.5)}
                    onMouseMove={e => onCellEnter(e, dow, hour)} />
            );
          })
        )}

        {/* Σ column (per-weekday totals) */}
        {Array.from({ length: 7 }, (_, i) => i + 1).map(dow => {
          const v = rowTotals[dow - 1][activeMetric];
          const f = fillFor(v, rowMax);
          const hovered = tip && tip.cell === 'sumcol-' + dow;
          return (
            <rect key={`sumcol-${dow}`} data-hover-target="" x={sumColX}
                  y={padT + (dow - 1) * (cellH + gap)}
                  width={cellW} height={cellH} rx="2"
                  fill={f.color} fillOpacity={f.opacity}
                  stroke={hovered ? '#fff' : (v > 0 ? 'none' : TH_X.border)}
                  strokeWidth={hovered ? 1 : (v > 0 ? 0 : 0.5)}
                  onMouseMove={e => onSumColEnter(e, dow)} />
          );
        })}

        {/* Σ row (per-hour totals) */}
        {Array.from({ length: 24 }, (_, hour) => {
          const v = colTotals[hour][activeMetric];
          const f = fillFor(v, colMax);
          const hovered = tip && tip.cell === 'sumrow-' + hour;
          return (
            <rect key={`sumrow-${hour}`} data-hover-target=""
                  x={padL + hour * (cellW + gap)}
                  y={sumRowY} width={cellW} height={cellH} rx="2"
                  fill={f.color} fillOpacity={f.opacity}
                  stroke={hovered ? '#fff' : (v > 0 ? 'none' : TH_X.border)}
                  strokeWidth={hovered ? 1 : (v > 0 ? 0 : 0.5)}
                  onMouseMove={e => onSumRowEnter(e, hour)} />
          );
        })}

        {/* Corner grand total (outline only; value in tooltip) */}
        {/* A painted transparent fill: fill="none" is invisible to
            hit-testing, so a pointer on the corner fell through to the
            svg and the grand total's tooltip could not be summoned. */}
        <rect data-hover-target="" x={sumColX} y={sumRowY}
              width={cellW} height={cellH} rx="2"
              fill="#fff" fillOpacity="0"
              stroke={tip && tip.cell === 'corner' ? '#fff' : TH_X.border}
              strokeWidth={tip && tip.cell === 'corner' ? 1 : 0.5}
              onMouseMove={onCornerEnter} />
      </svg>
      {a11y.descText && (
        <span className="sr-only" id={a11y.descId}>{a11y.descText}</span>
      )}

      <div style={{
        padding: '4px 14px 10px', display: 'flex', alignItems: 'center', gap: 8,
        fontFamily: 'monospace', fontSize: 10, color: TH_X.textDim,
      }}>
        <span>0</span>
        <svg aria-hidden="true" data-panel="Activity Heatmap — legend" data-static-panel=""
             viewBox={`0 0 ${legendW} 10`} preserveAspectRatio="none"
             width={legendW} height="10">
          <defs>
            <linearGradient id="heatLegendGrad" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%"  stopColor={mspec.color} stopOpacity="0.05" />
              <stop offset="100%" stopColor={mspec.color} stopOpacity="1" />
            </linearGradient>
          </defs>
          <rect data-role="legend" x="0" y="0" width={legendW} height="10" rx="2" fill="url(#heatLegendGrad)" />
        </svg>
        <span>{maxVal > 0 ? mspec.fmt(maxVal) : 'no data'}</span>
        <span style={{ marginLeft: 'auto' }}>intensity ∝ √(value / max) · Σ margins scaled independently</span>
      </div>

      {tip && <window.DashTooltip tip={tip} />}
    </div>
  );
}
window.ActivityHeatmapPanel = ActivityHeatmapPanel;
