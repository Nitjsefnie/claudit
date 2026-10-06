// Extra dashboard panels: Per-Session Context Growth.
// Loaded after dashboard-charts.jsx; depends on its globals (TH/COL/humanFmt).

const TH_X       = window.dashboardTheme;
const COL_X      = window.dashboardCol;
const humanFmt_X = window.humanFmt;


// ──────────────────────────────────────────────────────────────────────
// Per-Session Context Growth panel
// ──────────────────────────────────────────────────────────────────────

const CTX_TURN_CAP = Infinity;
// The y axis is the observed peak with headroom, through the rule every
// context panel shares (issue #648). There is no per-model cap table:
// the one that lived here named ten Claude models and fell back to a
// name rule for the rest, so it drew a cap line below the data of any
// model added after it was written. See src/ctx-axis.js.

function buildSessionTurns(events) {
  // Group events by session_id, sort by turn_index (real turn boundaries
  // computed in txToDashData) or ts as fallback, and emit per-turn ctx sizes.
  const bySess = new Map();
  for (const e of events) {
    const sid = e.session_id || 'unknown';
    if (!bySess.has(sid)) bySess.set(sid, []);
    bySess.get(sid).push(e);
  }
  const out = {};
  for (const [sid, evs] of bySess) {
    evs.sort((a, b) => {
      if (a.turn_index != null && b.turn_index != null) return a.turn_index - b.turn_index;
      return a.ts - b.ts;
    });
    const counts = {};
    for (const e of evs) counts[e.model] = (counts[e.model] || 0) + 1;
    let dom = 'unknown', max = 0;
    for (const [m, c] of Object.entries(counts)) if (c > max) { max = c; dom = m; }
    // Default behavior: every session has an implicit (turn 0, ctx 0)
    // origin. Real turns are 1-indexed off that.
    const seq = [{ t: 0, ctx: 0 }];
    evs.forEach((e, i) => {
      const t = (e.turn_index != null ? e.turn_index : i) + 1;
      // Per-call context window. Prefer the per-event `ctx` produced by
      // txToDashData (which respects usage.iterations max via
      // usageCtxInput); fall back to the input+create+read sum when an
      // older event-shape lacks it.
      seq.push({
        t,
        ctx: e.ctx != null
          ? e.ctx
          : (e.input_tokens || 0) + (e.cache_create || 0) + (e.cache_read || 0),
      });
    });
    if (!out[dom]) out[dom] = [];
    out[dom].push({ id: sid, seq });
  }
  return out;
}

function perTurnStats(sessions) {
  const empty = { turns: [], median: [], p25: [], p75: [], p90: [], count: [], maxT: 0 };
  if (!sessions || !sessions.length) return empty;
  const byTurn = new Map();
  for (const s of sessions) {
    for (const p of s.seq) {
      if (p.t >= CTX_TURN_CAP) break;
      if (!byTurn.has(p.t)) byTurn.set(p.t, []);
      byTurn.get(p.t).push(p.ctx);
    }
  }
  if (!byTurn.size) return empty;
  const maxT = Math.max(...byTurn.keys());
  const turns = [], median = [], p25 = [], p75 = [], p90 = [], count = [];
  const pick = (arr, q) => arr[Math.min(arr.length - 1, Math.floor(arr.length * q))];
  for (let t = 0; t <= maxT; t++) {
    const vals = byTurn.get(t);
    if (!vals || vals.length < 1) {
      turns.push(t);
      median.push(null); p25.push(null); p75.push(null); p90.push(null);
      count.push(0);
      continue;
    }
    vals.sort((a, b) => a - b);
    turns.push(t);
    median.push(vals[Math.floor(vals.length / 2)]);
    p25.push(pick(vals, 0.25));
    p75.push(pick(vals, 0.75));
    p90.push(pick(vals, 0.9));
    count.push(vals.length);
  }
  return { turns, median, p25, p75, p90, count, maxT };
}

// Backend bucket projections center on bucket midpoint, so a polyline
// or band that just walks those midpoints leaves a half-bucket visual
// gap at each end (the data extends through [midpoint - N/2, midpoint
// + N/2) but the polyline only reaches the midpoint). This helper
// prepends + appends a virtual point half-a-bucket past each end with
// LINEARLY EXTRAPOLATED values (slope from the two adjacent points)
// so the rendered line/band fully covers the bucket extent.
//
// `valueKeys` lists the numeric fields to extrapolate. `log = true`
// extrapolates in log10 space (right for latency / response chars
// where the y-axis is log). `min` clamps the extrapolated value
// (default 0). Single-point series fall back to flat carry — no
// slope info available.
function extendBucketSeries(points, halfMs, valueKeys, options) {
  if (!points || !points.length) return points;
  const opts = options || {};
  const inLog = opts.log === true;
  const minY = opts.min !== undefined ? opts.min : 0;
  if (points.length === 1) {
    const p = points[0];
    return [{ ...p, ts: p.ts - halfMs }, p, { ...p, ts: p.ts + halfMs }];
  }
  const first = points[0], second = points[1];
  const last  = points[points.length - 1];
  const penul = points[points.length - 2];
  const lerp = (edge, neighbor) => {
    if (inLog) {
      const eps = 1e-9;
      const le = Math.log10(Math.max(eps, edge));
      const ln = Math.log10(Math.max(eps, neighbor));
      return Math.pow(10, 1.5 * le - 0.5 * ln);
    }
    return 1.5 * edge - 0.5 * neighbor;
  };
  const projFirst = { ...first };
  const projLast  = { ...last };
  for (const k of valueKeys) {
    projFirst[k] = Math.max(minY, lerp(first[k], second[k]));
    projLast[k]  = Math.max(minY, lerp(last[k],  penul[k]));
  }
  projFirst.ts = first.ts - halfMs;
  projLast.ts  = last.ts  + halfMs;
  return [projFirst, ...points, projLast];
}

// Canonicalize backend model strings (e.g. "claude-opus-4-7-20251101") to
// the key used by `window.modelColors` ("claude-opus-4-7"). The VENDOR PREFIX
// IS KEPT (#472) — only the snapshot/variant suffix folds, and it names the
// same model.
function shortModelName(m) {
  if (!m) return 'unknown';
  let s = String(m).toLowerCase();
  // Strip trailing [variant] tag (e.g. "claude-fable-5[1m]"), then -YYYYMMDD date
  s = s.replace(/\[[^\]]*\]$/, '');
  s = s.replace(/-\d{8}$/, '');
  return s;
}

function ContextGrowthPanel({ events, realSessions, ctxTraces }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(1200);

  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  // Prefer real per-session ctx traces from the backend when present;
  // fall back to bucket-grouping the synth/live events. The
  // pseudo-model "<synthetic>" is dropped — it's a synthetic
  // resampling row from the parser, not a real model.
  const byModel = React.useMemo(() => {
    const dropKey = (k) => k === '<synthetic>' || k === 'synthetic';
    // Preferred path: per-FILE ctx traces. Each file (main session OR
    // sub-agent invocation) is its own conversation with its own
    // dominant model. This makes models that only appear in sub-agent
    // calls (auto-compact, prompt-suggestion) visible in the panel
    // even when no main session JSONL exists.
    //
    // Index shift: backend turns are 0-indexed (first response = t0).
    // Re-index to 1-based and prepend an implicit (turn 0, ctx 0)
    // origin so every trace — including single-turn sub-agent calls
    // — has at least 2 points and renders as a polyline + contributes
    // a value-0 anchor to the per-turn median/p25/p75/p90 stats.
    if (ctxTraces && ctxTraces.length) {
      const out = {};
      for (const t of ctxTraces) {
        if (!t.turns || !t.turns.length) continue;
        const key = shortModelName(t.model);
        if (dropKey(key)) continue;
        // turns arrives as a flat array of ctx values; the turn index is
        // the position (it used to be sent as a redundant `t` field).
        const seq = [
          { t: 0, ctx: 0 },
          ...t.turns.map((ctx, i) => ({ t: i + 1, ctx })),
        ];
        if (!out[key]) out[key] = [];
        // No id: only `seq` is ever read off these, and carrying the
        // file_key meant ~100 chars of string per trace across 8.9k
        // traces for a field nothing rendered.
        out[key].push({ seq });
      }
      return out;
    }
    if (realSessions && realSessions.length) {
      const out = {};
      for (const s of realSessions) {
        if (!s.turns || !s.turns.length) continue;
        const seq = [
          { t: 0, ctx: 0 },
          ...s.turns.map(p => ({ t: p.t + 1, ctx: p.ctx })),
        ];
        const used = (s.models_used && s.models_used.length)
          ? s.models_used
          : [s.model];
        const seenKeys = new Set();
        for (const m of used) {
          const key = shortModelName(m);
          if (dropKey(key)) continue;
          if (seenKeys.has(key)) continue;
          seenKeys.add(key);
          if (!out[key]) out[key] = [];
          out[key].push({ id: s.session_id, seq });
        }
      }
      return out;
    }
    const m = buildSessionTurns(events);
    for (const k of Object.keys(m)) if (dropKey(k)) delete m[k];
    return m;
  }, [events, realSessions, ctxTraces]);

  // Models present, sorted by session count desc. This drives both the
  // checkbox row and the per-model sub-panels.
  const models = React.useMemo(() =>
    Object.entries(byModel)
      .map(([m, ss]) => ({ model: m, count: ss.length }))
      .sort((a, b) => b.count - a.count)
  , [byModel]);

  // Selection = top 2 by session count, with explicit user toggles
  // layered on top. This avoids the "first synth-mode set sticks
  // through realSessions arrival" bug — the default tracks current
  // models without needing a reset effect.
  const [overrides, setOverrides] = React.useState({});
  // Top 2 by session count, through the shared rule (src/model-select
  // .js) — the same default the Tool Error Rate panel resolves.
  const sel = React.useMemo(
    () => window.modelSelect.topDefaultSelection(models, overrides, 2),
    [models, overrides]);

  function toggle(m) {
    setOverrides(prev => ({ ...prev, [m]: !sel.has(m) }));
  }

  // The ONE chart takes the card's own measured width. There are no
  // per-model cells any more (#649): the panel is header + checkbox row
  // + one comparison chart, so its height does not grow with models.
  const cmpW = w;
  const cmpH = 240;

  // Models actually drawn in the comparison overlay.
  const cmpModels = models.filter(m => sel.has(m.model));

  // The per-session traces are a drawing MODE behind the breakdown, off
  // by default (#649 point 4).
  const [showSessions, setShowSessions] = React.useState(false);

  return (
    <div ref={ref} style={{
      background: TH_X.bgAxes, border: `1px solid ${TH_X.border}`,
      borderRadius: 4, padding: 0, position: 'relative',
      display: 'flex', flexDirection: 'column',
    }}>
      {/* Header */}
      <div style={{ padding: '10px 14px 4px', borderBottom: `1px solid ${TH_X.border}`, order: 1 }}>
        <div style={{ color: TH_X.text, fontFamily: 'monospace', fontWeight: 700, fontSize: 14 }}>
          Per-Session Context Growth
        </div>
        <div style={{ color: TH_X.textDim, fontFamily: 'monospace', fontSize: 10, marginTop: 2 }}>
          context size = input + cache_create + cache_read · x = turn within session
        </div>
      </div>

      {/* Model checkbox row — directly below the comparison overlay
          (order 3, after the comparison at order 2). The trailing chip
          toggles the per-session traces behind the breakdown. */}
      <div style={{
        padding: '8px 14px', borderBottom: `1px solid ${TH_X.border}`,
        display: 'flex', flexWrap: 'wrap', gap: '14px 14px',
        fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim,
        order: 3, alignItems: 'center',
      }}>
        <span style={{ color: TH_X.textDim }}>compare:</span>
        {models.map(m => {
          const c = (window.modelColors && window.modelColors[m.model]) || '#888';
          const checked = sel.has(m.model);
          return (
            <LegendCheckboxRow key={m.model} id={m.model} color={c} checked={checked}
              onToggle={toggle} name={m.model} count={m.count} />
          );
        })}
        {!models.length && <span>no sessions in range</span>}
        <window.ToggleChip on={showSessions}
          onToggle={() => setShowSessions(s => !s)} label="sessions" />
      </div>

      {/* Comparison overlay — driven by checked models */}
      <div style={{ order: 2 }}>
        <window.ComparisonRow models={cmpModels} byModel={byModel} w={cmpW} h={cmpH}
          showSessions={showSessions} />
      </div>

    </div>
  );
}

// Reusable tooltip primitive (the original Tooltip lives in a closure; expose ours).
// Flips left/up when it would overflow the viewport right/bottom edges.
function DashTooltip({ tip }) {
  const ref = React.useRef(null);
  const [pos, setPos] = React.useState({ left: 0, top: 0, ready: false });
  React.useLayoutEffect(() => {
    if (!tip || !ref.current) return;
    const el = ref.current;
    const w = el.offsetWidth, h = el.offsetHeight;
    const parentRect = el.offsetParent ? el.offsetParent.getBoundingClientRect() : { left: 0, top: 0 };
    const margin = 8;
    let left = tip.x + 12;
    let top  = tip.y + 12;
    const absRight  = parentRect.left + left + w;
    const absBottom = parentRect.top  + top  + h;
    if (absRight  > window.innerWidth  - margin) left = tip.x - w - 12;
    if (absBottom > window.innerHeight - margin) top  = tip.y - h - 12;
    const minLeft = -parentRect.left + margin;
    const minTop  = -parentRect.top  + margin;
    if (left < minLeft) left = minLeft;
    if (top  < minTop)  top  = minTop;
    setPos({ left, top, ready: true });
  }, [tip]);

  if (!tip) return null;
  const style = {
    position: 'absolute',
    left: pos.left,
    top: pos.top,
    visibility: pos.ready ? 'visible' : 'hidden',
    borderColor: tip.accent || undefined,
    pointerEvents: 'none',
    zIndex: 5,
    width: 'max-content',
  };
  return (
    <div ref={ref} className="chart-tooltip" style={style}>
      {tip.title && (
        <div className="chart-tooltip-title" style={{ color: tip.accent || undefined }}>
          {tip.title}
        </div>
      )}
      {(tip.lines || []).map((l, i) => (
        <div key={i} className="chart-tooltip-row">
          <span className="chart-tooltip-key" style={{ flexShrink: 0 }}>{l[0]}</span>
          <span className="chart-tooltip-val" style={{
            color: l[2] || undefined,
            wordBreak: 'break-all', whiteSpace: 'normal', textAlign: 'right',
          }}>{l[1]}</span>
        </div>
      ))}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Response Sizes panel — visible-text-character daily-bucketed time
// series per model. Each line = that model's daily median chars in
// `text` content blocks; dashed line = p90. Log y-axis (response
// sizes span 4+ orders of magnitude). Chars (not output_tokens)
// because output_tokens silently includes thinking, and per-model
// thinking shares vary 0.7%–25% — token-based percentiles would
// conflate "longer responses" with "more thinking".
// ──────────────────────────────────────────────────────────────────────
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
      while (i < pts.length - 1 && xScale(pts[i + 1].ts) < mx) i++;
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
        best = { ts, p50, p90, n };
      }
    }
    if (!best || bestD > 32) { setTip(null); return; }
    const fmt = window.humanFmt;
    setTip({
      x: mx, y: my,
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

      <div style={{
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

          {/* Crosshair */}
          {tip && (
            <line x1={tip.x} x2={tip.x} y1={padT} y2={padT + plotH}
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

// ──────────────────────────────────────────────────────────────────────
// Tool Usage panel — daily-bucketed share-of-total tool-call ratios
// per tool, stacked to 100%. A tool is promoted to its own band if
// it ever cracked top-N at any single bucket (so a newcomer that
// ramped recently gets visibility, not buried in "Other"). User can
// override per-tool via checkboxes. Hovering "Other" shows the full
// per-bin breakdown of the unpromoted tools.
// ──────────────────────────────────────────────────────────────────────

// Stable color picker — hash a tool name to a hue. Avoids manually
// curating a palette for ~80 tools while keeping each tool's color
// stable across reloads and panels.
function _toolColor(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0;
  const hue = ((h % 360) + 360) % 360;
  return `hsl(${hue}, 60%, 55%)`;
}
const _OTHER_COLOR = '#5a627a';

// Tool error rate (per-model sub-panels mirroring ContextGrowthPanel
// layout). Each sub-panel shows EMA(α=0.15) lines for "Aggregate"
// (all tools in the model) plus per-tool series. Default ON:
// Aggregate + top-3 tools by n_total over the visible range.
// Numerator = n_error, denominator = n_total over settled calls
// (is_error IS NOT NULL); unmatched calls excluded by the API.
// Shared by every legend checkbox row in this file -- the model, tool
// and series pickers. The checkbox IS the colour key and carries the
// on/off state itself, so a row shows its colour once: the separate 10px
// swatch doubled it, and beside it the 24px box was the largest thing in
// an 11px row (#474). SC 2.5.8 rides its SPACING exception instead --
// each legend container's '14px 14px' holds row centres 27px apart.
// A drawing-mode toggle (the sessions traces, the per-tool lines) — a
// BUTTON, not a checkbox row: it gates how the chart is drawn, it is not
// a series picker with a colour key, so it is deliberately not a
// LegendCheckboxRow. The state is carried in the text (on/off), so the
// control reads without colour.
function ToggleChip({ on, onToggle, label }) {
  return (
    <button type="button" onClick={onToggle}
      style={{
        fontFamily: 'monospace', fontSize: 11, cursor: 'pointer',
        color: TH_X.text, background: 'transparent',
        border: `1px solid ${on ? TH_X.text : TH_X.border}`,
        borderRadius: 3, padding: '4px 10px',
      }}>
      {label}: {on ? 'on' : 'off'}
    </button>
  );
}

function LegendCheckboxRow({ id, color, checked, onToggle, name, count }) {
  return (
    <label style={{ display: 'inline-flex', alignItems: 'center', gap: 5, cursor: 'pointer', userSelect: 'none' }}>
      <input type="checkbox" checked={checked} onChange={() => onToggle(id)}
        style={{ accentColor: color, margin: 0, width: 13, height: 13 }} />
      <span style={{ color: TH_X.text, fontWeight: 600 }}>{name}</span>
      <span style={{ color: TH_X.textDim }}>({count})</span>
    </label>
  );
}

function ToolUsagePanel({ models, project, range, nonce }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(1200);
  const [tip, setTip] = React.useState(null);
  const [data, setData] = React.useState([]);
  const [bucketMs, setBucketMs] = React.useState(86_400_000);
  // Per-panel model filter — separate from any global picker so the
  // user can drill into "what does opus-4-7 use Bash for?" without
  // affecting other panels.
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
    fetch(`/api/tool-usage?range=${range || 'all'}${q}`, { credentials: 'same-origin' })
      .then(r => r.json())
      .then(b => {
        setData(b.buckets || []);
        if (b.bucket_s) setBucketMs(b.bucket_s * 1000);
      })
      .catch(err => console.error('tool-usage fetch failed', err));
  }, [project, range, activeModel, nonce]);

  // Dedup model list by short name for the select.
  const modelOpts = React.useMemo(() => {
    const grouped = {};
    for (const m of models || []) {
      const key = window.shortModelName ? window.shortModelName(m.model) : m.model;
      if (key === '<synthetic>' || key === 'synthetic') continue;
      grouped[key] = (grouped[key] || 0) + (m.n || 0);
    }
    return Object.entries(grouped)
      .sort((a, b) => b[1] - a[1])
      .map(([k, n]) => ({ key: k, n }));
  }, [models]);

  const TOP_N = 7;

  // 1) Pivot data into a Map<bucketTs, Map<tool, count>> + bucket totals.
  // 2) Compute per-tool overall count.
  // 3) Determine "promoted" tools: top-N at any single bucket.
  const { buckets, perBucket, totalsByTool, promoted } = React.useMemo(() => {
    const perBucket = new Map();      // ts -> Map<tool, n>
    const totalsByTool = new Map();   // tool -> total n across all buckets
    for (const r of data || []) {
      const t = Date.parse(r.ts);
      if (isNaN(t)) continue;
      if (!perBucket.has(t)) perBucket.set(t, new Map());
      const cur = perBucket.get(t).get(r.tool) || 0;
      perBucket.get(t).set(r.tool, cur + r.n);
      totalsByTool.set(r.tool, (totalsByTool.get(r.tool) || 0) + r.n);
    }
    const buckets = [...perBucket.keys()].sort((a, b) => a - b);
    // Per-bucket top-N → union → promoted set.
    const promoted = new Set();
    for (const ts of buckets) {
      const entries = [...perBucket.get(ts).entries()].sort((a, b) => b[1] - a[1]);
      for (const [tool] of entries.slice(0, TOP_N)) promoted.add(tool);
    }
    return { buckets, perBucket, totalsByTool, promoted };
  }, [data]);

  // Sorted promoted-tool list (largest overall first → big bands at bottom).
  const promotedList = React.useMemo(
    () => [...promoted].sort((a, b) => (totalsByTool.get(b) || 0) - (totalsByTool.get(a) || 0)),
    [promoted, totalsByTool]
  );
  const otherTools = React.useMemo(
    () => [...totalsByTool.keys()]
      .filter(t => !promoted.has(t))
      .sort((a, b) => (totalsByTool.get(b) || 0) - (totalsByTool.get(a) || 0)),
    [totalsByTool, promoted]
  );

  // Per-tool checkbox overrides — start with all promoted shown.
  // Treat the literal key "__OTHER__" the same way so the user can
  // toggle the Other band off when it's not interesting.
  const [overrides, setOverrides] = React.useState({});
  const sel = React.useMemo(() => {
    const s = new Set(promotedList);
    s.add('__OTHER__');
    for (const [k, on] of Object.entries(overrides)) {
      if (on) s.add(k); else s.delete(k);
    }
    return s;
  }, [promotedList, overrides]);
  function toggle(k) {
    setOverrides(prev => ({ ...prev, [k]: !sel.has(k) }));
  }
  // Bands actually drawn (in stacking order, largest at bottom).
  const bands = [...sel].filter(k => k !== '__OTHER__')
    .sort((a, b) => (totalsByTool.get(b) || 0) - (totalsByTool.get(a) || 0));
  const showOther = otherTools.length > 0 && sel.has('__OTHER__');

  // Build per-bucket share series. Each bucket's *displayed* bands +
  // optional Other rescale to sum to 1.0 — so unchecking a tool
  // redistributes the remaining bands across the full 0-100% height
  // (relative ratios among what's shown), not just leaves a hole.
  // Tooltip still has access to the absolute bucket total.
  const grid = React.useMemo(() => {
    const shares = new Map();
    bands.forEach(t => shares.set(t, []));
    const other = [];
    const totalCalls = [];
    for (const ts of buckets) {
      const counts = perBucket.get(ts);
      let bucketTotal = 0;
      for (const v of counts.values()) bucketTotal += v;
      totalCalls.push(bucketTotal);
      let bandSum = 0;
      for (const t of bands) bandSum += counts.get(t) || 0;
      let otherSum = 0;
      if (showOther) for (const t of otherTools) otherSum += counts.get(t) || 0;
      const denom = bandSum + (showOther ? otherSum : 0);
      for (const t of bands) {
        const v = counts.get(t) || 0;
        shares.get(t).push(denom > 0 ? v / denom : 0);
      }
      other.push(showOther && denom > 0 ? otherSum / denom : 0);
    }
    // Extend the stacked-area by half a bucket on each end so the
    // visual reaches the bucket edges (no half-bucket gap). Shares
    // and Other are linearly extrapolated from the two adjacent
    // buckets, clamped ≥ 0. Per-band sums at the new boundaries are
    // then renormalized to 1.0 so the stack never overshoots 100%.
    if (buckets.length >= 2 && bucketMs > 0) {
      const halfMs = bucketMs / 2;
      const extrap = (arr) => {
        if (arr.length < 2) return [arr[0] || 0, ...arr, arr[arr.length - 1] || 0];
        const first = Math.max(0, 1.5 * arr[0] - 0.5 * arr[1]);
        const last  = Math.max(0, 1.5 * arr[arr.length - 1] - 0.5 * arr[arr.length - 2]);
        return [first, ...arr, last];
      };
      const newShares = new Map();
      for (const t of bands) newShares.set(t, extrap(shares.get(t)));
      const newOther = extrap(other);
      const newTotal = extrap(totalCalls);
      // Renormalize boundary points so band sum + other = 1 there
      // (independent extrapolation can drift the sum away from 1).
      const fixIdx = (idx) => {
        let sum = 0;
        for (const t of bands) sum += newShares.get(t)[idx];
        if (showOther) sum += newOther[idx];
        if (sum <= 0) return;
        for (const t of bands) newShares.get(t)[idx] = newShares.get(t)[idx] / sum;
        if (showOther) newOther[idx] = newOther[idx] / sum;
      };
      fixIdx(0);
      fixIdx(newOther.length - 1);
      return {
        ts: [buckets[0] - halfMs, ...buckets, buckets[buckets.length - 1] + halfMs],
        shares: newShares,
        other: newOther,
        totalCalls: newTotal,
      };
    }
    return { ts: buckets, shares, other, totalCalls };
  }, [buckets, perBucket, bands, showOther, otherTools, bucketMs]);

  // Geometry
  const padL = 56, padR = 30, padT = 16, padB = 30;
  const h = 320;
  const plotW = Math.max(20, w - padL - padR);
  const plotH = h - padT - padB;
  const tMin = grid.ts[0] || (Date.now() - 24 * 3600 * 1000);
  const tMax = grid.ts[grid.ts.length - 1] || Date.now();
  const xScale = ts => padL + ((ts - tMin) / Math.max(1, tMax - tMin)) * plotW;
  const yScale = frac => padT + plotH - frac * plotH;

  // Buckets where the displayed bands+Other sum to 0 carry no data
  // FOR THE CURRENT FILTER — interpolate across them instead of
  // collapsing to baseline (which reads as a hard "data ends here").
  const liveIdx = React.useMemo(() => {
    const out = [];
    for (let i = 0; i < grid.ts.length; i++) {
      let total = showOther ? grid.other[i] : 0;
      for (const t of bands) total += grid.shares.get(t)[i];
      if (total > 0) out.push(i);
    }
    return out;
  }, [grid, bands, showOther]);

  // Stacked-area paths. Bottom-up: largest band first, "Other" last.
  // Path walks `liveIdx` only — gaps are bridged by linear segments
  // between adjacent live buckets.
  const stackPaths = React.useMemo(() => {
    const out = [];
    if (!liveIdx.length) return out;
    const cum = new Array(grid.ts.length).fill(0);
    const layers = [...bands.map(t => ({ tool: t, color: _toolColor(t), shares: grid.shares.get(t) }))];
    if (showOther) layers.push({ tool: '__OTHER__', color: _OTHER_COLOR, shares: grid.other });
    for (const layer of layers) {
      const top = [], bot = [];
      for (const i of liveIdx) {
        const baseY = yScale(cum[i]);
        const topY  = yScale(cum[i] + layer.shares[i]);
        bot.push(`${xScale(grid.ts[i])},${baseY}`);
        top.push(`${xScale(grid.ts[i])},${topY}`);
        cum[i] += layer.shares[i];
      }
      const d = `M ${top.join(' L ')} L ${bot.reverse().join(' L ')} Z`;
      out.push({ tool: layer.tool, color: layer.color, d });
    }
    return out;
  }, [grid, bands, showOther, liveIdx, plotW, plotH, tMin, tMax]);

  // Y-axis ticks at 0/25/50/75/100%.
  const yTicks = [0, 0.25, 0.5, 0.75, 1.0];
  // X-axis: adaptive labels.
  const xTicks = (isFinite(tMin) && isFinite(tMax)) ? window.timeTicksUTC(tMin, tMax) : [];

  function onMove(e) {
    const rect = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    if (mx < padL || mx > w - padR || my < padT || my > padT + plotH) {
      setTip(null); return;
    }
    if (!grid.ts.length) { setTip(null); return; }
    // Snap to nearest bucket on x.
    let bIdx = 0, bestD = 1e9;
    for (let i = 0; i < grid.ts.length; i++) {
      const d = Math.abs(xScale(grid.ts[i]) - mx);
      if (d < bestD) { bestD = d; bIdx = i; }
    }
    const cursorFrac = 1 - (my - padT) / plotH;
    // Identify which band the cursor is in (bottom-up cumulative).
    let cum = 0, hovered = null;
    for (const t of bands) {
      const sh = grid.shares.get(t)[bIdx];
      if (cursorFrac >= cum && cursorFrac < cum + sh) { hovered = t; break; }
      cum += sh;
    }
    if (!hovered && showOther && cursorFrac >= cum && cursorFrac < cum + grid.other[bIdx]) {
      hovered = '__OTHER__';
    }
    if (!hovered) { setTip(null); return; }
    const ts = grid.ts[bIdx];
    const totalCalls = grid.totalCalls[bIdx];
    const dateStr = new Date(ts).toISOString().slice(0, 10);
    const lines = [];
    // Denom = sum across what's actually displayed in this bucket
    // (matches the chart's rescaled share %).
    const counts = perBucket.get(ts);
    let bandSum = 0;
    for (const t of bands) bandSum += counts.get(t) || 0;
    let otherSum = 0;
    if (showOther) for (const t of otherTools) otherSum += counts.get(t) || 0;
    const shownDenom = Math.max(1, bandSum + (showOther ? otherSum : 0));
    if (hovered === '__OTHER__') {
      const otherEntries = otherTools
        .map(t => ({ tool: t, n: counts.get(t) || 0 }))
        .filter(e => e.n > 0)
        .sort((a, b) => b.n - a.n);
      const otherTotal = otherEntries.reduce((s, e) => s + e.n, 0);
      lines.push(['Other share',  (otherTotal / shownDenom * 100).toFixed(1) + '% of shown']);
      lines.push(['Other calls',  otherTotal.toLocaleString() + ' (bucket total: ' + totalCalls.toLocaleString() + ')']);
      for (const e of otherEntries) {
        lines.push([
          e.tool,
          `${(e.n / shownDenom * 100).toFixed(1)}% (${e.n.toLocaleString()})`,
        ]);
      }
      setTip({
        x: mx, y: my,
        title: `Other · ${dateStr}`,
        accent: _OTHER_COLOR,
        lines,
      });
    } else {
      const n = counts.get(hovered) || 0;
      setTip({
        x: mx, y: my,
        title: `${hovered} · ${dateStr}`,
        accent: _toolColor(hovered),
        lines: [
          ['share',     (n / shownDenom * 100).toFixed(1) + '% of shown'],
          ['absolute',  (n / Math.max(1, totalCalls) * 100).toFixed(1) + '% of bucket'],
          ['calls',     n.toLocaleString() + ' / ' + totalCalls.toLocaleString()],
        ],
      });
    }
  }

  const a11y = window.useChartA11y(
    'Tool Usage Ratio',
    `stacked bands to 100%, ${promotedList.length} tools shown`
      + (otherTools.length ? ` + ${otherTools.length} in Other` : ''),
    null);
  return (
    <div ref={ref} style={{
      background: TH_X.bgAxes, border: `1px solid ${TH_X.border}`,
      borderRadius: 4, padding: 0, position: 'relative',
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ padding: '10px 14px 4px', borderBottom: `1px solid ${TH_X.border}`, display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
        <div style={{ flex: 1 }}>
          <div style={{ color: TH_X.text, fontFamily: 'monospace', fontWeight: 700, fontSize: 14 }}>
            Tool Usage Ratio over Time
          </div>
          <div style={{ color: TH_X.textDim, fontFamily: 'monospace', fontSize: 10, marginTop: 2 }}>
            stacked share of tool calls per day · top-{TOP_N}-at-any-bucket promoted to own band · {showOther ? `${otherTools.length} smaller tools collapsed into Other (hover to expand)` : 'no Other bucket'}
          </div>
        </div>
        <div style={{ display: 'inline-flex', flexWrap: 'wrap', alignItems: 'center', gap: 6, fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim }}>
          <button
            type="button"
            onClick={() => {
              const next = {};
              for (const t of promotedList) next[t] = false;
              next['__OTHER__'] = false;
              setOverrides(next);
            }}
            style={{
              background: 'transparent', color: TH_X.textDim,
              border: `1px solid ${TH_X.border}`, borderRadius: 3,
              padding: '2px 8px', fontFamily: 'monospace', fontSize: 11,
              cursor: 'pointer',
            }}
          >none</button>
          <button
            type="button"
            onClick={() => {
              const next = {};
              for (const t of promotedList) next[t] = true;
              next['__OTHER__'] = true;
              setOverrides(next);
            }}
            style={{
              background: 'transparent', color: TH_X.textDim,
              border: `1px solid ${TH_X.border}`, borderRadius: 3,
              padding: '2px 8px', fontFamily: 'monospace', fontSize: 11,
              cursor: 'pointer',
            }}
          >all</button>
          <span style={{ marginLeft: 8 }}>model:</span>
          <select aria-label="Tool Usage model filter"
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

      <div style={{
        padding: '8px 14px', borderTop: `1px solid ${TH_X.border}`,
        display: 'flex', flexWrap: 'wrap', gap: '14px 14px',
        fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim,
        order: 99,
      }}>
        <span>show:</span>
        {promotedList.map(tool => {
          const c = _toolColor(tool);
          const checked = sel.has(tool);
          return (
            <LegendCheckboxRow key={tool} id={tool} color={c} checked={checked}
              onToggle={toggle} name={tool} count={(totalsByTool.get(tool) || 0).toLocaleString()} />
          );
        })}
        {otherTools.length > 0 && (() => {
          const checked = sel.has('__OTHER__');
          return (
            <LegendCheckboxRow id="__OTHER__" color={_OTHER_COLOR}
              checked={checked} onToggle={toggle} name="Other"
              count={`${otherTools.length} tools`} />
          );
        })()}
        {!promotedList.length && <span>no tool data in range</span>}
      </div>

      <div style={{ position: 'relative' }} onMouseMove={onMove} onMouseLeave={() => setTip(null)}>
        <svg role="img" aria-label={a11y.label} aria-describedby={a11y.descId}
          data-panel="Tool Usage Ratio" width={w} height={h} style={{ display: 'block' }}>
          {/* Y grid */}
          <rect data-role="plot" x={padL} y={padT} width={plotW} height={plotH} fill="none" />{yTicks.map((v, i) => (
            <line key={'g'+i} x1={padL} x2={w - padR}
              y1={yScale(v)} y2={yScale(v)}
              stroke={TH_X.grid} strokeOpacity="0.25" />
          ))}

          {/* Stacked bands */}
          {stackPaths.map(layer => (
            <path key={layer.tool} d={layer.d}
              fill={layer.color} fillOpacity="0.85" stroke="none" />
          ))}

          {/* Y labels */}
          <g data-role="axis">{yTicks.map((v, i) => (
            <text key={'yl'+i} x={padL - 9} y={yScale(v) + 3}
              fontSize="9" fill={TH_X.textDim} textAnchor="end" fontFamily="monospace">
              {(v * 100).toFixed(0)}%
            </text>
          ))}</g>
          {/* X labels */}
          <g data-role="axis">{xTicks.map((t, i) => (
            <text key={'xl'+i} x={xScale(t.ts)} y={h - padB + 14}
              fontSize="9" fill={TH_X.textDim} textAnchor="middle" fontFamily="monospace">
              {t.label}
            </text>
          ))}</g>
          {tip && (
            <line x1={tip.x} x2={tip.x} y1={padT} y2={padT + plotH}
              stroke="#fff" strokeOpacity="0.3" strokeDasharray="2,3" />
          )}
        </svg>
        {tip && <window.DashTooltip tip={tip} />}
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Reply Latency panel — per-(bucket, model) p10–p90 band + median line,
// plus scatter dots for top-1% slowest + bottom-1% fastest replies
// per bucket (when bucket_n >= 100). Log y-axis (latency 0.5s–400s).
// ──────────────────────────────────────────────────────────────────────
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
      .sort((a, b) => b[1] - a[1])
      .map(([k, n]) => ({ key: k, n }));
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
        x: mx, y: my,
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
      while (i < pts.length - 1 && xScale(pts[i + 1].ts) < mx) i++;
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
        bestD = d; bestKey = s.key; best = { ts, p10, p50, p90, n };
      }
    }
    if (!best || bestD > 32) { setTip(null); return; }
    setTip({
      x: mx, y: my,
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
    null);
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

      <div style={{
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

          {tip && (
            <line x1={tip.x} x2={tip.x} y1={padT} y2={padT + plotH}
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
        {tip && <window.DashTooltip tip={tip} />}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Activity Heatmap — weekday × hour grid in Europe/Prague local time.
// DST handling lives in the backend (Postgres AT TIME ZONE); this panel
// only renders the dow/hour cells it is given.
const _HEAT_DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const _HEAT_METRICS = [
  { key: 'requests',      label: 'requests',   color: 'oklch(0.78 0.14 245)',
    fmt: v => v.toLocaleString() },
  { key: 'output_tokens', label: 'output tok', color: COL_X.outputTokens,
    fmt: v => humanFmt_X(v) },
  { key: 'cost_usd',      label: 'cost',       color: COL_X.costUSD,
    fmt: v => window.humanCurrency(v) },
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
      .sort((a, b) => b[1] - a[1])
      .map(([k, n]) => ({ key: k, n }));
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
      + `${maxVal > 0 ? mspec.fmt(maxVal) : 'no data'}`, null);
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
            return (
              <rect key={`${dow}-${hour}`} x={x} y={y}
                    width={cellW} height={cellH} rx="2"
                    fill={f.color} fillOpacity={f.opacity}
                    stroke={v > 0 ? 'none' : TH_X.border}
                    strokeWidth={v > 0 ? 0 : 0.5}
                    onMouseMove={e => onCellEnter(e, dow, hour)} />
            );
          })
        )}

        {/* Σ column (per-weekday totals) */}
        {Array.from({ length: 7 }, (_, i) => i + 1).map(dow => {
          const v = rowTotals[dow - 1][activeMetric];
          const f = fillFor(v, rowMax);
          return (
            <rect key={`sumcol-${dow}`} x={sumColX}
                  y={padT + (dow - 1) * (cellH + gap)}
                  width={cellW} height={cellH} rx="2"
                  fill={f.color} fillOpacity={f.opacity}
                  stroke={v > 0 ? 'none' : TH_X.border}
                  strokeWidth={v > 0 ? 0 : 0.5}
                  onMouseMove={e => onSumColEnter(e, dow)} />
          );
        })}

        {/* Σ row (per-hour totals) */}
        {Array.from({ length: 24 }, (_, hour) => {
          const v = colTotals[hour][activeMetric];
          const f = fillFor(v, colMax);
          return (
            <rect key={`sumrow-${hour}`} x={padL + hour * (cellW + gap)}
                  y={sumRowY} width={cellW} height={cellH} rx="2"
                  fill={f.color} fillOpacity={f.opacity}
                  stroke={v > 0 ? 'none' : TH_X.border}
                  strokeWidth={v > 0 ? 0 : 0.5}
                  onMouseMove={e => onSumRowEnter(e, hour)} />
          );
        })}

        {/* Corner grand total (outline only; value in tooltip) */}
        <rect x={sumColX} y={sumRowY} width={cellW} height={cellH} rx="2"
              fill="none" stroke={TH_X.border} strokeWidth="0.5"
              onMouseMove={onCornerEnter} />
      </svg>

      <div style={{
        padding: '4px 14px 10px', display: 'flex', alignItems: 'center', gap: 8,
        fontFamily: 'monospace', fontSize: 10, color: TH_X.textDim,
      }}>
        <span>0</span>
        <svg aria-hidden="true" data-panel="Activity Heatmap — legend"
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

// ──────────────────────────────────────────────────────────────────────
// Cost by Context Size — where the money goes across the window
// ──────────────────────────────────────────────────────────────────────

// COL is keyed by SERIES NAME, not indexed — COL_X[0] is undefined, and
// an SVG rect with fill=undefined renders BLACK while a path with
// stroke=undefined renders nothing at all. Name the keys.
// One colour for both marks, exactly as TimeSeriesPanel's Cost (USD)
// does it (dashboard-charts.jsx:404-423): bars at fillOpacity 0.3 (0.85
// on hover) and the cumulative line in the SAME hue, made legible by a
// soft white halo underneath rather than by a second colour.
//
// That treatment dissolves the problem the earlier versions kept
// failing at. A line in a contrasting hue has to beat the bars on
// lightness AND stay visible on the dark surface, which nothing does
// well; a same-hue line over a 0.3-opacity field, ringed in white at
// 0.15, has no such conflict and is what the rest of the app already
// looks like.
const BAR_COLOR = (COL_X && COL_X.costUSD) || 'oklch(0.85 0.14 90)';
const BAR_OPACITY = 0.3;
const BAR_OPACITY_HOVER = 0.85;

// `measure` picks which side of the same endpoint the panel charts:
// 'cost' (dollars, the original) or 'tokens' (every token the calls in
// that bucket processed). ONE component rather than a copy — the mark
// treatment below (dim bars, a same-hue cumulative line under a white
// halo, container hover, rotated axis captions) is pinned by
// tests/test_panel_wiring.py against this implementation, and a second
// copy would drift out from under those guards.
function CostByContextPanel({ models, project, range, nonce, measure }) {
  const isTokens = measure === 'tokens';
  const ref = React.useRef(null), svgRef = React.useRef(null);
  const [w, setW] = React.useState(1200);
  const [tip, setTip] = React.useState(null);
  const [data, setData] = React.useState([]);
  const [meta, setMeta] = React.useState({ bucket_width: 50000, bucket_max: 1000000, total_cost_usd: 0, total_tokens: 0 });
  // Per-panel model filter, same convention as ToolUsagePanel: drill into
  // one model without disturbing the other panels.
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
    fetch(`/api/cost-by-context?range=${range || 'all'}${q}`, { credentials: 'same-origin' })
      .then(r => r.json())
      .then(b => {
        setData(b.buckets || []);
        setMeta({
          bucket_width: b.bucket_width || 50000,
          bucket_max: b.bucket_max || 1000000,
          total_cost_usd: b.total_cost_usd || 0,
          total_tokens: b.total_tokens || 0,
        });
      })
      .catch(err => console.error('cost-by-context fetch failed', err));
  }, [project, range, activeModel, nonce]);

  const modelOpts = React.useMemo(() => {
    const grouped = {};
    for (const m of models || []) {
      const key = window.shortModelName ? window.shortModelName(m.model) : m.model;
      if (key === '<synthetic>' || key === 'synthetic') continue;
      grouped[key] = (grouped[key] || 0) + (m.n || 0);
    }
    return Object.entries(grouped)
      .sort((a, b) => b[1] - a[1])
      .map(([k, n]) => ({ key: k, n }));
  }, [models]);

  // Every bucket from 0 to the overflow edge, so an empty bucket renders
  // as a gap rather than silently closing the gap between its neighbours
  // — the x-axis is a real number line, not a category list.
  const bars = React.useMemo(() => {
    const byEdge = new Map();
    for (const b of data) byEdge.set(b.ctx_bucket, b);
    const out = [];
    let running = 0;
    const grand = isTokens ? meta.total_tokens : meta.total_cost_usd;
    for (let e = 0; e <= meta.bucket_max; e += meta.bucket_width) {
      const hit = byEdge.get(e);
      const v = hit ? (isTokens ? hit.total_tokens : hit.cost_usd) : 0;
      running += v;
      out.push({
        edge: e,
        cost: v,
        requests: hit ? hit.requests : 0,
        cum: grand ? running / grand : 0,
        overflow: e === meta.bucket_max,
      });
    }
    return out;
  }, [data, meta, isTokens]);

  const maxCost = React.useMemo(
    () => Math.max(1e-9, ...bars.map(b => b.cost)), [bars]);

  const padL = 62, padR = 52, padT = 16, padB = 34;
  const h = 320;
  const plotW = Math.max(20, w - padL - padR);
  const plotH = h - padT - padB;
  const bw = plotW / Math.max(1, bars.length);
  const yCost = c => padT + plotH - (c / maxCost) * plotH;
  const yShare = f => padT + plotH - f * plotH;

  // Anchored to bucket EDGES, not centres. `cum` is the share of spend at
  // or below a bucket's upper bound, so it is only fully accumulated once
  // the whole bucket is behind you — plotting it at the centre states the
  // value half a bucket early. Starting at (left edge of bar 0, 0) and
  // stepping to each bucket's RIGHT edge is both correct and what makes
  // the curve span the bars edge to edge instead of floating inside them.
  const cumPath = React.useMemo(() => {
    if (!bars.length) return '';
    const pts = [`${padL},${yShare(0)}`];
    for (let i = 0; i < bars.length; i++) {
      pts.push(`${padL + (i + 1) * bw},${yShare(bars[i].cum)}`);
    }
    return pts.join(' L ');
  }, [bars, bw, plotW, plotH]);

  // The headline the panel exists to give: the share of spend above the
  // context size where the cumulative curve crosses 50%.
  const medianEdge = React.useMemo(() => {
    for (const b of bars) if (b.cum >= 0.5) return b.edge;
    return null;
  }, [bars]);

  const fmtTok = t => (t >= 1000 ? `${Math.round(t / 1000)}k` : String(t));
  // The measure's own formatter, used for the y-axis, the tooltip and
  // the total badge alike so all three read in the same unit.
  const fmtUsd = v => (isTokens
    ? humanFmt_X(v)
    : (v >= 1000 ? `$${(v / 1000).toFixed(1)}k` : `$${v.toFixed(v < 10 ? 2 : 0)}`));

  // Hit-test in the <svg>'s frame: padT/plotH are svg coordinates, and the
  // container's rect carries the header block above the svg, so comparing
  // across the frames parked the active band a header-height high (#645).
  // The tip still positions in container coordinates — its offsetParent.
  function onMove(e) {
    if (!ref.current || !svgRef.current) return;
    const rect = ref.current.getBoundingClientRect(), srect = svgRef.current.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top, sx = e.clientX - srect.left, sy = e.clientY - srect.top;
    if (sy < padT || sy > padT + plotH) { setTip(null); return; }
    const i = Math.floor((sx - padL) / bw);
    if (i < 0 || i >= bars.length) { setTip(null); return; }
    const b = bars[i];
    const lo = fmtTok(b.edge);
    const hi = b.overflow ? '∞' : fmtTok(b.edge + meta.bucket_width);
    setTip({
      x: mx, y: my, idx: i,
      title: `${lo}–${hi} ctx tokens`,
      accent: BAR_COLOR,
      lines: [
        [isTokens ? 'tokens' : 'cost',
          isTokens ? humanFmt_X(b.cost) : `$${b.cost.toFixed(2)}`],
        ['share', (isTokens ? meta.total_tokens : meta.total_cost_usd)
          ? `${(b.cost / (isTokens ? meta.total_tokens : meta.total_cost_usd) * 100).toFixed(1)}%`
          : '0%'],
        ['cumulative', `${(b.cum * 100).toFixed(1)}% of ${isTokens ? 'tokens' : 'spend'} at or below`],
        ['requests', b.requests.toLocaleString()],
        [isTokens ? 'tokens/request' : '$/request',
          b.requests
            ? (isTokens ? humanFmt_X(b.cost / b.requests) : `$${(b.cost / b.requests).toFixed(4)}`)
            : '—'],
      ],
    });
  }

  const a11y = window.useChartA11y(
    isTokens ? 'Tokens by Context Size' : 'Cost by Context Size',
    `bars with a cumulative share line, ${bars.length} context buckets`,
    medianEdge !== null
      ? `Half of all ${isTokens ? 'tokens sit' : 'spend sits'} above `
        + `${fmtTok(medianEdge)} context tokens.`
      : null);
  return (
    <div ref={ref} style={{
      background: TH_X.bgAxes, border: `1px solid ${TH_X.border}`,
      borderRadius: 4, padding: 0, position: 'relative',
      display: 'flex', flexDirection: 'column',
    }}
    onMouseMove={onMove}
    onMouseLeave={() => setTip(null)}>
      <div style={{ padding: '10px 14px 4px', borderBottom: `1px solid ${TH_X.border}`, display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
        <div style={{ flex: 1 }}>
          <div style={{ color: TH_X.text, fontFamily: 'monospace', fontWeight: 700, fontSize: 14 }}>
            {isTokens ? 'Tokens by Context Size' : 'Cost by Context Size'}
          </div>
          <div style={{ color: TH_X.textDim, fontFamily: 'monospace', fontSize: 10, marginTop: 2 }}>
            {isTokens ? 'tokens' : '$'} per {fmtTok(meta.bucket_width)} context bucket · cumulative share (right axis) · context = fresh + cache-create + cache-read
            {medianEdge !== null
              ? ` · half of all ${isTokens ? 'tokens sit' : 'spend sits'} above ${fmtTok(medianEdge)}`
              : ''}
          </div>
        </div>
        <div style={{ display: 'inline-flex', flexWrap: 'wrap', alignItems: 'center', gap: 6, fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim }}>
          <span>model:</span>
          <select aria-label="Cost by Context Size model filter"
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

      <svg ref={svgRef} role="img" aria-label={a11y.label} aria-describedby={a11y.descId}
        data-panel={isTokens ? 'Tokens by Context Size' : 'Cost by Context Size'}
        width={w} height={h} style={{ display: 'block' }}>
        <rect data-role="plot" x={padL} y={padT} width={plotW} height={plotH} fill="none" />{[0, 0.25, 0.5, 0.75, 1].map(f => (
          <line key={'g' + f} x1={padL} x2={padL + plotW} y1={yCost(maxCost * f)} y2={yCost(maxCost * f)}
                stroke={TH_X.grid} strokeWidth={1} />
        ))}
        <g data-role="axis">{[0, 0.25, 0.5, 0.75, 1].map(f => (
          <text key={f} x={padL - 8} y={yCost(maxCost * f) + 4} textAnchor="end"
                fill={TH_X.textDim} fontFamily="monospace" fontSize={10}>
            {fmtUsd(maxCost * f)}
          </text>
        ))}</g><g data-role="axis">{[0, 0.25, 0.5, 0.75, 1].map(f => (
          <text key={f} x={padL + plotW + 8} y={yShare(f) + 4} textAnchor="start" fill={TH_X.textDim}
                fontFamily="monospace" fontSize={10}>{Math.round(f * 100)}%</text>
        ))}</g>

        {bars.map((b, i) => (
          <rect key={b.edge} data-hover-target=""
                x={padL + i * bw + 1} y={yCost(b.cost)}
                width={Math.max(1, bw - 2)}
                height={Math.max(0, plotH + padT - yCost(b.cost))}
                fill={BAR_COLOR}
                fillOpacity={tip && tip.idx === i
                  ? BAR_OPACITY_HOVER : BAR_OPACITY} />
        ))}

        {cumPath && (<>
          {/* Area under the cumulative curve, then the white halo, then
              the line — the Cost (USD) stack, same opacities. */}
          <path d={`M ${cumPath} L ${padL + bars.length * bw},${padT + plotH} L ${padL},${padT + plotH} Z`}
                fill={BAR_COLOR} fillOpacity="0.04" stroke="none" />
          <path d={`M ${cumPath}`} fill="none"
                stroke="#fff" strokeOpacity="0.15" strokeWidth="4" />
          <path data-cumulative-line="" d={`M ${cumPath}`} fill="none"
                stroke={BAR_COLOR} strokeWidth="2" />
        </>)}

        {/* Hover crosshair, as on every other bucketed panel. */}
        {tip && tip.idx != null && (
          <line x1={padL + tip.idx * bw + bw / 2}
                x2={padL + tip.idx * bw + bw / 2}
                y1={padT} y2={padT + plotH}
                stroke={BAR_COLOR} strokeOpacity="0.4"
                strokeWidth="1" strokeDasharray="2,3" />
        )}

        <g data-role="axis">{bars.map((b, i) => (
          (i % Math.max(1, Math.round(bars.length / 10)) === 0 || b.overflow) ? (
            <text key={`x${b.edge}`} x={padL + i * bw + bw / 2} y={h - 12}
                  textAnchor="middle" fill={TH_X.textDim}
                  fontFamily="monospace" fontSize={10}>
              {b.overflow ? `${fmtTok(b.edge)}+` : fmtTok(b.edge)}
            </text>
          ) : null
        ))}</g>

        {/* Rotated captions naming each scale — how Cost (USD) labels a
            left and a right axis without spending a legend on it. */}
        <text data-role="axis" x={17} y={padT + plotH / 2} fontSize="9" fill={TH_X.textDim}
              textAnchor="middle" fontFamily="monospace"
              transform={`rotate(-90 17 ${padT + plotH / 2})`}>{isTokens ? 'tokens' : 'cost'}</text>
        <text data-role="axis" x={w - 12} y={padT + plotH / 2} fontSize="9" fill={TH_X.textDim}
              textAnchor="middle" fontFamily="monospace"
              transform={`rotate(-90 ${w - 12} ${padT + plotH / 2})`}>cumulative</text>

        {(() => {
          const totalStr = `Total: ${fmtUsd(isTokens ? meta.total_tokens : meta.total_cost_usd)}`;
          const boxW = Math.ceil(totalStr.length * 6.6) + 16;
          // Top-LEFT corner of the plot, mirroring TimeSeriesPanel's badge
          // (`padL + 6`, same padT + 2). The cumulative-share line starts
          // at 0 on this axis, so the left corner is where it collides least.
          const boxX = padL + 6;
          return (
            <g>
              <rect data-total-badge="" x={boxX} y={padT + 2} width={boxW} height={20} rx={4}
                    fill={TH_X.bgAxes} stroke={BAR_COLOR} strokeOpacity="0.8" />
              <text x={boxX + boxW / 2} y={padT + 16} fontSize="11" fontWeight="bold"
                    fill={BAR_COLOR} textAnchor="middle" fontFamily="monospace">
                {totalStr}
              </text>
            </g>
          );
        })()}
      </svg>
      {a11y.descText && (
        <span className="sr-only" id={a11y.descId}>{a11y.descText}</span>
      )}

      {tip && <window.DashTooltip tip={tip} />}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Cost by Agent Type — one bar per agent role, biggest first.
//
// Categorical labels with one value each, which is exactly what the
// sibling "Cost by Model" card already is, so this reuses window.HBar
// rather than growing another bespoke SVG. Colours come from the same
// hash-to-hue picker the Tool Usage bands use: the roster grows as
// ~/.claude/agents/ does, and a curated palette would go stale.
//
// The `general-purpose` bar is NOT a claim that those sessions were
// dispatched as general-purpose agents. It is where every transcript
// with no role recorded lands (see parse.resolve_agent_type) — a plain
// lead, a session started with an explicit --agent flag, and any
// subagent transcript predating Claude Code 2.1.126 all read the same
// in the file. The subtitle says so, because a bar this large silently
// meaning "unattributed" would be read as a measurement.
// ──────────────────────────────────────────────────────────────────────
function CostByAgentPanel({ models, project, range, nonce }) {
  const [rows, setRows] = React.useState([]);
  const [total, setTotal] = React.useState(0);
  // Per-panel model filter, same convention as ToolUsagePanel and
  // CostByContextPanel: drill into one model without disturbing the
  // other panels.
  const [activeModel, setActiveModel] = React.useState('');

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
      .sort((a, b) => b[1] - a[1])
      .map(([k, n]) => ({ key: k, n }));
  }, [models]);

  const [totalTokens, setTotalTokens] = React.useState(0);

  const bars = React.useMemo(() => rows.map(a => ({
    label: a.agent_type,
    value: a.cost_usd,
    color: _toolColor(a.agent_type),
    requests: a.requests,
  })), [rows]);

  // The same roles measured in tokens, biggest first — the ordering the
  // endpoint applies is by cost, which a free lane leaves arbitrary.
  const tokenBars = React.useMemo(() => rows.map(a => ({
    label: a.agent_type,
    value: a.total_tokens || 0,
    color: _toolColor(a.agent_type),
    requests: a.requests,
  })).filter(b => b.value > 0).sort((a, b) => b.value - a.value), [rows]);

  return (
    <div style={{
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
      {total > 0 && (
      <window.HBar
        embedded
        title="Cost by Agent Type"
        rows={bars}
        fmt={r => `${window.humanCurrency(r.value)} (${(r.value / total * 100).toFixed(1)}%)`} />
      )}
      <window.HBar
        embedded
        title="Tokens by Agent Type"
        rows={tokenBars}
        fmt={r => `${humanFmt_X(r.value)} (${totalTokens > 0 ? (r.value / totalTokens * 100).toFixed(1) : '0.0'}%)`} />
    </div>
  );
}


window.ContextGrowthPanel = ContextGrowthPanel;
window.DashTooltip = DashTooltip;
window.shortModelName = shortModelName;
window.perTurnStats = perTurnStats;
window.LegendCheckboxRow = LegendCheckboxRow;
window.ToggleChip = ToggleChip;
window.toolColor = _toolColor;
window.ResponseSizesPanel = ResponseSizesPanel;
window.ToolUsagePanel = ToolUsagePanel;
window.ActivityHeatmapPanel = ActivityHeatmapPanel;
window.ReplyLatencyPanel = ReplyLatencyPanel;
window.CostByContextPanel = CostByContextPanel;
window.CostByAgentPanel = CostByAgentPanel;
