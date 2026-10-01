// Page performance readout (issue #436) — the browser half of the
// journey, layout-stability and blocking telemetry that src/perf.js
// beacons, read back from /api/web-metrics.
//
// Modelled on ReplyLatencyPanel: a self-fetching panel taking
// { range, nonce }, fetching its own endpoint with
// credentials: 'same-origin', mounted alongside the other backend panels
// so its request goes out in parallel with /api/dashboard rather than
// waiting for it.
//
// Three properties of the data shape what is drawn here.
//
// `series` is the POOLED range-level readout and `buckets` the same grain
// one display bucket at a time. `exact` is true only on the live pass;
// on the rollup pass `series` is an n-weighted blend of per-bucket
// values, so the panel says which it is showing rather than drawing an
// approximation as a measurement. Nothing here re-derives a value from
// what it was given — a browser-side average of values is a value nobody
// measured.
//
// src/perf.js never beacons a part it did not time, so a MISSING row is
// real information, not a zero: an unmeasured part renders as an em
// dash. The observed metrics are read as the sums they are — a
// cumulative layout shift is a sum of shift values, and blocking time is
// a sum of task durations.
//
// And an unmeasured range is not an empty one: with no series the panel
// renders nothing at all, the way every cost surface is gated on there
// being cost in view. Zeros here would say "we never measured this" in
// the visual language of "we measured nothing wrong".

const { useState, useEffect } = React;

// Milliseconds read the way ReplyLatencyPanel reads seconds.
function perfMs(v) {
  if (v == null || typeof v !== 'number' || !isFinite(v)) return '—';
  if (v < 1) return v.toFixed(2) + ' ms';
  if (v < 1000) return Math.round(v) + ' ms';
  return (v / 1000).toFixed(2) + ' s';
}

// A dimensionless Layout Instability score: four decimal places, because
// the value that matters ("good", "needs improvement", "poor") lives in
// the first two.
function perfScore(v) {
  if (v == null || typeof v !== 'number' || !isFinite(v)) return '—';
  return v.toFixed(4);
}

// One row's p50 / p75, or the em dash when the client never measured it.
// The condition is on the ROW, not on the value: a measured zero and a
// never-timed part are different facts.
function perfPair(row, fmt) {
  return !row ? '—' : fmt(row.p50) + ' / ' + fmt(row.p75);
}

const PERF_CELL = {
  padding: '3px 8px', textAlign: 'right', whiteSpace: 'nowrap',
  fontFamily: 'var(--mono)', fontSize: 11,
};
const PERF_HEAD = Object.assign({}, PERF_CELL, {
  textAlign: 'left', color: 'var(--muted)', fontWeight: 600,
});

function WebMetricsPanel({ range, nonce }) {
  const [body, setBody] = useState(null);

  useEffect(() => {
    // `range` only. A `project=` was appended here and silently ignored —
    // /api/web-metrics has no project filter, because a browser beacon
    // carries no project: the client is a page, not a session on a project.
    // A parameter the server drops is worse than no parameter, since the
    // URL claims a narrowing the answer does not have.
    fetch(`/api/web-metrics?range=${range || 'all'}`, { credentials: 'same-origin' })
      .then(r => r.json())
      .then(b => setBody(b))
      .catch(err => console.error('web-metrics fetch failed', err));
  }, [range, nonce]);

  const series = (body && body.series) || [];
  // true only on the live pass; the rollup pass blends per-bucket values.
  const exact = !!(body && body.exact);
  const since = (body && body.since) || '';
  // F2 disclosure. Guests are deliberately IN this population -- the host
  // is guest-heavy and a panel blind to its own traffic is the worse
  // failure -- but every anonymous session shares one user_id, so a
  // panel mostly made of one caller has to LOOK that way.
  const guests = (body && body.guests) || 0;
  const beacons = (body && body.beacons) || 0;

  // One row per journey: the names the sink's table admits. A journey
  // with no rows still gets its line, with em dashes throughout, because
  // "we have never measured a sign-in" is not the same as "the sign-in
  // was instant".
  const journeys = [
    { metric: 'dashboard_open', label: 'Dashboard open' },
    { metric: 'inspector_open', label: 'Inspector open' },
    { metric: 'signin', label: 'Sign-in' },
  ];
  const rowFor = (metric, part) =>
    series.find(r => r.metric === metric && r.part === part) || null;

  // The observed metrics are sums, read from each row's own `total`.
  const rowsOf = metric => series.filter(r => r.metric === metric);
  const sumTotal = metric => rowsOf(metric)
    .reduce((s, r) => s + (r.total || 0), 0);
  const countOf = metric => rowsOf(metric)
    .reduce((s, r) => s + (r.n || 0), 0);
  // Breakdown by region and by phase, over whatever groups the data has.
  const groups = (metric, key) => {
    const by = {};
    for (const r of rowsOf(metric)) {
      const k = r[key] || 'other';
      by[k] = (by[k] || 0) + (r.total || 0);
    }
    return Object.entries(by).filter(([, v]) => v > 0)
      .sort((a, b) => b[1] - a[1]);
  };

  const measured = journeys.filter(j => rowFor(j.metric, 'total')).length;
  const shiftTotal = sumTotal('layout_shift');
  const shiftSamples = countOf('layout_shift');
  const blockTotal = sumTotal('longtask');
  const blockSamples = countOf('longtask');

  // The one-sentence readout the accessible name carries, built from the
  // same rows the table below shows so the two cannot drift.
  // "layout instability" and "blocked" both read as per-view metrics, and
  // neither is: each is a SUM over every visit in the range, across every
  // user. A cumulative layout shift of 4.0 is not a CLS of 4.0 — CLS is
  // per page view and capped at 1. The labels say summed, because that is
  // what the number is.
  const headline = `${measured} of 3 journeys measured; layout shift `
    + `${perfScore(shiftTotal)} summed over ${shiftSamples} shifts; main `
    + `thread blocked ${perfMs(blockTotal)} summed over ${blockSamples} `
    + `tasks; ${guests} of ${beacons} beacons from guests.`;
  const description = exact
    ? 'True percentiles, measured live on this view.'
    : 'Approximate: the per-bucket values are blended by sample count, '
      + 'not one value over the whole range.';
  const a11y = window.useChartA11y('Page performance', headline, description);

  // Nothing measured: render nothing. An empty dashboard and an
  // unmeasured one must look different.
  if (!series.length) return null;

  const header = ['journey', 'samples', 'p50 total', 'p75 total',
                  'p50 fetch', 'p75 fetch', 'p50 client', 'p75 client'];
  return (
    <div style={{
      background: 'var(--bg-card)', border: '1px solid var(--border)',
      borderRadius: 4, padding: '10px 14px 14px',
    }}>
      <div style={{
        display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap',
        marginBottom: 8, fontFamily: 'var(--mono)', fontSize: 11,
        color: 'var(--muted)',
      }}>
        <span style={{ color: 'var(--fg)' }}>page performance</span>
        <span>real-user telemetry from this page's own PerformanceObservers</span>
        {!exact && (
          <span style={{
            color: 'var(--warn)', border: '1px solid var(--warn)',
            borderRadius: 3, padding: '0 5px',
          }}>
            approximate — per-bucket values blended by sample count
          </span>
        )}
        {/* The window the answer was READ over, which is not always the
            window asked for: the rollup holds only closed buckets, so a
            range wider than the raw table is answered over less than it
            asked for, and `since` is where that shows. The endpoint sends
            it for exactly this line. */}
        {since && <span>window from {since}</span>}
      </div>

      <div role="img" aria-label={a11y.label} aria-describedby={a11y.descId}
        style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)' }}>
        {headline}
      </div>
      {a11y.descText && <span className="sr-only" id={a11y.descId}>{a11y.descText}</span>}

      <table style={{ width: '100%', borderCollapse: 'collapse', marginTop: 8 }}>
        <thead>
          <tr>{header.map((h, i) => (
            <th key={h} scope="col"
              style={i === 0 ? PERF_HEAD : { ...PERF_HEAD, textAlign: 'right' }}>
              {h}
            </th>
          ))}</tr>
        </thead>
        <tbody>
          {journeys.map(j => {
            const total = rowFor(j.metric, 'total');
            const fetchRow = rowFor(j.metric, 'fetch');
            const clientRow = rowFor(j.metric, 'client');
            return (
              <tr key={j.metric}>
                <th scope="row" style={{ ...PERF_HEAD, color: 'var(--fg)' }}>{j.label}</th>
                <td style={PERF_CELL}>{total ? total.n.toLocaleString() : '—'}</td>
                <td style={PERF_CELL}>{perfPair(total, v => perfMs(v))}</td>
                <td style={PERF_CELL}>{total ? perfMs(total.p75) : '—'}</td>
                <td style={PERF_CELL}>{perfPair(fetchRow, v => perfMs(v))}</td>
                <td style={PERF_CELL}>{fetchRow ? perfMs(fetchRow.p75) : '—'}</td>
                <td style={PERF_CELL}>{perfPair(clientRow, v => perfMs(v))}</td>
                <td style={PERF_CELL}>{clientRow ? perfMs(clientRow.p75) : '—'}</td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <div style={{
        display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))',
        gap: 12, marginTop: 12,
      }}>
        {[
          { name: 'layout shift, summed', metric: 'layout_shift',
            fmt: perfScore,
            total: shiftTotal, samples: shiftSamples, sample: 'shifts observed',
            by: groups('layout_shift', 'region') },
          { name: 'main-thread blocking, summed', metric: 'longtask',
            fmt: perfMs,
            total: blockTotal, samples: blockSamples,
            sample: 'long tasks observed',
            by: groups('longtask', 'phase') },
        ].map(block => (
          <div key={block.metric}>
            <div className="stat">
              <div className="stat-label">{block.name}</div>
              <div className="stat-value">{block.fmt(block.total)}</div>
              <div className="stat-delta">
                {block.samples.toLocaleString()} {block.sample}
              </div>
            </div>
            {block.by.length > 0 && (
              <table style={{ width: '100%', borderCollapse: 'collapse', marginTop: 4 }}>
                <tbody>
                  {block.by.map(([key, value]) => (
                    <tr key={key}>
                      <td style={{ ...PERF_HEAD, fontWeight: 400 }}>{key.replace(/_/g, ' ')}</td>
                      <td style={PERF_CELL}>{block.fmt(value)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

window.WebMetricsPanel = WebMetricsPanel;