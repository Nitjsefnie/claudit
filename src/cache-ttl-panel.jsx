// The Prompt-Cache TTL Split panel: stacked 5m + 1h hourly cache-create
// bars with a 5m-share strip. Extracted to its own file in #633 -- the
// module-size ratchet's committed entry for dashboard-charts-extra.jsx
// never rises, and the legend fix adds lines, so the panel moved out
// instead of growing a file already far over the ceiling. Loaded after
// dashboard-charts.jsx, whose globals (TH/COL/humanFmt/fmtDate) it reads.

const TH_X       = window.dashboardTheme;
const COL_X      = window.dashboardCol;
const humanFmt_X = window.humanFmt;
const fmtDate_X  = window.fmtDate;
// ──────────────────────────────────────────────────────────────────────
// Cache TTL panel — stacked 5m + 1h hourly bars, with 5m share strip
// ──────────────────────────────────────────────────────────────────────

function CacheTTLPanel({ events, range, binMs }) {
  const ref = React.useRef(null), svgRef = React.useRef(null);
  const [size, setSize] = React.useState({ w: 1200, h: 320 });
  const [tip, setTip] = React.useState(null);
  const [yLabelPx, setYLabelPx] = React.useState(0);

  // Widest rendered y label. These are token counts, so their width is
  // unbounded, and a fixed gutter is a budget that gets consumed silently —
  // the same shape as the 100M/cumulative collision.
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
    const ro = new ResizeObserver(es => {
      const r = es[0].contentRect;
      setSize({ w: r.width, h: r.height });
    });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  const { w, h } = size;
  // padL grows with the y labels: they sit at padL - 6 and must clear the
  // rotated captions, whose boxes end at x~17. +35 keeps ~12px.
  const padR = 60, padT = 50;
  const padL = Math.min(
    Math.max(60, size.w * 0.15),
    Math.max(60, Math.ceil(yLabelPx) + 35)
  );
  const sharePctH = 56;        // bottom strip
  // Separates the main plot's baseline from the share strip's ceiling.
  // This has to clear the *labels*, not just the lines: the main axis "0"
  // sits in a 12px box centred on the baseline and the strip's "100%" in a
  // 10px box centred on shareTop, so anything under ~12 makes them collide
  // (at the old value of 6 they overlapped by 6px).
  const gap = 16;
  // The legend, in a band of its own BELOW the share strip (#633): at
  // translate(padL + 8, padT + 12) it sat INSIDE the plot rectangle,
  // across the bars it explains, and ran 8.8px past the panel's right
  // edge at a 320px viewport. Entries flow left to right and wrap to a
  // further row when the next would not fit; padB buys one row height per
  // row, so the band is sized from the legend it holds.
  const LEG_SW = 12, LEG_GAP = 6, LEG_SPACING = 12, LEG_ROW_H = 16;
  const legRef = React.useRef(null);
  const [legAdv, setLegAdv] = React.useState(0);
  // Advance of the 10px legend mono, measured off a rendered label — a
  // predicted advance disagrees with the real one (app.css letter-spacing;
  // the comparison panel measures its advance the same way).
  React.useLayoutEffect(() => {
    const t = legRef.current && legRef.current.querySelector('text');
    if (!t || !t.getComputedTextLength) return;
    const n = (t.textContent || '').length;
    if (!n) return;
    const a = t.getComputedTextLength() / n;
    if (a > 0 && Math.abs(a - legAdv) > 0.05) setLegAdv(a);
  });
  const LEGEND = [
    { key: '5m', label: 'ephemeral_5m', fill: COL_X.inputTokens },
    { key: '1h', label: 'ephemeral_1h', fill: COL_X.cacheCreateTokens },
  ];
  const legRows = [[]];
  {
    let x = 0, row = 0;
    for (const e of LEGEND) {
      const ew = LEG_SW + LEG_GAP + e.label.length * legAdv;
      if (x > 0 && x + ew > w - padR) { legRows.push([]); row += 1; x = 0; }
      legRows[row].push({ ...e, x });
      x += ew + LEG_SPACING;
    }
  }
  // padB: the x-axis band (the labels sit at shareBot + 14) plus one
  // legend row height per legend row.
  const padB = 36 + legRows.length * LEG_ROW_H;
  const plotW = Math.max(10, w - padL - padR);
  const plotH = Math.max(40, h - padT - padB - sharePctH - gap);

  // Trim x-range to where data actually exists (with small pad).
  // Otherwise a synthetic 3-month range with two days of real data shows
  // 90% empty space.
  const dataRange = React.useMemo(() => {
    const tsList = events
      .filter(e => (e.ephemeral_5m || 0) + (e.ephemeral_1h || 0) > 0)
      .map(e => e.ts);
    if (!tsList.length) return range;
    const dMin = Math.min(...tsList);
    const dMax = Math.max(...tsList);
    const span = Math.max(dMax - dMin, 60_000);
    const pad = span * 0.04;
    return { start: dMin - pad, end: dMax + pad };
  }, [events, range.start, range.end]);

  // Cache TTL trims its visible range to non-zero cache data, but it still
  // cannot choose bins finer than the dashboard/server aggregation floor.
  const adaptiveBin = React.useMemo(
    () => window.cacheTtlBinMs(dataRange, binMs),
    [dataRange.start, dataRange.end, binMs]
  );

  const useRange = dataRange;
  const useBin = adaptiveBin;

  // Build bins, snapping start to bin boundary so labels read cleanly
  const bins = React.useMemo(() => {
    const arr = [];
    let bStart = Math.floor(useRange.start / useBin) * useBin;
    const end = Math.ceil(useRange.end / useBin) * useBin;
    const sorted = events.slice().sort((a, b) => a.ts - b.ts);
    let i = 0;
    while (i < sorted.length && sorted[i].ts < bStart) i++;
    while (bStart < end) {
      const bEnd = bStart + useBin;
      let s5 = 0, s1 = 0, n = 0;
      while (i < sorted.length && sorted[i].ts < bEnd) {
        s5 += sorted[i].ephemeral_5m || 0;
        s1 += sorted[i].ephemeral_1h || 0;
        n++; i++;
      }
      arr.push({ start: bStart, end: bEnd, s5, s1, n });
      bStart = bEnd;
    }
    return arr;
  }, [events, useRange.start, useRange.end, useBin]);

  let total5 = 0, total1 = 0, maxBin = 1;
  for (const b of bins) {
    total5 += b.s5;
    total1 += b.s1;
    const t = b.s5 + b.s1;
    if (t > maxBin) maxBin = t;
  }

  const xScale = ts => padL + ((ts - useRange.start) / (useRange.end - useRange.start)) * plotW;
  const yBar = v => padT + plotH - (v / maxBin) * plotH;
  const barW = Math.max(2, (plotW / Math.max(1, bins.length)) * 0.9);

  // y ticks
  function niceTicks(maxV, n = 4) {
    if (maxV <= 0) return [0];
    const step0 = maxV / n;
    const exp = Math.pow(10, Math.floor(Math.log10(step0)));
    const norm = step0 / exp;
    const niceStep = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * exp;
    const arr = [];
    for (let v = 0; v <= maxV; v += niceStep) arr.push(v);
    return arr;
  }
  const yTicks = niceTicks(maxBin);

  // x ticks (adaptive: months / days / hours)
  const xTicks = window.timeTicksUTC(useRange.start, useRange.end);

  // Share strip Y origin
  const shareTop = padT + plotH + gap;
  const shareBot = shareTop + sharePctH;

  // Median + p95 of share %
  const sharePct = bins.map(b => {
    const t = b.s5 + b.s1;
    return t > 0 ? (b.s5 / t) * 100 : null;
  });
  const validShares = sharePct.filter(v => v !== null).sort((a, b) => a - b);
  const median = validShares.length ? validShares[Math.floor(validShares.length / 2)] : null;
  const p95 = validShares.length ? validShares[Math.floor(validShares.length * 0.95)] : null;

  function shareY(p) { return shareBot - (p / 100) * sharePctH; }

  // Hit-test in the <svg>'s frame: padT/shareBot are svg coordinates and
  // the container's rect carries the panel's 1px border, so comparing
  // across the frames killed the share strip's bottom row and parked a
  // live 1px band over the title area (#696). The tip still positions
  // in container coordinates — its offsetParent.
  function onMove(e) {
    const rect = ref.current.getBoundingClientRect(), srect = svgRef.current.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const sx = e.clientX - srect.left, sy = e.clientY - srect.top;
    if (sx < padL || sx > w - padR || sy < padT || sy > shareBot) {
      setTip(null); return;
    }
    const frac = (sx - padL) / plotW;
    const ts = useRange.start + frac * (useRange.end - useRange.start);
    let idx = Math.floor((ts - bins[0].start) / useBin);
    if (idx < 0) idx = 0;
    if (idx >= bins.length) idx = bins.length - 1;
    const b = bins[idx];
    const tot = b.s5 + b.s1;
    const pct = tot > 0 ? (b.s5 / tot) * 100 : 0;
    setTip({
      x: mx, y: my, idx,
      title: `${fmtDate_X(b.start, {day:true})}`,
      accent: COL_X.cacheCreateTokens,
      lines: [
        ['ephemeral 5m', humanFmt_X(b.s5)],
        ['ephemeral 1h', humanFmt_X(b.s1)],
        ['total',        humanFmt_X(tot)],
        ['5m share',     tot > 0 ? pct.toFixed(1) + '%' : '—'],
        ['records',      String(b.n)],
      ],
    });
  }

  const grandTotal = total5 + total1;
  const sharePctOverall = grandTotal > 0 ? (total5 / grandTotal) * 100 : 0;
  const a11y = window.useChartA11y(
    'Prompt-Cache TTL Split',
    `stacked bars, ${bins.length} bins, 5m share ${sharePctOverall.toFixed(1)}%`,
    `Cache-create tokens per bucket, ephemeral 5m stacked over ephemeral `
    + `1h. 5m total ${humanFmt_X(total5)}, 1h total ${humanFmt_X(total1)}.`);

  return (
    <div ref={ref} style={{
      background: TH_X.bgAxes, border: `1px solid ${TH_X.border}`,
      borderRadius: 4, padding: 0, position: 'relative', minHeight: 320,
    }}
    onMouseMove={onMove}
    onMouseLeave={() => setTip(null)}>
      <svg ref={svgRef} role="img" aria-label={a11y.label} aria-describedby={a11y.descId}
        data-panel="Prompt-Cache TTL Split" width={w} height={h} style={{ display: 'block' }}>
        {/* Title */}
        <text data-role="title" x={w/2} y={20} fontSize="14" fontWeight="bold" fill={TH_X.text}
          textAnchor="middle" fontFamily="monospace">
          Prompt-Cache TTL Split
        </text>
        <text x={w/2} y={36} fontSize="10" fill={TH_X.textDim}
          textAnchor="middle" fontFamily="monospace">
          {bins.length.toLocaleString()} bins · 5m {humanFmt_X(total5)} · 1h {humanFmt_X(total1)} · 5m share {sharePctOverall.toFixed(1)}%
        </text>

        {/* Y grid */}
        <rect data-role="plot" x={padL} y={padT} width={plotW} height={plotH} fill="none" />{yTicks.map((v, i) => (
          <line key={'g'+i} x1={padL} x2={w - padR}
            y1={yBar(v)} y2={yBar(v)}
            stroke={TH_X.grid} strokeOpacity="0.3" />
        ))}

        {/* Stacked bars: 1h on bottom, 5m on top */}
        {bins.map((b, idx) => {
          if (b.s5 + b.s1 <= 0) return null;
          const x = xScale(b.start);
          const y1 = yBar(b.s1);                    // top of 1h
          const y5 = yBar(b.s5 + b.s1);             // top of 5m
          const h1 = padT + plotH - y1;             // 1h bar height
          const h5 = y1 - y5;                       // 5m bar height
          // The family's ONE hover treatment (#697): bars rest a dim 0.3
          // field and the hovered bin lifts to 0.85, keyed on the BIN so
          // both stacked segments are marked together. The old peak-bin
          // annotation folded away: resting the peak pre-lifted at 0.85
          // made its own hover a no-op — the interaction gate fails any
          // hoverable mark that does not visibly respond — and the peak
          // is already the tallest bar, self-annotating.
          const isHover = tip != null && tip.idx === idx;
          return (
            <g key={'bar'+idx}>
              <rect data-hover-target="" x={x} y={y1} width={barW}
                height={Math.max(0, h1)}
                fill={COL_X.cacheCreateTokens}
                fillOpacity={isHover ? 0.85 : 0.3} />
              <rect data-hover-target="" x={x} y={y5} width={barW}
                height={Math.max(0, h5)}
                fill={COL_X.inputTokens}
                fillOpacity={isHover ? 0.85 : 0.3} />
            </g>
          );
        })}

        {/* Crosshair, snapped to the hovered bin's bar centre (#646). */}
        {tip && bins[tip.idx] && (
          <line x1={xScale(bins[tip.idx].start) + barW / 2}
            x2={xScale(bins[tip.idx].start) + barW / 2} y1={padT} y2={shareBot}
            stroke="#fff" strokeOpacity="0.25" strokeWidth="1" strokeDasharray="2,3" />
        )}

        {/* Y-axis labels */}
        <g data-role="axis">{yTicks.map((v, i) => (
          <text data-yl-label="" key={'yl'+i} x={padL - 6} y={yBar(v) + 4}
            fontSize="9" fill={TH_X.textDim} textAnchor="end" fontFamily="monospace">
            {humanFmt_X(v)}
          </text>
        ))}</g>

        {/* Top-panel y label */}
        <text data-role="axis" x={14} y={padT + plotH/2} fontSize="9" fill={TH_X.textDim}
          textAnchor="middle" fontFamily="monospace"
          transform={`rotate(-90 14 ${padT + plotH/2})`}>cache_create / bin</text>

        {/* Legend, below the share strip in the band padB bought for it
            (#633). The outer group is the marked region the layout guard
            reads; rows translate within it. */}
        <g data-role="legend" ref={legRef}
          transform={`translate(${padL}, ${shareBot + 26})`}>
          {legRows.map((row, ri) => (
            <g key={'legrow' + ri} transform={`translate(0, ${ri * LEG_ROW_H})`}>
              {row.map(e => (
                <g key={e.key}>
                  <rect x={e.x} y={0} width={LEG_SW} height={LEG_SW}
                    fill={e.fill} fillOpacity="0.85" />
                  <text x={e.x + LEG_SW + LEG_GAP} y={10} fontSize="10"
                    fill={TH_X.text} fontFamily="monospace">{e.label}</text>
                </g>
              ))}
            </g>
          ))}
        </g>

        {/* Share strip background */}
        <rect x={padL} y={shareTop} width={plotW} height={sharePctH}
          fill="#0f1428" fillOpacity="0.6" />

        {/* Share strip: one continuous line+area that linearly
            interpolates across empty bins (no cache_create activity)
            so sparse data still reads as a single trace. The line is
            also extended half a bucket past each end with a linear
            extrapolation of the share value, so the visual reaches
            the full plot width. */}
        {(() => {
          const valid = [];
          for (let i = 0; i < bins.length; i++) {
            const v = sharePct[i];
            if (v !== null) valid.push({ ts: bins[i].start, share: v });
          }
          if (valid.length < 2) return null;
          const binDur = bins[0].end - bins[0].start;
          const half = binDur / 2;
          const f = valid[0], s = valid[1];
          const l = valid[valid.length - 1], p2 = valid[valid.length - 2];
          const clampPct = v => Math.max(0, Math.min(100, v));
          const extended = [
            { ts: f.ts - half, share: clampPct(1.5 * f.share - 0.5 * s.share) },
            ...valid,
            { ts: l.ts + half, share: clampPct(1.5 * l.share - 0.5 * p2.share) },
          ];
          const xy = extended.map(p =>
            ({ x: xScale(p.ts) + barW / 2, y: shareY(p.share) })
          );
          const fill = `M ${xy[0].x},${shareBot} ` +
            xy.map(p => `L ${p.x},${p.y}`).join(' ') +
            ` L ${xy[xy.length-1].x},${shareBot} Z`;
          const line = `M ` + xy.map(p => `${p.x},${p.y}`).join(' L ');
          return (
            <g>
              <path d={fill} fill={COL_X.inputTokens} fillOpacity="0.20" />
              <path d={line} stroke={COL_X.inputTokens} strokeWidth="1.2" fill="none" />
            </g>
          );
        })()}

        {/* Reference lines on share strip */}
        {median !== null && (
          <g>
            <line x1={padL} x2={w - padR} y1={shareY(median)} y2={shareY(median)}
              stroke={TH_X.textDim} strokeWidth="0.8" strokeOpacity="0.6" strokeDasharray="3,3" />
            <text x={w - padR - 4} y={shareY(median) - 3} fontSize="9"
              fill={TH_X.textDim} textAnchor="end" fontFamily="monospace">
              median {median.toFixed(0)}%
            </text>
          </g>
        )}
        {p95 !== null && (
          <g>
            <line x1={padL} x2={w - padR} y1={shareY(p95)} y2={shareY(p95)}
              stroke={TH_X.textDim} strokeWidth="0.8" strokeOpacity="0.6" strokeDasharray="3,3" />
            <text x={w - padR - 4} y={shareY(p95) - 3} fontSize="9"
              fill={TH_X.textDim} textAnchor="end" fontFamily="monospace">
              p95 {p95.toFixed(0)}%
            </text>
          </g>
        )}

        {/* Share strip y labels */}
        <g data-role="axis">{[0, 50, 100].map((p, i) => (
          <text key={'sy'+i} x={padL - 6} y={shareY(p) + 3}
            fontSize="8" fill={TH_X.textDim} textAnchor="end" fontFamily="monospace">
            {p}%
          </text>
        ))}</g>
        <text data-role="axis" x={14} y={shareTop + sharePctH/2} fontSize="9" fill={TH_X.textDim}
          textAnchor="middle" fontFamily="monospace"
          transform={`rotate(-90 14 ${shareTop + sharePctH/2})`}>5m share</text>

        {/* X-axis labels (under share strip) */}
        <g data-role="axis">{xTicks.map((t, i) => (
          <text key={'x'+i} x={xScale(t.ts)} y={shareBot + 14}
            fontSize="9" fill={TH_X.textDim} textAnchor="middle" fontFamily="monospace">
            {t.label}
          </text>
        ))}</g>

        {/* Strip border */}
        <rect x={padL} y={shareTop} width={plotW} height={sharePctH}
          fill="none" stroke={TH_X.border} strokeOpacity="0.6" />
      </svg>
      {a11y.descText && (
        <span className="sr-only" id={a11y.descId}>{a11y.descText}</span>
      )}
      {tip && <window.DashTooltip tip={tip} />}
    </div>
  );
}

window.CacheTTLPanel = CacheTTLPanel;
