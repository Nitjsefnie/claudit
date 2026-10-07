// ──────────────────────────────────────────────────────────────────────
// Reply Latency panel — per-(bucket, model) p10–p90 band + median line,
// plus scatter dots for top-1% slowest + bottom-1% fastest replies
// per bucket (when bucket_n >= 100). Log y-axis (latency 0.5s–400s).
//
// Moved out of dashboard-charts-extra.jsx into its own module: that file
// sits at its measured size entry, and an outgrown rework moves code
// into a new module (the #652/#659 pattern). #690's marks live here:
// every median point is a `data-hover-target` — an invisible hit circle
// that lights at the datum when the panel's tooltip snaps to it — so
// the interaction sweep hovers the panel's points, not just its bars.
// ──────────────────────────────────────────────────────────────────────
const TH_X = window.dashboardTheme;
// Render-time bindings into the shared modules (load order puts
// dashboard-charts-extra.jsx first; these resolve at first render).
const LegendCheckboxRow = window.LegendCheckboxRow;
const extendBucketSeries = window.extendBucketSeries;

function ReplyLatencyPanel({ project, range, nonce, models }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(1200);
  const [tip, setTip] = React.useState(null);
  const [bands, setBands] = React.useState([]);
  const [outliers, setOutliers] = React.useState([]);
  const [bucketMs, setBucketMs] = React.useState(86_400_000);
  const [activeModel, setActiveModel] = React.useState('');

  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  React.useEffect(() => {
    const q = (project ? `&project=${encodeURIComponent(project)}` : '')
            + (activeModel ? `&model=${encodeURIComponent(activeModel)}` : '');
    fetch(`/api/reply-latency?range=${range || 'all'}${q}`, { credentials: 'same-origin' })
      .then(r => r.json())
      .then(b => {
        setBands(b.bands || []);
        setOutliers(b.outliers || []);
        if (b.bucket_s) setBucketMs(b.bucket_s * 1000);
      })
      .catch(err => console.error('reply-latency fetch failed', err));
  }, [project, range, activeModel, nonce]);

  // Per-model series for the bands.
  const series = React.useMemo(() => {
    const drop = k => k === '<synthetic>' || k === 'synthetic';
    const out = new Map();
    for (const b of bands) {
      const key = shortModelName(b.model);
      if (drop(key)) continue;
      const ts = Date.parse(b.ts);
      if (isNaN(ts)) continue;
      if (!out.has(key)) out.set(key, []);
      out.get(key).push({ ts, n: b.n, p10: b.p10, p50: b.p50, p90: b.p90 });
    }
    const arr = [];
    const half = bucketMs / 2;
    for (const [key, points] of out) {
      points.sort((a, b) => a.ts - b.ts);
      const n = points.reduce((s, p) => s + p.n, 0);
      // Log-space linear extrapolation by half a bucket on each end
      // so the median + p10/p90 band visually span the full bucket
      // extent without flat-carry artifacts.
      const extended = extendBucketSeries(
        points, half, ['p10', 'p50', 'p90'], { log: true, min: 0 }
      );
      arr.push({ key, points: extended, n });
    }
    arr.sort((a, b) => b.n - a.n);
    return arr;
  }, [bands, bucketMs]);

  // All models on by default; uncheck individually.
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

  // Outlier dots filtered by visible models too.
  const visibleKeys = React.useMemo(() => new Set(visible.map(s => s.key)), [visible]);
  const visibleOutliers = React.useMemo(
    () => outliers
      .map(o => ({ ...o, key: shortModelName(o.model), tsMs: Date.parse(o.ts) }))
      .filter(o => !isNaN(o.tsMs) && visibleKeys.has(o.key)),
    [outliers, visibleKeys]
  );

  // Dedup model list for the model select.
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

  // Geometry. Y log-scale, range from 0.1s to max p90 (clamped >= 10s).
  let tMin = Infinity, tMax = -Infinity, yMaxRaw = 1;
  for (const s of visible) {
    for (const p of s.points) {
      if (p.ts < tMin) tMin = p.ts;
      if (p.ts > tMax) tMax = p.ts;
      if (p.p90 > yMaxRaw) yMaxRaw = p.p90;
    }
  }
  for (const o of visibleOutliers) {
    if (o.tsMs < tMin) tMin = o.tsMs;
    if (o.tsMs > tMax) tMax = o.tsMs;
    if (o.latency_s > yMaxRaw) yMaxRaw = o.latency_s;
  }
  if (!isFinite(tMin) || !isFinite(tMax) || tMin === tMax) {
    tMin = Date.now() - 24 * 3600 * 1000;
    tMax = Date.now();
  }
  const yMin = 0.1;
  const yMax = Math.max(10, yMaxRaw * 1.2);
  const logYMin = Math.log10(yMin);
  const logYMax = Math.log10(yMax);

  const padL = 56, padR = 30, padT = 16, padB = 30;
  const h = 320;
  const plotW = Math.max(20, w - padL - padR);
  const plotH = h - padT - padB;
  const xScale = ts => padL + ((ts - tMin) / Math.max(1, tMax - tMin)) * plotW;
  const yScale = v => padT + plotH - ((Math.log10(Math.max(yMin, v)) - logYMin) / (logYMax - logYMin)) * plotH;

  // Y decade ticks.
  const yTicks = [];
  for (let p = Math.ceil(logYMin); p <= Math.floor(logYMax); p++) yTicks.push(Math.pow(10, p));

  // X adaptive labels.
  const xTicks = (isFinite(tMin) && isFinite(tMax)) ? window.timeTicksUTC(tMin, tMax) : [];

  function fmtSecs(s) {
    if (s < 1) return s.toFixed(2) + 's';
    if (s < 60) return s.toFixed(1) + 's';
    if (s < 3600) return (s / 60).toFixed(1) + 'm';
    return (s / 3600).toFixed(1) + 'h';
  }

  function onMove(e) {
    const rect = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    if (mx < padL || mx > w - padR || my < padT || my > padT + plotH) {
      setTip(null); return;
    }
    // Try outlier dots first (small targets, but exact times).
    let bestO = null, bestOD = 1e9;
    for (const o of visibleOutliers) {
      const px = xScale(o.tsMs), py = yScale(o.latency_s);
      const d = Math.hypot(px - mx, py - my);
      if (d < bestOD) { bestOD = d; bestO = o; }
    }
    if (bestO && bestOD < 8) {
      // file_key shape: <project>/<session_id>/<filename>.jsonl —
      // drop the project segment, keep session/filename so the
      // tooltip stays narrow but still uniquely identifies the line.
      const fk = String(bestO.file_key || '');
      const fileShort = fk.split('/').slice(-2).join('/');
      setTip({
        x: mx, y: my, cx: xScale(bestO.tsMs),
        title: 'outlier · ' + bestO.key,
        accent: (window.modelColors && window.modelColors[bestO.key]) || '#888',
        lines: [
          ['latency', fmtSecs(bestO.latency_s)],
          ['when',    new Date(bestO.tsMs).toISOString().slice(0, 19) + 'Z'],
          ['file',    fileShort],
          ['line',    String(bestO.line || '')],
        ],
      });
      return;
    }
    // Else: nearest median line (interpolated).
    let best = null, bestD = 1e9, bestKey = null;
    for (const s of visible) {
      const pts = s.points;
      if (!pts.length) continue;
      const firstX = xScale(pts[0].ts);
      const lastX  = xScale(pts[pts.length - 1].ts);
      if (mx < firstX - 2 || mx > lastX + 2) continue;
      let i = 0;
      // <= so the scan lands ON the point when the cursor sits at its
      // exact x (the hit circles hover their own centres, #690): the
      // interpolation is continuous across the boundary, so only the
      // index changes.
      while (i < pts.length - 1 && xScale(pts[i + 1].ts) <= mx) i++;
      const a = pts[i];
      const b = pts[Math.min(i + 1, pts.length - 1)];
      const ax = xScale(a.ts), bx = xScale(b.ts);
      const t = (a === b || bx === ax) ? 0 : Math.max(0, Math.min(1, (mx - ax) / (bx - ax)));
      const ts = a.ts + t * (b.ts - a.ts);
      const lerpLog = (av, bv) => {
        const la = Math.log10(Math.max(yMin, av));
        const lb = Math.log10(Math.max(yMin, bv));
        return Math.pow(10, la + t * (lb - la));
      };
      const p10 = lerpLog(a.p10, b.p10);
      const p50 = lerpLog(a.p50, b.p50);
      const p90 = lerpLog(a.p90, b.p90);
      const n   = Math.round(a.n + t * (b.n - a.n));
      const py = yScale(p50);
      const d = Math.abs(py - my);
      if (d < bestD) {
        bestD = d; bestKey = s.key; best = { ts, p10, p50, p90, n, bi: i };
      }
    }
    if (!best || bestD > 32) { setTip(null); return; }
    // model/bi key the point's own hit circle (#690): the datum the tip
    // snapped to lights up.
    setTip({
      x: mx, y: my, cx: xScale(best.ts),
      model: bestKey, bi: best.bi,
      title: bestKey + ' · ' + new Date(best.ts).toISOString().slice(0, 10),
      accent: (window.modelColors && window.modelColors[bestKey]) || '#888',
      lines: [
        ['replies', best.n.toLocaleString()],
        ['p10',     fmtSecs(best.p10)],
        ['median',  fmtSecs(best.p50)],
        ['p90',     fmtSecs(best.p90)],
      ],
    });
  }

  const a11y = window.useChartA11y(
    'Reply Latency',
    `p10-p90 bands, ${visible.length} models, log scale`,
    'Reply-latency percentiles per model over time, with outlier dots '
      + 'for the slowest and fastest replies per bucket.');
  return (
    <div ref={ref} style={{
      background: TH_X.bgAxes, border: `1px solid ${TH_X.border}`,
      borderRadius: 4, padding: 0, position: 'relative',
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ padding: '10px 14px 4px', borderBottom: `1px solid ${TH_X.border}`, display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
        <div style={{ flex: 1 }}>
          <div style={{ color: TH_X.text, fontFamily: 'monospace', fontWeight: 700, fontSize: 14 }}>
            Reply Latency over Time
          </div>
          <div style={{ color: TH_X.textDim, fontFamily: 'monospace', fontSize: 10, marginTop: 2 }}>
            user msg → first assistant event · per-(bucket, model) p10–p90 band, median line, top/bottom-1% outlier dots (bucket n ≥ 100) · log y
          </div>
        </div>
        <label style={{ display: 'inline-flex', flexWrap: 'wrap', alignItems: 'center', gap: 6, fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim }}>
          model:
          <select aria-label="Reply Latency model filter"
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
        </label>
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
        {!series.length && <span>no reply-latency data in range</span>}
      </div>

      <div style={{ position: 'relative' }} onMouseMove={onMove} onMouseLeave={() => setTip(null)}>
        <svg role="img" aria-label={a11y.label} aria-describedby={a11y.descId}
          data-panel="Reply Latency" width={w} height={h} style={{ display: 'block' }}>
          <rect data-role="plot" x={padL} y={padT} width={plotW} height={plotH} fill="none" />{yTicks.map((v, i) => (
            <line key={'g'+i} x1={padL} x2={w - padR}
              y1={yScale(v)} y2={yScale(v)}
              stroke={TH_X.grid} strokeOpacity="0.25" />
          ))}

          {/* p10–p90 band per visible model */}
          {visible.map(s => {
            const c = (window.modelColors && window.modelColors[s.key]) || '#888';
            const top = [], bot = [];
            for (const p of s.points) {
              if (!p.p90 || !p.p10) continue;
              top.push(`${xScale(p.ts)},${yScale(p.p90)}`);
              bot.push(`${xScale(p.ts)},${yScale(p.p10)}`);
            }
            if (top.length < 2) return null;
            const ribbon = `M ${top.join(' L ')} L ${bot.reverse().join(' L ')} Z`;
            return <path key={'band-'+s.key} d={ribbon} fill={c} fillOpacity="0.20" stroke="none" />;
          })}

          {/* Median lines */}
          {visible.map(s => {
            const c = (window.modelColors && window.modelColors[s.key]) || '#888';
            const pts = s.points
              .filter(p => p.p50 > 0)
              .map(p => `${xScale(p.ts)},${yScale(p.p50)}`).join(' ');
            return <polyline key={'med-'+s.key} points={pts}
              stroke={c} strokeWidth="1.8" fill="none" />;
          })}

          {/* Outlier dots (top/bottom 1%) */}
          {visibleOutliers.map((o, i) => {
            const c = (window.modelColors && window.modelColors[o.key]) || '#888';
            return <circle key={'o'+i} cx={xScale(o.tsMs)} cy={yScale(o.latency_s)}
              r="2.5" fill={c} fillOpacity="0.6" stroke="none" />;
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

          {tip && (
            <line x1={tip.cx} x2={tip.cx} y1={padT} y2={padT + plotH}
              stroke="#fff" strokeOpacity="0.3" strokeDasharray="2,3" />
          )}

          <g data-role="axis">{yTicks.map((v, i) => (
            <text key={'yl'+i} x={padL - 9} y={yScale(v) + 3}
              fontSize="9" fill={TH_X.textDim} textAnchor="end" fontFamily="monospace">
              {v < 1 ? v.toFixed(1) + 's'
               : v < 60 ? Math.round(v) + 's'
               : v < 3600 ? (v/60).toFixed(v < 600 ? 1 : 0).replace(/\.0$/, '') + 'm'
               : (v/3600).toFixed(v < 36000 ? 1 : 0).replace(/\.0$/, '') + 'h'}
            </text>
          ))}</g>
          <g data-role="axis">{xTicks.map((t, i) => (
            <text key={'xl'+i} x={xScale(t.ts)} y={h - padB + 14}
              fontSize="9" fill={TH_X.textDim} textAnchor="middle" fontFamily="monospace">
              {t.label}
            </text>
          ))}</g>
          <text data-role="axis" x={14} y={padT + plotH/2} fontSize="9" fill={TH_X.textDim}
            textAnchor="middle" fontFamily="monospace"
            transform={`rotate(-90 14 ${padT + plotH/2})`}>latency (log)</text>
        </svg>
        {a11y.descText && (
          <span className="sr-only" id={a11y.descId}>{a11y.descText}</span>
        )}
        {tip && <window.DashTooltip tip={tip} />}
      </div>
    </div>
  );
}
window.ReplyLatencyPanel = ReplyLatencyPanel;
