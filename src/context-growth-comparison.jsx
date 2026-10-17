// Measured bands stack the title, plot, x ticks, caption and wrapped
// legend rows. Fixed pitches can overlap long model names and clip the
// last wrapped row. Pure packing lives in src/panel-layout.js so it can
// be tested without a browser.

const TH_X = window.dashboardTheme;
const humanFmt_X = window.humanFmt;
const perTurnStats = window.perTurnStats;

// The fixed string both advance probes are measured from: letters and
// digits only, so no glyph in it is a special width.
const PROBE_CHARS = '0123456789abcdefghij';

function ComparisonRow({ models, byModel, w, h, showSessions }) {
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
  // row off the bottom at phone width.
  const padL = 60, padR = 30, padT = 30, padB = 60;
  const plotW = Math.max(10, w - padL - padR);
  const plotH = Math.max(10, h - padT - padB);

  // One stats bundle per checked model, in the same order as `models`.
  // The session list rides along because the axis below is scaled from
  // it, not from the percentiles: see the axis note further down.
  const series = React.useMemo(() => models.map(m => {
    const sessions = byModel[m.model] || [];
    return { model: m.model, count: sessions.length, sessions,
             stats: perTurnStats(sessions) };
  }), [models, byModel]);

  // The axis uses the shared observed-data rule: peak context among every
  // turn of every session of every CHECKED model, plus headroom. Shared
  // axes are the point of the single chart: a model checked beside
  // a taller one sits lower, and that is the comparison working.
  const yMax = window.ctxAxis.ctxAxisTopFor(series.map(s => s.sessions));
  const xMax = Math.max(1, ...series.map(s => s.stats.maxT || 0));
  const xScale = t => padL + (t / xMax) * plotW;
  const yScale = v => padT + plotH - (v / yMax) * plotH;

  const yTicks = window.ctxAxis.ctxAxisTicks(yMax, 5);
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
      lines.push([`${s.model} p25–p75`, `${fmt(s.stats.p25[turn])}–${fmt(s.stats.p75[turn])}`]);
      lines.push([`${s.model} p90`, fmt(s.stats.p90[turn])]);
      lines.push([`${s.model} active`, `${live} / ${s.count}`]);
    }
    setTip({ x: mx, y: my, cx: xScale(turn), bi: turn, title: `turn ${turn}`, accent: '#ffffff', lines });
  }

  const titleText = series.length === 0
    ? 'select models above to compare'
    : series.length === 1
      ? `${series[0].model}  ·  median per turn`
      : series.map(s => s.model).join(' vs ') + '  ·  median per turn';
  const shownTitle = window.panelLayout.fitText(
    titleText, titleAdv || 7.8, Math.max(24, w - padL - padR));
  const legend = window.panelLayout.packLegend(
    series, adv || 5.7, plotW, 16, 18);
  // The static legend-key row: what each mark IS, once, in a fixed-size
  // row that never grows with the model count. Entries
  // carry a null count, which packs as no count text at all; the
  // `sessions` key appears only while the traces are on.
  const keyEntries = [
    { model: 'median', count: null },
    { model: 'p25–p75', count: null },
    { model: 'p90', count: null },
    ...(showSessions ? [{ model: 'sessions', count: null }] : []),
  ];
  const keyLegend = window.panelLayout.packLegend(
    keyEntries, adv || 5.7, plotW, 16, 18);
  const legendTop = padT + plotH + 46;
  const keyTop = legendTop + Math.max(1, legend.rows.length) * 16 + 4;
  const svgH = keyTop + Math.max(1, keyLegend.rows.length) * 16 + 8;
  const a11y = window.useChartA11y(
    'Context Growth — comparison',
    `median context per turn, ${series.length} models`,
    series.length
      ? `Compared: ${series.map(s => `${s.model} (${s.count} files)`).join(', ')}.`
      : null);

  // The static key's marks, keyed by its entry label. Each mark shows the
  // shape a CHECKED model's line carries, in the neutral text colour: the
  // colour itself is the model's, and the checkbox beside its name is
  // that colour key.
  function keyMark(label, x, y) {
    const c = TH_X.text;
    switch (label) {
      case 'median':
        return <line x1={x} x2={x + 16} y1={y + 5} y2={y + 5}
          stroke={c} strokeWidth="2" />;
      case 'p90':
        return <line x1={x} x2={x + 16} y1={y + 5} y2={y + 5}
          stroke={c} strokeWidth="1.2" strokeDasharray="4,3" />;
      case 'sessions':
        return <line x1={x} x2={x + 16} y1={y + 5} y2={y + 5}
          stroke={c} strokeWidth="0.7" strokeOpacity="0.6" />;
      default: // 'p25–p75'
        return <rect x={x} y={y + 1} width={16} height={8}
          fill={c} fillOpacity="0.25" />;
    }
  }

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

        {/* The static legend-key row — what each mark is. Packed and
            wrapped by the same packer, so it fits at 320px the way the
            model clusters above it do. */}
        <g>
          {keyLegend.rows.flat().map(c => {
            return (
              <g key={'key-' + c.label} data-role="legend"
                transform={`translate(${padL + c.x}, ${keyTop + c.y})`}>
                {keyMark(c.label, c.ruleX, 0)}
                <text x={9} y={9} fontSize="9.5" fontWeight="700" fill={TH_X.text} fontFamily="monospace">{c.text}</text>
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

        {/* Per-session traces — behind everything, at low opacity, in the
            model's colour, gated by the sessions toggle. */}
        {showSessions && series.map(s => {
          const c = (window.modelColors && window.modelColors[s.model]) || '#888';
          return s.sessions.map((sess, i) => {
            const pts = [];
            for (const p of sess.seq) {
              pts.push(`${xScale(p.t)},${yScale(Math.min(p.ctx, yMax))}`);
            }
            if (pts.length < 2) return null;
            return (
              <polyline key={`tr-${s.model}-${i}`} points={pts.join(' ')}
                stroke={c} strokeWidth="0.7" strokeOpacity="0.25" fill="none" />
            );
          });
        })}

        {/* p25–p75 band per checked model — translucent, in the model's
            colour. */}
        {series.map(s => {
          const c = (window.modelColors && window.modelColors[s.model]) || '#888';
          const top = [], bot = [];
          for (let i = 0; i < s.stats.turns.length; i++) {
            const lo = s.stats.p25[i], hi = s.stats.p75[i];
            if (lo == null || hi == null) continue;
            top.push(`${xScale(s.stats.turns[i])},${yScale(Math.min(hi, yMax))}`);
            bot.push(`${xScale(s.stats.turns[i])},${yScale(Math.min(lo, yMax))}`);
          }
          if (top.length < 2) return null;
          const ribbon = `M ${top.join(' L ')} L ${bot.reverse().join(' L ')} Z`;
          return <path key={'band-'+s.model} d={ribbon}
            fill={c} fillOpacity="0.25" stroke="none" />;
        })}

        {/* p90 line per checked model, dashed so it cannot be read as
            the median. */}
        {series.map(s => {
          const c = (window.modelColors && window.modelColors[s.model]) || '#888';
          return (
            <polyline key={'p90-'+s.model}
              points={buildLine(s.stats.turns, s.stats.p90)}
              stroke={c} strokeWidth="1.2" strokeDasharray="4,3" fill="none" />
          );
        })}

        {/* Median line per checked model, on top of band and p90. */}
        {series.map(s => {
          const c = (window.modelColors && window.modelColors[s.model]) || '#888';
          return (
            <polyline key={'med-'+s.model} points={buildLine(s.stats.turns, s.stats.median)}
              stroke={c} strokeWidth="2" fill="none" />
          );
        })}

        {/* The plot AREA, for the layout guard. It has to be a real
            element: an empty region measures as nothing, and the series
            that occupy it are drawn at a hundred different sizes, so
            none of them can stand in for the region they share. */}
        <rect data-role="plot" x={padL} y={padT} width={plotW} height={plotH}
          fill="none" pointerEvents="none" />

        {/* Crosshair */}
        {tip && (
          <line x1={tip.cx} x2={tip.cx} y1={padT} y2={padT + plotH}
            stroke="#fff" strokeOpacity="0.3" strokeDasharray="2,3" />
        )}

        {/* Hit columns, one per turn: invisible at rest, the
            hovered turn's column lifts when the tooltip snaps to it. */}
        {Array.from({ length: xMax + 1 }, (_, turn) => (
          <rect key={'hit' + turn} data-hover-target=""
            x={turn === 0 ? padL
              : padL + (turn - 0.5) * plotW / xMax} y={padT}
            width={turn === 0 ? plotW / xMax / 2 : plotW / xMax}
            height={plotH} fill="#fff"
            fillOpacity={tip && tip.bi === turn ? 0.12 : 0} />
        ))}

        {/* Draw every tick; there is no separate cap label to collide with. */}
        <g data-role="axis">
        {yTicks.map((v, i) => (
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
        {/* Stack the centred caption and left-anchored legend in separate
            bands so they cannot overlap at narrower widths. */}
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
