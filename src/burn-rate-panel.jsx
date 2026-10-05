// The Session Burn Rate panel — a scatter of sessions (dot area scaled by
// context at session end) under per-series EMA lines of tokens per hour.
//
// Split out of dashboard-charts.jsx (#634/#635): the phone-width layout
// fixes this module carries grew the source file past its committed size
// entry, and the size ratchet's rule is that an outgrown file moves code
// into a new module. Nothing about the panel changed but where it lives
// (the same split as context-growth-comparison.jsx, #630) — except the
// two fixes themselves:
//
//   * The heading is two lines (#635): the bold panel name on line one,
//     the dates and counts on a dimmed subtitle beneath, elided by
//     fitText where even the subtitle is wider than the panel. The old
//     single centred line was ~608px and overflowed both panel edges at
//     375 and 320px.
//   * The legend is packed BEFORE the plot box is sized (#634): padB is
//     one 16px band per packed legend row and the panel grows downward by
//     the same rows, so a five-row legend at phone width no longer slides
//     its lower rows up into the plot and across the x-tick labels.
//
// Shared values are captured at module scope under _B names: the
// text/babel scripts share one global lexical scope, so a plain `TH_B`
// would collide with dashboard-charts.jsx's own (the reason the
// comparison panel's copies end in _X).
const TH_B = window.dashboardTheme;
const MODEL_COLORS_B = window.modelColors;
const humanFmt_B = window.humanFmt;
const fmtDate_B = window.fmtDate;
const timeTicksUTC_B = window.timeTicksUTC;
const Tooltip_B = window.Tooltip;
const useChartA11y_B = window.useChartA11y;
// --- Burn rate panel ---
function BurnRatePanel({ events, sessions, limitHits, range: propRange, windowBoundaries }) {
  const ref = React.useRef(null);
  const [size, setSize] = React.useState({ w: 1200, h: 360 });
  const [tip, setTip] = React.useState(null);
  const [legendAdv, setLegendAdv] = React.useState(0);
  const [yLabelPx, setYLabelPx] = React.useState(0);

  // Per-character advance of the legend's 10px monospace, plus the widest
  // rendered y label — both measured from nodes that actually painted. Same
  // reasoning as the axis gutters elsewhere: a predicted advance disagrees
  // with the real one because app.css adds letter-spacing that measurement
  // APIs outside the SVG don't see.
  React.useLayoutEffect(() => {
    if (!ref.current) return;
    const t = ref.current.querySelector('text[data-legend-item]');
    if (t && t.getComputedTextLength) {
      const n = (t.textContent || '').length;
      if (n) {
        const a = t.getComputedTextLength() / n;
        if (a > 0 && Math.abs(a - legendAdv) > 0.05) setLegendAdv(a);
      }
    }
    let m = 0;
    ref.current.querySelectorAll('text[data-yl-label]').forEach(e => {
      const len = e.getComputedTextLength ? e.getComputedTextLength() : 0;
      if (len > m) m = len;
    });
    if (m > 0 && Math.abs(m - yLabelPx) > 0.5) setYLabelPx(m);
  });

  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => {
      const r = es[0].contentRect;
      setSize({ w: r.width, h: r.height });
    });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const { w, h } = size;

  // The prop range is derived from hourly bucket MIDPOINTS, so it
  // doesn't match what this chart plots (raw session start/end + limit
  // hits). Derive a data-envelope range here so sessions never escape
  // the plot box on the left and the chart never trails into empty
  // space on the right.
  const range = (() => {
    let lo = Infinity, hi = -Infinity;
    // Sessions (dots + EMA polylines) are plotted at midpoints, so the
    // range that makes them fill the plot is the min/max of midpoints.
    for (const s of sessions) {
      const mid = (s.start + s.end) / 2;
      if (mid < lo) lo = mid;
      if (mid > hi) hi = mid;
    }
    for (const lh of limitHits) { if (lh.ts < lo) lo = lh.ts; if (lh.ts > hi) hi = lh.ts; }
    if (lo === Infinity || lo === hi) return propRange;
    return { start: lo, end: hi };
  })();
  // Top is the two-line heading (#635): the bold name, then the dimmed
  // dates+counts subtitle at baseline 30, so padT clears both. Bottom is
  // the x-tick labels plus the legend, packed before the plot box is
  // sized (the packing sits after `series`): padB is one 16px band per
  // legend row.
  // padL sized from the measured y labels, not fixed at 60. Labels are
  // end-anchored at padL - 8 and the rotated "Tokens per hour…" caption
  // occupies roughly x 8..22, so the widest label needs padL - 8 - width to
  // stay clear of it; +40 keeps ~10px. At the old fixed 60 the current
  // widest tick ("100K", 25.6px) left only 4.4px — the same shape as the
  // 100M/cumulative collision on the right gutter, one character from
  // breaking.
  const padR = 30, padT = 40;
  const padL = Math.min(
    Math.max(60, w * 0.2),
    Math.max(60, Math.ceil(yLabelPx) + 40)
  );

  // EMA + polyline rendering assume time-sorted sessions; the backend
  // returns them in cost-desc order, so re-sort by midpoint ascending.
  const sortedSessions = sessions.slice().sort((a, b) => {
    const am = (a.start + a.end) / 2;
    const bm = (b.start + b.end) / 2;
    return am - bm;
  });
  const sessionData = sortedSessions.map((s, i) => {
    const dur = (s.end - s.start) / 3600000;
    const durH = Math.max(dur, 1/60);
    const sums = { input: 0, output: 0, cc: 0, cr: 0, cost: 0 };
    const modelCounts = {};
    for (const e of s.events) {
      sums.input += e.input_tokens;
      sums.output += e.output_tokens;
      sums.cc += e.cache_create;
      sums.cr += e.cache_read;
      sums.cost += e.cost_usd;
      modelCounts[e.model] = (modelCounts[e.model] || 0) + 1;
    }
    let primary = 'opus-4-6', max = 0;
    for (const [m, c] of Object.entries(modelCounts)) if (c > max) { max = c; primary = m; }
    return {
      idx: i,
      start: s.start, end: s.end,
      mid: (s.start + s.end) / 2,
      durH,
      reqs: (s.requests != null) ? s.requests : s.events.length,
      ctxEnd: s.ctxEnd != null ? s.ctxEnd : null,
      primary,
      sums,
      out_per_h:    sums.output / durH,
      input_per_h:  sums.input / durH,
      cc_per_h:     sums.cc / durH,
      cr_per_h:     sums.cr / durH,
      // Cost/h scaled by 100 so dots share the EMA's tokens-per-hour
      // log axis without needing a second scale: $1/h ≈ 100 tok/h ≈ same
      // visual band. Y-axis label calls out the dual meaning.
      cost_per_h_x100: (sums.cost / durH) * 100,
    };
  });

  function ema(arr, alpha = 0.15) {
    if (!arr.length) return [];
    const out = [arr[0]];
    for (let i = 1; i < arr.length; i++) out.push(alpha * arr[i] + (1 - alpha) * out[i-1]);
    return out;
  }

  const series = {
    output: { color: '#ee4444', label: 'Output', vals: ema(sessionData.map(s => s.out_per_h)) },
    input:  { color: '#44dd66', label: 'Input',  vals: ema(sessionData.map(s => s.input_per_h)) },
    cc:     { color: '#dd66aa', label: 'Cache Create', vals: ema(sessionData.map(s => s.cc_per_h)) },
    cr:     { color: '#44bbbb', label: 'Cache Read',   vals: ema(sessionData.map(s => s.cr_per_h)) },
  };

  // The legend is packed BEFORE the plot box is sized (#634): padB below
  // is one 16px band per packed legend row, and the panel div grows by the
  // same rows, so a legend that wraps to five rows at phone width grows
  // the panel downward instead of sliding its lower rows up into the plot
  // and across the x-tick labels. Same arithmetic the JSX used to run
  // inline; it moved up here so the pad it sizes exists before plotH.
  const legendItems = Object.entries(series).map(([k, s]) => (
    { key: k, color: s.color, label: `${s.label} (EMA)` }));
  legendItems.push({ key: '__ratelimit', color: '#ff3366', label: 'Rate limit hit' });
  const legendAdvUsed = legendAdv || 6.4;
  const LEGEND_SWATCH = 20, LEGEND_GAP = 6, LEGEND_SPACING = 24, LEGEND_ROW = 16;
  // Wrap instead of running off the panel: at a 800px viewport the
  // five entries need ~680px against ~630px of usable width, and an
  // unwrapped row put text 23px outside the svg.
  const legendAvail = Math.max(120, w - (padL + 20) - padR);
  let legendCx = 0, legendRow = 0;
  const legendPlaced = legendItems.map(it => {
    const wEntry = LEGEND_SWATCH + LEGEND_GAP + it.label.length * legendAdvUsed
      + LEGEND_SPACING;
    if (legendCx > 0 && legendCx + wEntry > legendAvail) { legendRow += 1; legendCx = 0; }
    const at = legendCx, row = legendRow;
    legendCx += wEntry;
    return { ...it, at, row };
  });
  const legendRows = legendRow + 1;
  const padB = 56 + (legendRows - 1) * LEGEND_ROW;
  const plotW = Math.max(10, w - padL - padR);
  const plotH = Math.max(10, h - padT - padB);

  // The heading's second line (#635): the dates and counts, dimmed under
  // the bold name. Elided by fitText where even the subtitle is wider
  // than the panel — the single centred line this replaces was ~608px and
  // overflowed both panel edges at 375 and 320px. Its advance is the
  // measured legend advance (both are 10px monospace), never a predicted
  // one.
  const burnSub = `${fmtDate_B(range.start, {day:true})} – ${fmtDate_B(range.end, {day:true})}, ${new Date(range.end).getUTCFullYear()} UTC  |  `
    + `${sessions.length.toLocaleString()} sessions, ${events.reduce((s,e)=>s+(e.requests==null?1:e.requests),0).toLocaleString()} requests`;

  // Densify each EMA line: linearly interpolate between session midpoints
  // so hit-testing works along the whole curve, not just at session points.
  const DENSE_STEPS = 32; // sub-points per segment
  function densify(vals) {
    const dense = [];
    if (sessionData.length === 0) return dense;
    if (sessionData.length === 1) {
      dense.push({ ts: sessionData[0].mid, val: vals[0], srcIdx: 0, t: 0 });
      return dense;
    }
    for (let i = 0; i < sessionData.length - 1; i++) {
      const a = sessionData[i], b = sessionData[i + 1];
      const va = vals[i], vb = vals[i + 1];
      for (let s = 0; s < DENSE_STEPS; s++) {
        const t = s / DENSE_STEPS;
        dense.push({
          ts: a.mid + (b.mid - a.mid) * t,
          val: va + (vb - va) * t,
          srcIdx: t < 0.5 ? i : i + 1,
          t,
        });
      }
    }
    const last = sessionData.length - 1;
    dense.push({ ts: sessionData[last].mid, val: vals[last], srcIdx: last, t: 0 });
    return dense;
  }
  const densified = {};
  for (const k of Object.keys(series)) densified[k] = densify(series[k].vals);

  // Axis range covers BOTH the EMA lines (tokens/h) AND the dot positions
  // (cost/h × 100), so pull both sets of values into the min/max.
  let allRates = [];
  for (const k of Object.keys(series)) allRates = allRates.concat(series[k].vals);
  for (const s of sessionData) allRates.push(s.cost_per_h_x100);
  allRates = allRates.filter(v => v > 0);
  const yMin = Math.max(1, Math.min(...allRates) * 0.3);
  const yMax = Math.max(...allRates) * 3;
  const logYMin = Math.log10(yMin), logYMax = Math.log10(yMax);
  const xScale = ts => padL + ((ts - range.start) / (range.end - range.start)) * plotW;
  const yScale = v => {
    // Clamp to [yMin, yMax] so out-of-range sessions sit on the plot
    // edge instead of leaking out the bottom into the legend strip
    // (`yMin * 0.1` floor caused the negative-fraction overshoot).
    const cv = Math.max(yMin, Math.min(yMax, v));
    return padT + plotH - ((Math.log10(cv) - logYMin) / (logYMax - logYMin)) * plotH;
  };

  const yTicks = [];
  for (let p = Math.ceil(logYMin); p <= Math.floor(logYMax); p++) yTicks.push(Math.pow(10, p));

  const xTicks = timeTicksUTC_B(range.start, range.end);

  // Find nearest session dot to cursor
  function onMove(e) {
    const rect = ref.current.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    if (mx < padL || mx > w - padR || my < padT || my > padT + plotH) {
      setTip(null); return;
    }
    let best = null, bestD = 1e9;
    for (const s of sessionData) {
      const sx = xScale(s.mid), sy = yScale(s.cost_per_h_x100);
      const d = Math.hypot(sx - mx, sy - my);
      if (d < bestD) { bestD = d; best = s; }
    }
    // Also check rate limits (vertical bands)
    let nearLimit = null;
    for (const lh of limitHits) {
      const lx = xScale(lh.ts);
      if (Math.abs(lx - mx) < 5) nearLimit = lh;
    }
    if (nearLimit) {
      setTip({ x: mx, y: my, title: 'Rate limit hit', accent: '#ff3366',
        lines: [['when', fmtDate_B(nearLimit.ts, {full:true}) + ' UTC']] });
      return;
    }
    // Check proximity to EMA lines (output/input/cache create/cache read).
    // Use the densified curves so hover works along the whole line, not
    // only where session points exist.
    if (sessionData.length > 0) {
      let bestSeriesKey = null, bestSeriesD = 1e9, bestPoint = null;
      for (const k of Object.keys(series)) {
        const dense = densified[k];
        for (const p of dense) {
          const px = xScale(p.ts);
          if (Math.abs(px - mx) > 30) continue; // cheap reject
          const py = yScale(p.val);
          const d = Math.hypot(px - mx, py - my);
          if (d < bestSeriesD) { bestSeriesD = d; bestSeriesKey = k; bestPoint = p; }
        }
      }
      // Prefer line over dot when line is significantly closer
      const dotD = best ? Math.hypot(xScale(best.mid)-mx, yScale(best.cost_per_h_x100)-my) : 1e9;
      if (bestSeriesKey && bestSeriesD < 14 && bestSeriesD < dotD - 4) {
        const sk = series[bestSeriesKey];
        const sAtCol = sessionData[bestPoint.srcIdx];
        const raw = {
          output: sAtCol.out_per_h,
          input:  sAtCol.input_per_h,
          cc:     sAtCol.cc_per_h,
          cr:     sAtCol.cr_per_h,
        }[bestSeriesKey];
        setTip({
          x: mx, y: my,
          title: sk.label + ' (EMA)',
          accent: sk.color,
          lines: [
            ['nearest sess', '#' + (sAtCol.idx + 1) + ' / ' + sessionData.length],
            ['when',         fmtDate_B(bestPoint.ts, {full:true})],
            ['model',        sAtCol.primary],
            ['EMA tok/hr',   humanFmt_B(bestPoint.val)],
            ['raw tok/hr',   humanFmt_B(raw)],
          ],
        });
        return;
      }
    }
    if (best && bestD < 30) {
      const ctxKnown = best.ctxEnd != null;
      const areaPts2 = ctxKnown
        ? Math.min(Math.max(best.ctxEnd / 4000, 25), 250)
        : 16;
      const dotR = Math.sqrt(areaPts2);
      const sizeNote = ctxKnown
        ? `${dotR.toFixed(1)}px (ctx ${humanFmt_B(best.ctxEnd)})`
        : `${dotR.toFixed(1)}px (ctx unknown)`;
      setTip({
        x: mx, y: my,
        title: 'Session ' + (best.idx + 1),
        accent: MODEL_COLORS_B[best.primary] || '#888',
        lines: [
          ['model',         best.primary],
          ['start',         fmtDate_B(best.start, {full:true})],
          ['duration',      best.durH < 1 ? (best.durH*60).toFixed(0)+'m' : best.durH.toFixed(1)+'h'],
          ['requests',      String(best.reqs)],
          ['ctx at end',    ctxKnown ? humanFmt_B(best.ctxEnd) : 'unknown'],
          ['out tok/hr',    humanFmt_B(best.out_per_h)],
          ['cache rd tok/hr', humanFmt_B(best.cr_per_h)],
          ['cost / hour',   '$' + (best.cost_per_h_x100 / 100).toFixed(2) + '/h'],
          ['est. cost',     '$' + best.sums.cost.toFixed(2)],
          ['dot radius',    sizeNote],
        ],
      });
    } else {
      setTip(null);
    }
  }

  const a11y = useChartA11y_B(
    'Session Burn Rate',
    `scatter, ${sessionData.length} sessions`,
    `Each dot is one session, its area scaled by context at session end; `
    + `open dashed dots have unknown context. `
    + `${limitHits.length} rate-limit ${limitHits.length === 1 ? 'line' : 'lines'} marked.`);
  return (
    <div ref={ref} style={{
      background: TH_B.bgAxes, border: `1px solid ${TH_B.border}`,
      borderRadius: 4, padding: 0,
      // The panel grows one legend row tall per wrapped legend row (#634):
      // the legend band lives entirely below the plot, so the wrapped
      // rows expand the panel instead of the legend sliding into it.
      height: 380 + (legendRows - 1) * LEGEND_ROW, position: 'relative',
    }}
    onMouseMove={onMove}
    onMouseLeave={() => setTip(null)}>
      <svg role="img" aria-label={a11y.label} aria-describedby={a11y.descId}
        data-panel="Session Burn Rate" width={w} height={h} style={{ display: 'block' }}>
        <defs>
          <clipPath id="burn-plot-clip">
            <rect x={padL} y={padT} width={plotW} height={plotH} />
          </clipPath>
        </defs>
        <text data-role="title" x={w/2} y={16} fontSize="14" fontWeight="bold" fill={TH_B.text}
          textAnchor="middle" fontFamily="monospace">
          {window.panelLayout.fitText('Session Burn Rate', legendAdvUsed, Math.max(60, w - 24))}
        </text>
        <text data-role="title" x={w/2} y={30} fontSize="10" fill={TH_B.textDim}
          textAnchor="middle" fontFamily="monospace">
          {window.panelLayout.fitText(burnSub, legendAdvUsed, Math.max(60, w - 24))}
        </text>
        <g data-role="axis">{yTicks.map((v, i) => (
          <text data-yl-label="" key={'yl'+i} x={padL - 8} y={yScale(v) + 4}
            fontSize="10" fill={TH_B.textDim} textAnchor="end" fontFamily="monospace">
            {humanFmt_B(v)}
          </text>
        ))}</g><rect data-role="plot" x={padL} y={padT} width={plotW} height={plotH} fill="none" />
        <g clipPath="url(#burn-plot-clip)">
        {windowBoundaries.map((wb, i) => (
          <line key={'wb'+i} x1={xScale(wb)} x2={xScale(wb)}
            y1={padT} y2={padT + plotH}
            stroke="#fff" strokeOpacity="0.1" strokeWidth="1" strokeDasharray="2,3" />
        ))}
        {yTicks.map((v, i) => (
          <line key={'yg'+i} x1={padL} x2={w-padR}
            y1={yScale(v)} y2={yScale(v)}
            stroke={TH_B.grid} strokeOpacity="0.25" />
        ))}
        {sessionData.map((s, i) => {
          // Scale dot AREA by ctx-at-end-of-session.
          //   100k ctx → 25 area-pts²,  1M ctx → 250 area-pts²
          // When ctxEnd is null (empty ctx_turns),
          // render a fixed small open circle instead of the old durH × 60
          // duration fallback — that fallback collapsed every kvalita
          // subagent-only / synthetic-trailing session to either max-r or
          // a meaningless duration-scaled size.
          const ctxKnown = s.ctxEnd != null;
          const areaPts2 = ctxKnown
            ? Math.min(Math.max(s.ctxEnd / 4000, 25), 250)
            : 16; // r ≈ 4 px sentinel for ctx-unknown
          const r = Math.sqrt(areaPts2);
          const isHover = tip && tip.title === 'Session ' + (s.idx + 1);
          return (
            <circle key={'sd'+i} cx={xScale(s.mid)} cy={yScale(s.cost_per_h_x100)}
              r={isHover ? r + 2 : r}
              fill={ctxKnown ? (MODEL_COLORS_B[s.primary] || '#888') : 'none'}
              fillOpacity={isHover ? 0.95 : 0.5}
              stroke={ctxKnown ? '#fff' : (MODEL_COLORS_B[s.primary] || '#888')}
              strokeOpacity={isHover ? 0.9 : (ctxKnown ? 0.3 : 0.85)}
              strokeWidth={isHover ? 1.5 : (ctxKnown ? 0.5 : 1.2)}
              strokeDasharray={ctxKnown ? undefined : '2,2'} />
          );
        })}
        {Object.entries(series).map(([k, s]) => {
          const pts = densified[k].map(p => `${xScale(p.ts)},${yScale(p.val)}`).join(' ');
          return <polyline key={k} points={pts}
            stroke={s.color} strokeWidth="1.5" fill="none" strokeOpacity="0.85" />;
        })}
        {limitHits.map((lh, i) => (
          <line key={'lh'+i} x1={xScale(lh.ts)} x2={xScale(lh.ts)}
            y1={padT} y2={padT + plotH}
            stroke="#ff3366" strokeWidth="2" strokeOpacity="0.7" />
        ))}
        </g>
        <g data-role="axis">{xTicks.map((t, i) => (
          <text key={'x'+i} x={xScale(t.ts)} y={h - padB + 14}
            fontSize="10" fill={TH_B.textDim} textAnchor="middle" fontFamily="monospace">
            {t.label}
          </text>
        ))}</g>
        {/* x=18 not 14: rotated text's box extends about one ascent to the
            left of its baseline, so at 14 it began 3.5px from the edge. */}
        <text data-role="axis" x={18} y={padT + plotH/2} fontSize="10" fill={TH_B.textDim}
          textAnchor="middle" fontFamily="monospace"
          transform={`rotate(-90 18 ${padT + plotH/2})`}>Tokens per hour (EMA) / 100 × Cost per hour</text>

        {/* Legend entries come from the packing that ran above, next to
            the pad it sizes (#634): one 16px band per wrapped row. The
            cumulative per-entry advance and the 16px row are that block's;
            the swatch/gap/spacing constants moved up with it. */}
        <g transform={`translate(${padL + 20}, ${h - 22 - (legendRows - 1) * LEGEND_ROW})`}>
          {legendPlaced.map(it => (
            <g data-role="legend" key={it.key} transform={`translate(${it.at}, ${it.row * LEGEND_ROW})`}>
              <line x1={0} x2={LEGEND_SWATCH} y1={6} y2={6} stroke={it.color} strokeWidth="2" />
              <text data-legend-item="" x={LEGEND_SWATCH + LEGEND_GAP} y={10} fontSize="10"
                fill={TH_B.text} fontFamily="monospace">{it.label}</text>
            </g>
          ))}
        </g>
      </svg>
      {a11y.descText && (
        <span className="sr-only" id={a11y.descId}>{a11y.descText}</span>
      )}
      <Tooltip_B tip={tip} />
    </div>
  );
}

window.BurnRatePanel = BurnRatePanel;
