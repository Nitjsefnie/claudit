window.PickerMeasure = function PickerMeasure({ list, active, digits }) {
  const count = '0'.repeat(digits);
  return <div className="pp-measure" aria-hidden="true" style={{
    position: 'absolute', visibility: 'hidden', overflow: 'hidden',
    width: 0, height: 0, display: 'flex', gap: 6,
    pointerEvents: 'none', whiteSpace: 'nowrap',
  }}>
    {list.map(p => <button key={p.project_id} data-project-id={p.project_id}
      className="pp-btn pp-proj" tabIndex={-1}>{p.display_name}</button>)}
    <span className="pp-core">
      <button className="pp-btn pp-nav" tabIndex={-1}>&#8249;</button>
      <span className="pp-core-count">{count} / {count}</span>
      <button className="pp-btn pp-nav" tabIndex={-1}>&#8250;</button>
    </span>
    {active !== '' && <button className="pp-btn on pp-jump"
      tabIndex={-1}>{active} &#8617;</button>}
  </div>;
};
