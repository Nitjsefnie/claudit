const { useState, useLayoutEffect, useRef } = React;

function ProjectPicker({ projects, active, onChange }) {
  const stripRef = useRef(null);
  const [page, setPage] = useState(0);
  const [ends, setEnds] = useState(null);
  const list = projects || [];
  const pageDigits = String(list.length || 1).length;
  const pageCount = ends ? Math.max(1, ends.length) : (list.length || 1);
  const safePage = Math.min(page, pageCount - 1);
  const end = ends ? Math.abs(ends[safePage]) : list.length;
  const start = ends && safePage ? Math.abs(ends[safePage - 1]) : 0;
  const shown = ends ? list.slice(start, end) : list;
  const pinched = !!(ends && ends[safePage] < 0);
  const activeOffPage = active !== '' && !shown.some(p => p.project_id === active);
  useLayoutEffect(() => {
    const strip = stripRef.current;
    if (!strip || !window.pickerPages) return undefined;
    let live = true;
    const apply = () => {
      if (!live) return;
      const next = window.pickerPages.computePages(strip);
      if (next != null && next.join(',') !== (ends || []).join(',')) {
        setEnds(next);
      }
    };
    apply();
    const ro = new ResizeObserver(apply);
    ro.observe(strip);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(apply);
    return () => { live = false; ro.disconnect(); };
  }, [list, ends, active, page]);
  return (
    <div
      ref={stripRef}
      className="project-picker"
      data-perf-region="project_picker"
      data-picker="projects"
      data-projects-ready={projects != null}
      style={{ overflowX: 'hidden', position: 'relative' }}
    >
      <button
        className={'pp-btn pp-all' + (active === '' ? ' on' : '')}
        onClick={() => onChange('')}
      >All</button>
      {shown.map(p => (
        <button
          key={p.project_id} data-project-id={p.project_id}
          className={'pp-btn pp-proj' + (active === p.project_id ? ' on' : '')}
          style={pinched ? {
            flex: '1 1 auto', minWidth: 0, minHeight: 26, padding: '4px 0',
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
          <span className="pp-count">{String(safePage + 1)
            .padStart(pageDigits, '0')} / {String(pageCount)
            .padStart(pageDigits, '0')}</span>
          <button
            className="pp-btn pp-nav"
            onClick={() => setPage(safePage + 1)}
            disabled={safePage >= pageCount - 1}
            title="Next page"
          >&#8250;</button>
          {active !== '' && (
            <button
              className="pp-btn on pp-jump"
              style={{ visibility: activeOffPage ? 'visible' : 'hidden' }}
              onClick={() => setPage(window.pickerPages.pageForIndex(
                ends, list.findIndex(p => p.project_id === active)
              ))}
              title="Jump to the selected project"
            >{active} &#8617;</button>
          )}
        </span>
      )}
      <window.PickerMeasure list={list} active={active} digits={pageDigits} />
    </div>
  );
}

window.ProjectPicker = ProjectPicker;
