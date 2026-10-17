// The Tool Error Rate panel — ONE chart, not a sub-panel
// per model.
// Its own module keeps dashboard-charts-extra.jsx within the size ratchet.
// Model checkboxes select the series in one fixed-height chart, so
// adding models does not grow a grid of sub-panels.
// Each checked model adds its AGGREGATE error-rate EMA (α=0.15) line in
// its own colour, always. The per-tool toggle (showPerTool, off by
// default) adds the tools picked in the tool picker (top-3 by calls plus
// Other, unless unchecked), one dash pattern per tool; the tool legend
// shows the patterns when the toggle is on. The rate/EMA math lives in
// src/rate-series.js — plain JS, node-testable. Numerator = n_error,
// denominator = n_total over settled calls; unmatched calls excluded by
// the API.
const TH_X = window.dashboardTheme;

// One dash pattern per tool, by PICKER position; cycled when the picker
// holds more tools than patterns. Indexed only inside the toolDash map.
const TOOL_DASHES = ['', '4,3', '1,3', '7,3', '2,4', '5,2'];

const PROBE_CHARS = '0123456789abcdefghij';
const OTHER = '__OTHER__';
const _OTHER_COLOR = '#5a627a';

function ToolErrorRatePanel({ project, range, nonce }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(1200);
  const [data, setData] = React.useState([]);
  const [bucketMs, setBucketMs] = React.useState(86400000);
  // The chart's height is a constant, whatever the model
  // count: header + checkbox rows + one fixed-height chart.
  const h = 240;
  // Cap visible per-tool checkboxes; the rest collapse into a single
  // "Other" series (ToolUsagePanel's TOP_N treatment, so the row stays
  // readable on models with many tools).
  const TOP_N = 5;

  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  // Measured advance of the legend's 9.5px monospace, read off a fixed
  // probe string parked off-canvas. The comparison panel measures its
  // advances the same way and for the same reason: a predicted advance
  // can let long model names overlap their neighbours.
  const legRef = React.useRef(null);
  const [adv, setAdv] = React.useState(0);
  React.useLayoutEffect(() => {
    const t = legRef.current;
    if (!t || !t.getComputedTextLength) return;
    const a = t.getComputedTextLength() / PROBE_CHARS.length;
    if (a > 0 && Math.abs(a - adv) > 0.05) setAdv(a);
  });

  React.useEffect(() => {
    const q = (project ? `&project=${encodeURIComponent(project)}` : '');
    fetch(`/api/tool-error-rate?range=${range || 'all'}${q}`, { credentials: 'same-origin' })
      .then(r => r.json())
      .then(b => {
        setData(b.buckets || []);
        if (b.bucket_s) setBucketMs(b.bucket_s * 1000);
      })
      .catch(err => console.error('tool-error-rate fetch failed', err));
  }, [project, range, nonce]);

  const byModel = React.useMemo(() => {
    const out = {};
    for (const r of (data || [])) {
      const t = Date.parse(r.ts);
      if (isNaN(t)) continue;
      const key = window.shortModelName
        ? window.shortModelName(r.model) : r.model;
      if (!key || key === '<synthetic>' || key === 'synthetic') continue;
      if (!out[key]) out[key] = {
        perBucketTool: new Map(),
        totalsByTool:  new Map(),
        bucketSet:     new Set(),
      };
      const M = out[key];
      M.bucketSet.add(t);
      if (!M.perBucketTool.has(t)) M.perBucketTool.set(t, new Map());
      const cur = M.perBucketTool.get(t).get(r.tool) || { n_total: 0, n_error: 0 };
      cur.n_total += r.n_total;
      cur.n_error += r.n_error;
      M.perBucketTool.get(t).set(r.tool, cur);
      M.totalsByTool.set(r.tool, (M.totalsByTool.get(r.tool) || 0) + r.n_total);
    }
    for (const k of Object.keys(out)) {
      out[k].buckets = [...out[k].bucketSet].sort((a, b) => a - b);
      delete out[k].bucketSet;
    }
    return out;
  }, [data]);

  const models = React.useMemo(() => {
    return Object.entries(byModel)
      .map(([m, v]) => {
        let total = 0;
        for (const n of v.totalsByTool.values()) total += n;
        return { model: m, total };
      })
      .sort((a, b) => b.total - a.total);
  }, [byModel]);

  const toolEntries = React.useMemo(() => {
    const totals = new Map();
    for (const v of Object.values(byModel)) {
      for (const [tool, n] of v.totalsByTool) {
        totals.set(tool, (totals.get(tool) || 0) + n);
      }
    }
    return [...totals.entries()]
      .sort((a, b) => b[1] - a[1]).map(([tool, n]) => ({ model: tool, count: n }));
  }, [byModel]);

  const visibleTools = React.useMemo(
    () => toolEntries.slice(0, TOP_N).map(e => e.model),
    [toolEntries]);
  const otherTools = React.useMemo(
    () => toolEntries.slice(TOP_N).map(e => e.model),
    [toolEntries]);
  const hasOther = otherTools.length > 0;
  const otherTotal = React.useMemo(
    () => otherTools.reduce((s, t) => {
      let n = 0;
      for (const v of Object.values(byModel)) {
        n += v.totalsByTool.get(t) || 0;
      }
      return s + n;
    }, 0),
    [otherTools, byModel]);

  const [modelOverrides, setModelOverrides] = React.useState({});
  const selModels = React.useMemo(
    () => window.modelSelect.topDefaultSelection(models, modelOverrides, 2),
    [models, modelOverrides]);
  const [toolOverrides, setToolOverrides] = React.useState({});
  const toolPickerEntries = React.useMemo(() => {
    const entries = visibleTools.map(t => {
      let n = 0;
      for (const v of Object.values(byModel)) {
        n += v.totalsByTool.get(t) || 0;
      }
      return { model: t, count: n };
    });
    if (hasOther) entries.push({ model: OTHER, count: otherTotal });
    return entries;
  }, [visibleTools, hasOther, otherTotal, byModel]);
  const selTools = React.useMemo(
    () => window.modelSelect.topDefaultSelection(
      toolPickerEntries, { [OTHER]: true, ...toolOverrides }, 3),
    [toolPickerEntries, toolOverrides]);
  const [showPerTool, setShowPerTool] = React.useState(false);

  function toggleModel(m) {
    setModelOverrides(prev => ({ ...prev, [m]: !selModels.has(m) }));
  }
  function toggleTool(k) {
    setToolOverrides(prev => ({ ...prev, [k]: !selTools.has(k) }));
  }

  const drawn = React.useMemo(
    () => window.rateSeries.drawnLines(models, selModels, byModel, visibleTools, otherTools, OTHER),
    [models, selModels, byModel, visibleTools, otherTools]);

  const hitBuckets = [...new Set(drawn.flatMap(d => byModel[d.model].buckets))].sort((a, b) => a - b);

  const yMax = React.useMemo(() => {
    let m = 0;
    for (const d of drawn) {
      for (const [k, arr] of d.perKey) {
        if (k !== '__AGG__' && !showPerTool) continue;
        if (k !== '__AGG__' && !selTools.has(k)) continue;
        for (const p of arr) if (p.ema > m) m = p.ema;
      }
    }
    return Math.max(m * 1.1, 0.001);
  }, [drawn, showPerTool, selTools]);

  const xMin = React.useMemo(() => {
    let lo = 0;
    for (const d of drawn) {
      const md = byModel[d.model];
      if (md.buckets.length && (!lo || md.buckets[0] < lo)) lo = md.buckets[0];
    }
    return lo;
  }, [drawn, byModel]);
  const xMax = React.useMemo(() => {
    let hi = 1;
    for (const d of drawn) {
      const md = byModel[d.model];
      const last = md.buckets.length ? md.buckets[md.buckets.length - 1] + bucketMs : 1;
      if (last > hi) hi = last;
    }
    return hi;
  }, [drawn, byModel, bucketMs]);

  // padL 50, not 38: a 5-char percentage label end-anchored at padL - 5
  // renders 29.3px wide, which left 3.7px to the panel edge; 50 gives
  // 15.7px and still ~10px for a 6-char label.
  const padL = 50, padR = 6, padT = 22, padB = 22;
  const plotW = Math.max(1, w - padL - padR);
  const plotH = Math.max(1, h - padT - padB);
  const xs = (t) => padL + ((t - xMin) / Math.max(1, xMax - xMin)) * plotW;
  const ys = (v) => padT + plotH - (v / yMax) * plotH;

  function colorFor(key) {
    if (key === OTHER) return _OTHER_COLOR;
    return window.toolColor(key);
  }

  function labelFor(key) {
    if (key === OTHER) return `Other (${otherTools.length})`;
    return key;
  }

  // One dash pattern per tool, keyed by the DISPLAY label from the
  // PICKER position — checked or not. The line loop and the legend both
  // read this one map; a second index site is the drift again.
  const toolDash = React.useMemo(() => Object.fromEntries(
    toolPickerEntries.map((k, ti) =>
      [labelFor(k.model), TOOL_DASHES[ti % TOOL_DASHES.length]])),
  [toolPickerEntries, otherTools.length]);

  // The per-tool lines are drawn in the MODEL's colour, so the legend
  // that distinguishes them is the dash pattern: one per checked tool,
  // from the shared toolDash map (its picker position). Shown only
  // while the toggle is on. Packed by the measured
  // advance, like every legend on these panels, so long labels fit.
  const toolLegend = React.useMemo(() => {
    if (!showPerTool) return { rows: [] };
    const picked = toolPickerEntries.filter(e => selTools.has(e.model));
    return {
      rows: window.panelLayout.packLegend(
        picked.map(e => ({ model: labelFor(e.model), count: null })),
        (adv || 5.7), plotW, 16, 18).rows,
    };
  }, [showPerTool, toolPickerEntries, selTools, plotW, adv, otherTools.length]);

  const [tip, setTip] = React.useState(null);

  function onMove(e) {
    const rect = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    if (mx < padL || mx > w - padR || my < padT || my > padT + plotH) {
      setTip(null); return;
    }
    if (!drawn.length) { setTip(null); return; }
    let best = null;
    for (const d of drawn) {
      const md = byModel[d.model];
      for (const ts of md.buckets) {
        const d2 = Math.abs(xs(ts + bucketMs / 2) - mx);
        if (!best || d2 < best.d2) best = { d2, ts };
      }
    }
    const ts = best.ts, bi = hitBuckets.indexOf(best.ts);
    const lines = [];
    for (const d of drawn) {
      const md = byModel[d.model];
      const bucket = md.perBucketTool.get(ts);
      if (!bucket) continue;
      let aT = 0, aE = 0;
      for (const v of bucket.values()) { aT += v.n_total; aE += v.n_error; }
      lines.push([`${d.model} aggregate`,
        aT ? `${aE}/${aT} = ${((aE / aT) * 100).toFixed(2)}%` : '-']);
      if (!showPerTool) continue;
      for (const k of toolPickerEntries) {
        if (!selTools.has(k.model)) continue;
        if (k.model === OTHER) {
          let oT = 0, oE = 0;
          for (const tool of otherTools) {
            const v = bucket.get(tool);
            if (v) { oT += v.n_total; oE += v.n_error; }
          }
          if (oT > 0) lines.push([`${d.model} other`,
            `${oE}/${oT} = ${((oE / oT) * 100).toFixed(2)}%`]);
        } else {
          const v = bucket.get(k.model);
          if (v) lines.push([`${d.model} ${k.model}`,
            `${v.n_error}/${v.n_total} = ${((v.n_error / v.n_total) * 100).toFixed(2)}%`]);
        }
      }
    }
    setTip({
      x: mx, y: my, cx: xs(ts + bucketMs / 2), bi,
      title: new Date(ts).toISOString().replace('T', ' ').slice(0, 16) + ' UTC',
      accent: '#ddd',
      lines,
    });
  }

  const a11y = window.useChartA11y(
    'Tool Error Rate',
    `error-rate EMA lines, ${drawn.length} models`,
    drawn.length ? `Showing aggregate error rate for: ${drawn.map(d => d.model).join(', ')}`
      + (showPerTool ? ', with per-tool lines.' : '.') : null);

  const svgH = h + Math.max(0, toolLegend.rows.length) * 16 + 8;

  return (
    <div ref={ref} style={{
      background: TH_X.bgAxes, border: `1px solid ${TH_X.border}`,
      borderRadius: 4, padding: 0, position: 'relative',
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ padding: '10px 14px 4px', borderBottom: `1px solid ${TH_X.border}` }}>
        <div style={{ color: TH_X.text, fontFamily: 'monospace', fontWeight: 700, fontSize: 14 }}>
          Tool Error Rate
        </div>
        <div style={{ color: TH_X.textDim, fontFamily: 'monospace', fontSize: 10, marginTop: 2 }}>
          per-model EMA (α=0.15) of n_error / n_total · only tool calls with a settled tool_result counted
        </div>
      </div>

      {!models.length && (
        <div style={{ padding: 16, color: TH_X.textDim, fontFamily: 'monospace', fontSize: 12 }}>
          no tool calls in range
        </div>
      )}

      {/* Model checkbox strip — BELOW the chart, like every other
          panel's legend: order 99 paints it after the chart; each
          checkbox IS its model's colour key. */}
      {models.length > 0 && (
        <div data-role="legend" style={{
          padding: '8px 14px', borderTop: `1px solid ${TH_X.border}`,
          display: 'flex', flexWrap: 'wrap', gap: '14px 14px',
          fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim,
          alignItems: 'center', order: 99,
        }}>
          <span>models:</span>
          {models.map(m => {
            const c = (window.modelColors && window.modelColors[m.model]) || '#888';
            return (
              <window.LegendCheckboxRow key={m.model} id={m.model} color={c}
                checked={selModels.has(m.model)} onToggle={toggleModel}
                name={m.model} count={m.total.toLocaleString()} />
            );
          })}
          <window.ToggleChip on={showPerTool} onToggle={() => setShowPerTool(s => !s)}
            label="per-tool" />
        </div>
      )}

      {/* Tool picker strip: top-3 tools by calls plus Other, below the
          chart with the models strip. */}
      {models.length > 0 && (
        <div data-role="legend" style={{
          padding: '8px 14px', borderTop: `1px solid ${TH_X.border}`,
          display: 'flex', flexWrap: 'wrap', gap: '14px 14px',
          fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim,
          alignItems: 'center', order: 99,
        }}>
          <span>per-tool:</span>
          {toolPickerEntries.map(k => {
            const c = k.model === OTHER ? _OTHER_COLOR : colorFor(k.model);
            return (
              <window.LegendCheckboxRow key={k.model} id={k.model} color={c}
                checked={selTools.has(k.model)} onToggle={toggleTool}
                name={labelFor(k.model)} count={k.count.toLocaleString()} />
            );
          })}
        </div>
      )}

      {/* The ONE chart. The svg grows only when the dashed tool
          legend below it wraps; both are independent of the model
          count. */}
      {models.length > 0 && (
        <div style={{ position: 'relative' }} onMouseMove={onMove}
          onMouseLeave={() => setTip(null)}>
          <svg role="img" aria-label={a11y.label} aria-describedby={a11y.descId}
            data-panel="Tool Error Rate" width={w} height={svgH} style={{ display: 'block' }}>
            <text ref={legRef} x="-9999" y="-9999" fontSize="9.5" opacity="0"
              fontFamily="monospace">{PROBE_CHARS}</text>

            {/* y axis */}
            <line x1={padL} y1={padT} x2={padL} y2={padT + plotH} stroke={TH_X.border} />
            <line x1={padL} y1={padT + plotH} x2={padL + plotW} y2={padT + plotH} stroke={TH_X.border} />

            {/* y ticks: 0%, 50%, 100% of yMax */}
            <g data-role="axis">{[0, 0.5, 1].map((f, i) => {
              const v = f * yMax;
              return (
                <g key={i}>
                  <line x1={padL - 3} y1={ys(v)} x2={padL} y2={ys(v)} stroke={TH_X.border} />
                  <text x={padL - 5} y={ys(v) + 3} textAnchor="end"
                        fontSize="9" fontFamily="monospace" fill={TH_X.textDim}>
                    {(v * 100).toFixed(v < 0.01 ? 2 : 1)}%
                  </text>
                </g>
              );
            })}</g>

            <rect data-role="plot" x={padL} y={padT} width={plotW} height={plotH} fill="none" />

            {/* Aggregate EMA per checked model, in the model's colour. */}
            {drawn.map(d => {
              const arr = d.perKey.get('__AGG__') || [];
              if (arr.length < 2) return null;
              const c = (window.modelColors && window.modelColors[d.model]) || '#888';
              const pts = arr.map(p => `${xs(p.t_ms + bucketMs / 2)},${ys(p.ema)}`).join(' ');
              return (
                <polyline key={'agg-' + d.model} points={pts} fill="none"
                  stroke={c} strokeWidth="1.6" />
              );
            })}

            {/* Per-tool EMA lines, dashed per tool, in the model's
                colour, behind the aggregate lines. */}
            {showPerTool && drawn.map(d => {
              const c = (window.modelColors && window.modelColors[d.model]) || '#888';
              return toolPickerEntries.map((k) => {
                if (!selTools.has(k.model)) return null;
                const arr = d.perKey.get(k.model) || [];
                if (arr.length < 2) return null;
                const pts = arr.map(p => `${xs(p.t_ms + bucketMs / 2)},${ys(p.ema)}`).join(' ');
                return (
                  <polyline key={`tool-${d.model}-${k.model}`} points={pts} fill="none"
                    stroke={c} strokeWidth="1"
                    strokeDasharray={toolDash[labelFor(k.model)] || undefined} />
                );
              });
            })}

            {/* The dashed per-tool legend — which pattern is which tool.
                Packed by the measured advance and wrapped, so it fits
                320px the way every legend on these panels does. */}
            {toolLegend.rows.length > 0 && (
              <g transform={`translate(${padL}, ${h + 6})`}>
                {toolLegend.rows.flat().map(c => (
                  <g key={'tl-' + c.label} data-role="legend"
                    transform={`translate(${c.x}, ${c.y})`}>
                    <line x1={c.ruleX} x2={c.ruleX + 16} y1={5} y2={5}
                      stroke={TH_X.text} strokeWidth="1.2"
                      strokeDasharray={toolDash[c.label] || undefined} />
                    <text x={9} y={9} fontSize="9.5" fontWeight="700"
                      fill={TH_X.text} fontFamily="monospace">{c.text}</text>
                  </g>
                ))}
              </g>
            )}

            {/* Hit columns per union bucket: the lift is the position indicator and the mark. */}
            {hitBuckets.map((ts, i) => <rect data-hover-target="" key={'h' + i}
              x={xs(ts)} y={padT} width={xs(ts + bucketMs) - xs(ts)}
              height={plotH} fill="#fff"
              fillOpacity={tip && tip.bi === i ? 0.12 : 0} />)}
          </svg>
          {a11y.descText && (
            <span className="sr-only" id={a11y.descId}>{a11y.descText}</span>
          )}
          {tip && <window.DashTooltip tip={tip} />}
        </div>
      )}
    </div>
  );
}

window.ToolErrorRatePanel = ToolErrorRatePanel;
