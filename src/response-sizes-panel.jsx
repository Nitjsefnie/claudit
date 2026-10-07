// ──────────────────────────────────────────────────────────────────────
// Response Sizes panel — visible-text-character daily-bucketed time
// series per model. Each line = that model's daily median chars in
// `text` content blocks; dashed line = p90. Log y-axis (response sizes
// span 4+ orders of magnitude). Chars (not output_tokens) because
// output_tokens silently includes thinking, and per-model thinking
// shares vary 0.7%–25% — token-based percentiles would conflate
// "longer responses" with "more thinking".
//
// Moved out of dashboard-charts-extra.jsx into its own module (#690):
// that file sits at its measured size entry, and an outgrown rework
// moves code into a new module (the #652/#659 pattern). #690's marks
// live here: one hit circle per DISTINCT median position, so models
// plotting the same median at the same ts stay one reachable dot.
// ──────────────────────────────────────────────────────────────────────
const TH_X = window.dashboardTheme;
// Render-time bindings into the shared modules (load order puts
// dashboard-charts-extra.jsx first; these resolve at first render).
const LegendCheckboxRow = window.LegendCheckboxRow;
const extendBucketSeries = window.extendBucketSeries;

function ResponseSizesPanel({ data, bucketS }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(1200);
  const [tip, setTip] = React.useState(null);
  const [yLabelPx, setYLabelPx] = React.useState(0);

  // Widest rendered y label, so the gutter tracks the labels instead of
  // being a fixed budget that a wider decade silently consumes.
  React.useLayoutEffect(() => {
    if (!ref.current) return;
    let m = 0;
    ref.current.querySelectorAll('text[data-yl-label]').forEach(e => {
      const len = e.getComputedTextLength ? e.getComputedTextLength() : 0;
      if (len > m) m = len;
    });
    if (m > 0 && Math.abs(m - yLabelPx) > 0.5) setYLabelPx(m);
  });
  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  // Build per-model series (sorted by ts ascending). Drop <synthetic>.
  const series = React.useMemo(() => {
    const drop = (k) => k === '<synthetic>' || k === 'synthetic';
    const out = new Map();
    for (const d of data || []) {
      if (!(d.n > 0)) continue;
      const key = shortModelName(d.model);
      if (drop(key)) continue;
      const ts = Date.parse(d.ts);
      if (isNaN(ts)) continue;
      if (!out.has(key)) out.set(key, []);
      out.get(key).push({ ts, n: d.n, p50: d.p50, p90: d.p90 });
    }
    const result = [];
    const halfMs = ((bucketS || 86400) * 1000) / 2;
    for (const [key, points] of out) {
      points.sort((a, b) => a.ts - b.ts);
      const n = points.reduce((s, p) => s + p.n, 0);
      const extended = extendBucketSeries(
        points, halfMs, ['p50', 'p90'], { log: true, min: 0 }
      );
      result.push({ key, points: extended, n });
    }
    result.sort((a, b) => b.n - a.n);
    return result;
  }, [data, bucketS]);

  // All models on by default, user can toggle any off.
  const [overrides, setOverrides] = React.useState({});
  const sel = React.useMemo(() => {
    const s = new Set(series.map(m => m.key));
    for (const [k, on] of Object.entries(overrides)) {
      if (on) s.add(k); else s.delete(k);
    }
    return s;
  }, [series, overrides]);
  function toggle(k) {
    setOverrides(prev => ({ ...prev, [k]: !sel.has(k) }));
  }
  const visible = series.filter(m => sel.has(m.key));

  // X-domain: union of all visible timestamps. Y-domain: log of p90 max.
  let tMin = Infinity, tMax = -Infinity, yMaxRaw = 1;
  for (const s of visible) {
    for (const p of s.points) {
      if (p.ts < tMin) tMin = p.ts;
      if (p.ts > tMax) tMax = p.ts;
      if (p.p90 > yMaxRaw) yMaxRaw = p.p90;
    }
  }
  if (!isFinite(tMin) || !isFinite(tMax) || tMin === tMax) {
    tMin = Date.now() - 24 * 3600 * 1000;
    tMax = Date.now();
  }
  const yMin = 1;
  const yMax = Math.max(10, yMaxRaw * 1.2);
  const logYMin = Math.log10(yMin);
  const logYMax = Math.log10(yMax);

  // padL tracks the y labels (anchored at padL - 9) so they stay clear of
  // the rotated "visible chars (log)" caption, whose box ends near x=17.
  const padR = 30, padT = 16, padB = 30;
  const padL = Math.min(
    Math.max(56, w * 0.15),
    Math.max(56, Math.ceil(yLabelPx) + 32)
  );
  const h = 280;
  const plotW = Math.max(20, w - padL - padR);
  const plotH = h - padT - padB;
  const xScale = ts => padL + ((ts - tMin) / Math.max(1, tMax - tMin)) * plotW;
  const yScale = v => padT + plotH - ((Math.log10(Math.max(yMin, v)) - logYMin) / (logYMax - logYMin)) * plotH;

  // Y-axis decade ticks.
  const yTicks = [];
  for (let p = Math.ceil(logYMin); p <= Math.floor(logYMax); p++) yTicks.push(Math.pow(10, p));

  // X-axis: adaptive labels (UTC).
  const xTicks = window.timeTicksUTC(tMin, tMax);

  function onMove(e) {
    // Use the SVG's own bounding rect — the panel wraps the SVG in a
    // nested div, so the outer container ref would offset by header +
    // checkbox row heights and break the hit-test entirely.
    const rect = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    if (mx < padL || mx > w - padR || my < padT || my > padT + plotH) {
      setTip(null); return;
    }
    // Hit-test against the LINES (not just discrete day points) so
    // hovering between two daily buckets snaps to the model's
    // interpolated value at the cursor's x. Linear interp in log-y
    // space matches what's drawn (the polyline between two log-mapped
    // points is a straight line in screen space).
    let best = null, bestD = 1e9, bestKey = null;
    for (const s of visible) {
      const pts = s.points;
      if (!pts.length) continue;
      // Skip this model entirely when the cursor is outside its real
      // data x-range (the polyline only spans first→last point — past
      // those, the line doesn't exist, so we shouldn't hover it).
      const firstX = xScale(pts[0].ts);
      const lastX  = xScale(pts[pts.length - 1].ts);
      if (mx < firstX - 2 || mx > lastX + 2) continue;
      // Find the segment whose x-range contains mx.
      let i = 0;
      // <= so the scan lands ON the point when the cursor sits at its
      // exact x (the hit circles hover their own centres, #690).
      while (i < pts.length - 1 && xScale(pts[i + 1].ts) <= mx) i++;
      const a = pts[i];
      const b = pts[Math.min(i + 1, pts.length - 1)];
      const ax = xScale(a.ts), bx = xScale(b.ts);
      const t = (a === b || bx === ax) ? 0 : Math.max(0, Math.min(1, (mx - ax) / (bx - ax)));
      const ts   = a.ts  + t * (b.ts  - a.ts);
      const lerpLog = (av, bv) => {
        const la = Math.log10(Math.max(1, av));
        const lb = Math.log10(Math.max(1, bv));
        return Math.pow(10, la + t * (lb - la));
      };
      const p50 = lerpLog(a.p50, b.p50);
      const p90 = lerpLog(a.p90, b.p90);
      const n   = Math.round(a.n + t * (b.n - a.n));
      const py = yScale(p50);
      const d = Math.abs(py - my);  // X is exactly at cursor, so just Y distance
      if (d < bestD) {
        bestD = d;
        bestKey = s.key;
        best = { ts, p50, p90, n, bi: i };
      }
    }
    if (!best || bestD > 32) { setTip(null); return; }
    const fmt = window.humanFmt;
    // model/bi key the point's own hit circle (#690): the datum the tip
    // snapped to lights up.
    setTip({
      x: mx, y: my, cx: xScale(best.ts),
      model: bestKey, bi: best.bi,
      title: bestKey + ' · ' + new Date(best.ts).toISOString().slice(0, 10),
      accent: (window.modelColors && window.modelColors[bestKey]) || '#888',
      lines: [
        ['responses', best.n.toLocaleString()],
        ['median',    fmt(Math.round(best.p50))],
        ['p90',       fmt(Math.round(best.p90))],
      ],
    });
  }

  const a11y = window.useChartA11y(
    'Response Sizes',
    `daily median + p90 lines, ${visible.length} models, log scale`,
    visible.length
      ? `Models shown: ${visible.map(s => s.key).join(', ')}.`
      : null);
  return (
    <div ref={ref} style={{
      background: TH_X.bgAxes, border: `1px solid ${TH_X.border}`,
      borderRadius: 4, padding: 0, position: 'relative',
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ padding: '10px 14px 4px', borderBottom: `1px solid ${TH_X.border}` }}>
        <div style={{ color: TH_X.text, fontFamily: 'monospace', fontWeight: 700, fontSize: 14 }}>
          Response Sizes by Model
        </div>
        <div style={{ color: TH_X.textDim, fontFamily: 'monospace', fontSize: 10, marginTop: 2 }}>
          daily median + p90 of visible response characters (text blocks; thinking excluded) · log y-axis · solid = median, dashed = p90
        </div>
      </div>

      <div data-role="legend" style={{
        padding: '8px 14px', borderTop: `1px solid ${TH_X.border}`,
        display: 'flex', flexWrap: 'wrap', gap: '14px 14px',
        fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim,
        order: 99,
      }}>
        <span>show:</span>
        {series.map(m => {
          const c = (window.modelColors && window.modelColors[m.key]) || '#888';
          const checked = sel.has(m.key);
          return (
            <LegendCheckboxRow key={m.key} id={m.key} color={c} checked={checked}
              onToggle={toggle} name={m.key} count={m.n.toLocaleString()} />
          );
        })}
        {!series.length && <span>no responses in range</span>}
      </div>

      <div style={{ position: 'relative' }} onMouseMove={onMove} onMouseLeave={() => setTip(null)}>
        <svg role="img" aria-label={a11y.label} aria-describedby={a11y.descId}
          data-panel="Response Sizes" width={w} height={h} style={{ display: 'block' }}>
          {/* Y grid */}
          <rect data-role="plot" x={padL} y={padT} width={plotW} height={plotH} fill="none" />{yTicks.map((v, i) => (
            <line key={'g'+i} x1={padL} x2={w - padR}
              y1={yScale(v)} y2={yScale(v)}
              stroke={TH_X.grid} strokeOpacity="0.25" />
          ))}

          {/* Lines per visible model — p90 dashed underneath, median on top */}
          {visible.map(s => {
            const c = (window.modelColors && window.modelColors[s.key]) || '#888';
            const ptsP90 = s.points
              .filter(p => p.p90 > 0)
              .map(p => `${xScale(p.ts)},${yScale(p.p90)}`).join(' ');
            return (
              <polyline key={'p90-'+s.key} points={ptsP90}
                stroke={c} strokeWidth="1.1" strokeDasharray="4,3"
                strokeOpacity="0.7" fill="none" />
            );
          })}
          {visible.map(s => {
            const c = (window.modelColors && window.modelColors[s.key]) || '#888';
            const ptsP50 = s.points
              .filter(p => p.p50 > 0)
              .map(p => `${xScale(p.ts)},${yScale(p.p50)}`).join(' ');
            return (
              <polyline key={'p50-'+s.key} points={ptsP50}
                stroke={c} strokeWidth="1.8" fill="none" />
            );
          })}

          {/* Hit circles, one per DISTINCT median position (#690):
              invisible at rest, the dot under the pointer lights. The
              lighting keys on the CURSOR sitting inside the dot — models
              tie at the same median (the tip names whichever series won
              the nearest-line race), so keying the dot on the tip would
              light only the first-ordered model's dot and leave the one
              under the pointer dark. One circle per position, because
              coincident points are one dot on screen; a per-point circle
              would stack thirty deep with only the top one reachable. */}
          {(() => {
            const dots = new Map();
            for (const s of visible) {
              for (const p of s.points) {
                if (p.p50 <= 0) continue;
                const k = `${p.ts}|${p.p50}`;
                if (!dots.has(k)) {
                  dots.set(k, {
                    ts: p.ts, p50: p.p50,
                    color: (window.modelColors
                      && window.modelColors[s.key]) || '#888',
                  });
                }
              }
            }
            return [...dots.values()].map(d => (
              <circle key={`hit-${d.ts}-${d.p50}`} data-hover-target=""
                cx={xScale(d.ts)} cy={yScale(d.p50)} r="4"
                fill={d.color}
                fillOpacity={tip && Math.hypot(tip.x - xScale(d.ts),
                  tip.y - yScale(d.p50)) <= 4.5 ? 0.85 : 0}
                stroke="none" />
            ));
          })()}

          {/* Crosshair */}
          {tip && (
            <line x1={tip.cx} x2={tip.cx} y1={padT} y2={padT + plotH}
              stroke="#fff" strokeOpacity="0.3" strokeDasharray="2,3" />
          )}

          {/* Y labels */}
          <g data-role="axis">{yTicks.map((v, i) => (
            <text data-yl-label="" key={'yl'+i} x={padL - 9} y={yScale(v) + 3}
              fontSize="9" fill={TH_X.textDim} textAnchor="end" fontFamily="monospace">
              {window.humanFmt(v)}
            </text>
          ))}</g>
          {/* X labels */}
          <g data-role="axis">{xTicks.map((t, i) => (
            <text key={'xl'+i} x={xScale(t.ts)} y={h - padB + 14}
              fontSize="9" fill={TH_X.textDim} textAnchor="middle" fontFamily="monospace">
              {t.label}
            </text>
          ))}</g>
          <text data-role="axis" x={14} y={padT + plotH/2} fontSize="9" fill={TH_X.textDim}
            textAnchor="middle" fontFamily="monospace"
            transform={`rotate(-90 14 ${padT + plotH/2})`}>visible chars (log)</text>
        </svg>
        {a11y.descText && (
          <span className="sr-only" id={a11y.descId}>{a11y.descText}</span>
        )}
        {tip && <window.DashTooltip tip={tip} />}
      </div>
    </div>
  );
}

window.ResponseSizesPanel = ResponseSizesPanel;
