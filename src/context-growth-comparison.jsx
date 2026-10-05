// The Per-Session Context Growth COMPARISON panel — the overlay that draws
// every checked model's median context per turn on one set of axes.
//
// Split out of dashboard-charts-extra.jsx (issue #630): the panel grew a
// measured-advance layout pass that the file's committed size entry had no
// room for, and the size ratchet's own rule is that an outgrown file moves
// code into a new module. Nothing about the panel changed but where it
// lives.
//
// Its layout is BANDED, top to bottom: title, plot, x ticks, axis caption,
// then one legend row per wrapped row. Every band is measured rather than
// assumed. At the fixed offsets the panel used before, a 17-character
// model name overran the count label beside it by 9.5px and through the
// swatch rule, and the wrapped legend row fell off the bottom of a
// fixed-height svg, which clipped it away (#630). The packing arithmetic
// lives in src/panel-layout.js as pure functions, so it is testable
// without a browser.

const TH_X = window.dashboardTheme;
const humanFmt_X = window.humanFmt;
const perTurnStats = window.perTurnStats;
const capForModel = window.capForModel;

// The fixed string both advance probes are measured from: letters and
// digits only, so no glyph in it is a special width.
const PROBE_CHARS = '0123456789abcdefghij';

function ComparisonRow({ models, byModel, w, h }) {
  const ref = React.useRef(null);
  const [tip, setTip] = React.useState(null);
  // Measured advance of this panel's two monospace text roles, read off a
  // real rendered string each. Predicted advances disagree with the real
  // one, and both the legend packing and the title fit are arithmetic on
  // character counts — so the counts are only as good as this number.
  const legRef = React.useRef(null);
  const titleRef = React.useRef(null);
  const [adv, setAdv] = React.useState(0);
  const [titleAdv, setTitleAdv] = React.useState(0);
  // Measured off two FIXED probe strings parked off-canvas, never off the
  // text being laid out. Measuring the laid-out label instead is circular:
  // fitText may replace it with an ellipsis, whose glyph is wider than the
  // monospace advance, so the measured average moves, the pack changes,
  // and the panel re-renders forever.
  React.useLayoutEffect(() => {
    for (const [r, set, cur] of [
      [legRef, setAdv, adv], [titleRef, setTitleAdv, titleAdv]]) {
      const t = r.current;
      if (!t || !t.getComputedTextLength) continue;
      const a = t.getComputedTextLength() / PROBE_CHARS.length;
      if (a > 0 && Math.abs(a - cur) > 0.05) set(a);
    }
  });

  // The PLOT keeps the height it had; everything below it is banded (x
  // ticks, caption, then one band per legend row) and the svg grows to
  // fit those bands. A fixed svg height is what clipped the wrapped legend
  // row off the bottom at phone width (issue #630).
  const padL = 60, padR = 30, padT = 30, padB = 60;
  const plotW = Math.max(10, w - padL - padR);
  const plotH = Math.max(10, h - padT - padB);

  // One stats bundle per checked model, in the same order as `models`.
  const series = React.useMemo(() => models.map(m => {
    const sessions = byModel[m.model] || [];
    return { model: m.model, count: sessions.length, stats: perTurnStats(sessions) };
  }), [models, byModel]);

  // Adaptive cap: 1M when any opus is in the comparison, else 200k. Then
  // expand if the data exceeds it.
  let observedMax = 0;
  for (const s of series) for (const v of s.stats.p90) if (v && v > observedMax) observedMax = v;
  const baseCap = Math.max(200_000, ...series.map(s => capForModel(s.model)));
  const cap = Math.max(baseCap, observedMax * 1.05);
  const yMax = cap * 1.05;
  // Dynamic x-domain: max turn across all checked models
  const xMax = Math.max(1, ...series.map(s => s.stats.maxT || 0));
  const xScale = t => padL + (t / xMax) * plotW;
  const yScale = v => padT + plotH - (v / yMax) * plotH;

  function yTickValues(maxV, n = 5) {
    if (maxV <= 0) return [0];
    const step0 = maxV / n;
    const exp = Math.pow(10, Math.floor(Math.log10(step0)));
    const norm = step0 / exp;
    const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * exp;
    const arr = [];
    for (let v = 0; v <= maxV; v += step) arr.push(v);
    return arr;
  }
  const yTicks = yTickValues(cap, 5);
  function xTickValues(maxV, n = 6) {
    if (maxV <= 0) return [0];
    const step0 = maxV / n;
    const exp = Math.pow(10, Math.floor(Math.log10(step0)));
    const norm = step0 / exp;
    const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * exp;
    const arr = [];
    for (let v = 0; v <= maxV; v += step) arr.push(Math.round(v));
    if (arr[arr.length - 1] !== maxV && (maxV - arr[arr.length - 1]) / step > 0.4) arr.push(maxV);
    return arr;
  }
  const xTicks = xTickValues(xMax);

  function buildLine(turns, vals) {
    const pts = [];
    for (let i = 0; i < turns.length; i++) {
      if (vals[i] === null || vals[i] === undefined) continue;
      pts.push(`${xScale(turns[i])},${yScale(Math.min(vals[i], yMax))}`);
    }
    return pts.join(' ');
  }

  function onMove(e) {
    const rect = ref.current.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    if (mx < padL || mx > w - padR || my < padT || my > padT + plotH) {
      setTip(null); return;
    }
    const turn = Math.round(((mx - padL) / plotW) * xMax);
    if (turn < 0 || turn > xMax) { setTip(null); return; }
    const fmt = v => v !== null && v !== undefined ? humanFmt_X(v) : '—';
    const lines = [];
    for (const s of series) {
      const live = s.stats.count[turn] || 0;
      lines.push([`${s.model} median`, fmt(s.stats.median[turn])]);
      lines.push([`${s.model} active`, `${live} / ${s.count}`]);
    }
    setTip({ x: mx, y: my, title: `turn ${turn}`, accent: '#ffffff', lines });
  }

  const titleText = series.length === 0
    ? 'select models above to compare'
    : series.length === 1
      ? `${series[0].model}  ·  median per turn`
      : series.map(s => s.model).join(' vs ') + '  ·  median per turn';
  // The title is fitted to the panel, not trusted to fit: at a phone
  // width it is 401.8px of text in a 329px svg and simply ran off the
  // right edge (#630).
  const shownTitle = window.panelLayout.fitText(
    titleText, titleAdv || 7.8, Math.max(24, w - padL - padR));
  // Legend clusters, packed from the measured advance and wrapping when
  // they run out of width; the row count is what sizes the svg below.
  const legend = window.panelLayout.packLegend(
    series, adv || 5.7, plotW, 16, 18);
  const legendTop = padT + plotH + 46;
  const svgH = legendTop + Math.max(1, legend.rows.length) * 16 + 8;
  const a11y = window.useChartA11y(
    'Context Growth — comparison',
    `median context per turn, ${series.length} models`,
    series.length
      ? `Compared: ${series.map(s => `${s.model} (${s.count} files)`).join(', ')}.`
      : null);

  return (
    <div ref={ref} style={{ position: 'relative', borderBottom: `1px solid ${TH_X.border}` }}
      onMouseMove={onMove} onMouseLeave={() => setTip(null)}>
      <svg role="img" aria-label={a11y.label} aria-describedby={a11y.descId}
        data-panel="Context Growth — comparison" width={w} height={svgH} style={{ display: 'block' }}>
        <text data-role="title" x={padL} y={20} fontSize="11" fontWeight="bold" fill={TH_X.text}
          fontFamily="monospace">
          {shownTitle}
        </text>

        {/* Off-canvas advance probes. Unmarked and invisible: they exist
            only so the layout above has a real rendered string to measure. */}
        <text ref={legRef} x="-9999" y="-9999" fontSize="9.5" opacity="0"
          fontFamily="monospace">{PROBE_CHARS}</text>
        <text ref={titleRef} x="-9999" y="-9999" fontSize="11" fontWeight="bold"
          opacity="0" fontFamily="monospace">{PROBE_CHARS}</text>

        {/* Legend — one cluster per checked model, packed from the
            measured advance so a long model name cannot run into the
            count beside it, and wrapped so the last row is drawn rather
            than clipped by the svg edge. */}
        <g>
          {legend.rows.flat().map(c => {
            const col = (window.modelColors && window.modelColors[c.label]) || '#888';
            return (
              <g key={c.label} data-role="legend"
                transform={`translate(${padL + c.x}, ${legendTop + c.y})`}>
                <rect x={0} y={0} width={4} height={12} fill={col} />
                <text x={9} y={9} fontSize="9.5" fontWeight="700" fill={col} fontFamily="monospace">{c.text}</text>
                <line x1={c.ruleX} x2={c.ruleX + 16} y1={5} y2={5} stroke={col} strokeWidth="2" />
                <text x={c.countX} y={9} fontSize="9.5" fill={TH_X.text} fontFamily="monospace">
                  {c.countText}
                </text>
              </g>
            );
          })}
        </g>

        {/* Y grid */}
        {yTicks.map((v, i) => (
          <line key={'g'+i} x1={padL} x2={w - padR}
            y1={yScale(v)} y2={yScale(v)}
            stroke={TH_X.grid} strokeOpacity="0.25" />
        ))}

        {/* Cap line */}
        <line x1={padL} x2={w - padR} y1={yScale(cap)} y2={yScale(cap)}
          stroke="#ff5577" strokeWidth="1" strokeDasharray="2,3" strokeOpacity="0.7" />
        <text x={padL - 6} y={yScale(cap) + 3} fontSize="9"
          fill="#ff5577" textAnchor="end" fontFamily="monospace">{humanFmt_X(cap)}</text>

        {/* Median line per checked model. p90 dropped — overlapping
            dashed lines for 2+ models read as noise, and per-model
            spread is already shown in the sub-panels below as IQR
            ribbons. */}
        {series.map(s => {
          const c = (window.modelColors && window.modelColors[s.model]) || '#888';
          return (
            <polyline key={'med-'+s.model} points={buildLine(s.stats.turns, s.stats.median)}
              stroke={c} strokeWidth="2" fill="none" />
          );
        })}

        {/* The plot AREA, for the layout guard (#631). It has to be a real
            element: an empty region measures as nothing, and the series
            that occupy it are drawn at a hundred different sizes, so
            none of them can stand in for the region they share. */}
        <rect data-role="plot" x={padL} y={padT} width={plotW} height={plotH}
          fill="none" pointerEvents="none" />

        {/* Crosshair */}
        {tip && (
          <line x1={tip.x} x2={tip.x} y1={padT} y2={padT + plotH}
            stroke="#fff" strokeOpacity="0.3" strokeDasharray="2,3" />
        )}

        {/* Y labels. A tick within one label-height of the cap line is
            dropped: cap is max(baseCap, observedMax * 1.05), so it lands
            just off a round tick — 1.02M against a 1M tick sat 3.5px away
            and the two labels overlapped. The cap label wins, being the
            one that carries meaning. */}
        <g data-role="axis">
        {yTicks.filter(v => Math.abs(yScale(v) - yScale(cap)) >= 12).map((v, i) => (
          <text key={'yl'+i} x={padL - 9} y={yScale(v) + 3}
            fontSize="9" fill={TH_X.textDim} textAnchor="end" fontFamily="monospace">
            {humanFmt_X(v)}
          </text>
        ))}
        </g>
        {/* X labels */}
        <g data-role="axis">
        {xTicks.map((t, i) => (
          <text key={'x'+i} x={xScale(t)} y={padT + plotH + 14}
            fontSize="9" fill={TH_X.textDim} textAnchor="middle" fontFamily="monospace">
            {t}
          </text>
        ))}
        </g>
        <text data-role="axis" x={14} y={padT + plotH/2} fontSize="9" fill={TH_X.textDim}
          textAnchor="middle" fontFamily="monospace"
          transform={`rotate(-90 14 ${padT + plotH/2})`}>context size</text>
        {/* The caption gets its own band between the x ticks and the legend.
            Pinned to the bottom (h - 10) it shared a 1px-apart band with the
            legend, and since it is centred while the legend is left-anchored
            at fixed 270px clusters, narrower viewports slid the caption into
            a legend entry — at 1224px "median (1,314 files)" overlapped it by
            7.5px. Stacking the bands makes horizontal position irrelevant. */}
        <text data-role="axis" x={(padL + w - padR)/2} y={padT + plotH + 33} fontSize="9" fill={TH_X.textDim}
          textAnchor="middle" fontFamily="monospace">turn number within session</text>
      </svg>
      {a11y.descText && (
        <span className="sr-only" id={a11y.descId}>{a11y.descText}</span>
      )}
      {tip && <window.DashTooltip tip={tip} />}
    </div>
  );
}

window.ComparisonRow = ComparisonRow;
