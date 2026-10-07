// The dashboard's project picker strip: fit-computed pagination (#774).
//
// The maintainer ruling: show only the chips that fit the strip's current
// width and page the rest through prev/next — no horizontal scrolling or
// scrollbar remains, and the strip's height and position never move while
// the page changes, the window resizes, or the user pages. The page size
// is therefore measured, not fixed. The arithmetic and the strip read live
// in src/picker-fit.js (plain JS, node-tested); this file owns the React
// glue. The rendered half of the acceptance — overflow/scrollbar and
// no-shift-while-paging-or-refitting, each proven red by a seeded case —
// lives in scripts/ci/panel_layout_rules.mjs.
//
// It was extracted from app.jsx (issue #774): the file sat at its size
// baseline and entries never rise, so the strip's new brain moved out
// beside the other extracted panels.
const { useState, useLayoutEffect, useRef } = React;

function ProjectPicker({ projects, active, onChange }) {
  const stripRef = useRef(null);
  const [page, setPage] = useState(0);
  const [perPage, setPerPage] = useState(null); // null = unfitted first render
  const list = projects || [];
  // The applied page size floors the measured fit at one chip (#807): a
  // strip too narrow for any full chip still pages one project at a
  // time — perPage 0 showed empty pages and made the jump's index / 0
  // produce NaN. The floor lives HERE, not in computeFit, so the
  // measured fit stays truthful (0 = room for none), and the pinched
  // chip's shrink-to-fit style below is what keeps the forced page inside
  // the strip: plain chips keep flex-shrink: 0, which is also the shape the
  // rendered guard's red proof needs to overflow.
  const size = perPage == null ? null : Math.max(1, perPage);
  const pinched = perPage === 0;
  const pageCount = size != null
    ? Math.max(1, Math.ceil(list.length / size))
    : (list.length || 1);
  // Clamp rather than store a corrected page: a stale index would strand
  // the user on a blank page with no chips to click their way out of.
  const safePage = Math.min(page, pageCount - 1);
  const shown = perPage == null
    ? list
    : list.slice(safePage * size, safePage * size + size);
  // The active chip may live on another page. Nothing renders as `on` then
  // — including "All" — so surface the selection instead of leaving the
  // filtered dashboard looking unfiltered.
  const activeOffPage = active !== '' && !shown.some(p => p.project_id === active);
  // One fit pass = measure the strip and its hidden full row, then apply.
  // It runs in useLayoutEffect so every pass commits before the browser
  // paints — the full row never paints and nothing below the strip moves.
  // It re-runs per applied page size (the verify pass) and on the refit
  // triggers; a stable number sets no state, so the loop settles.
  useLayoutEffect(() => {
    const strip = stripRef.current;
    if (!strip || !window.pickerFit) return undefined;
    let live = true;
    const apply = () => {
      if (!live) return;
      const next = window.pickerFit.computeFit(strip);
      if (next != null && next !== perPage) setPerPage(next);
    };
    apply();
    const ro = new ResizeObserver(apply);
    ro.observe(strip);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(apply);
    return () => { live = false; ro.disconnect(); };
  }, [list, perPage, active]);
  return (
    <div
      ref={stripRef}
      className="project-picker"
      data-perf-region="project_picker"
      data-picker="projects"
      style={{ overflowX: 'hidden', position: 'relative' }}
    >
      <button
        className={'pp-btn pp-all' + (active === '' ? ' on' : '')}
        onClick={() => onChange('')}
      >All</button>
      {shown.map(p => (
        <button
          key={p.project_id}
          className={'pp-btn pp-proj' + (active === p.project_id ? ' on' : '')}
          style={pinched ? {
            flex: '0 1 auto', minWidth: 0,
            overflow: 'hidden', textOverflow: 'ellipsis',
          } : undefined}
          onClick={() => onChange(p.project_id)}
          title={`${p.session_count} sessions · $${p.total_cost.toFixed(2)}`}
        >{p.display_name}</button>
      ))}
      {pageCount > 1 && (
        <span className="pp-pager">
          <button
            className="pp-btn pp-nav"
            onClick={() => setPage(safePage - 1)}
            disabled={safePage === 0}
            title="Previous page"
          >&#8249;</button>
          <span className="pp-count">{safePage + 1} / {pageCount}</span>
          <button
            className="pp-btn pp-nav"
            onClick={() => setPage(safePage + 1)}
            disabled={safePage >= pageCount - 1}
            title="Next page"
          >&#8250;</button>
          {activeOffPage && (
            <button
              className="pp-btn on pp-jump"
              onClick={() => setPage(Math.floor(
                list.findIndex(p => p.project_id === active) / size
              ))}
              title="Jump to the selected project"
            >{active} &#8617;</button>
          )}
        </span>
      )}
      {/* The hidden measure row: every chip plus the jump replica, so the
          fit reads widths the real strip no longer renders and reserves
          the jump whether or not the current page shows it. Zero-sized,
          clipped, and aria-hidden — no layout, no tab stops. */}
      <div
        className="pp-measure"
        aria-hidden="true"
        style={{
          position: 'absolute', visibility: 'hidden', overflow: 'hidden',
          width: 0, height: 0, display: 'flex', gap: 6,
          pointerEvents: 'none', whiteSpace: 'nowrap',
        }}
      >
        {list.map(p => (
          <button key={p.project_id} className="pp-btn pp-proj" tabIndex={-1}>
            {p.display_name}
          </button>
        ))}
        {active !== '' && (
          <button className="pp-btn on pp-jump" tabIndex={-1}>{active} &#8617;</button>
        )}
      </div>
    </div>
  );
}

window.ProjectPicker = ProjectPicker;
