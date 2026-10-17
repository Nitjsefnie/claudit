// The Overview's status block — everything the summary shows when it is
// not showing numbers.
//   kind 'loading' → the placeholder, while the request is in flight
//   kind 'empty'   → "no usage data in range", the wording every other
//                    panel already uses for a range with no rows
//   kind 'error'   → the status plus the detail naming it
//   kind 'data'    → not rendered UNLESS the request failed over data an
//                    earlier response left on screen: those numbers are
//                    stale, and saying so beats showing them as current
// Stat is app.jsx's and arrives as a prop, so neither module has to load
// before the other.
//
function OverviewStatus({ summary, activeRange, Stat }) {
  if (summary.kind !== 'data') {
    return (
      <div className="dash-summary" data-perf-region="summary_strip">
        <Stat label="status" value={summary.text} warn={summary.kind === 'error'} />
        <Stat label="range" value={activeRange} />
        {summary.detail && <Stat label="detail" value={summary.detail} />}
      </div>
    );
  }
  if (!summary.error) return null;
  return (
    <div className="dash-summary" data-perf-region="summary_strip">
      <Stat label="status" value="error" warn />
      <Stat label="detail" value={summary.detail} />
    </div>
  );
}

window.OverviewStatus = OverviewStatus;
