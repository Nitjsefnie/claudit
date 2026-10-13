const { useState } = React;
const BRAND = window.BRAND || {};

// Range controls shared by the dashboard routes.
function RangePicker({ active, onChange }) {
  const presets = [
    { label: '24h',  value: '1d'   },
    { label: '7d',   value: '7d'   },
    { label: '30d',  value: '30d'  },
    { label: '90d',  value: '90d'  },
    { label: '1y',   value: '365d' },
    { label: 'all',  value: 'all'  },
  ];
  return (
    <div className="project-picker" data-perf-region="range_picker" style={{ borderTop: '1px solid #2a2a4a' }}>
      <span style={{ color: '#9090b0', fontFamily: 'monospace', fontSize: 11, marginRight: 8 }}>range:</span>
      {presets.map(p => (
        <button
          key={p.value}
          className={'pp-btn ' + (active === p.value ? 'on' : '')}
          onClick={() => onChange(p.value)}
        >{p.label}</button>
      ))}
    </div>
  );
}

function ExportButton({ range, project }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const onClick = async () => {
    if (busy) return;
    setBusy(true);
    setErr('');
    const url = `/api/export?range=${encodeURIComponent(range)}` +
      (project ? `&project=${encodeURIComponent(project)}` : '');
    try {
      const res = await fetch(url, { credentials: 'same-origin' });
      if (!res.ok) {
        let msg = `export failed (${res.status})`;
        try { const j = await res.json(); if (j && j.detail) msg = j.detail; } catch (_) {}
        setErr(msg);
        return;
      }
      const blob = await res.blob();
      const objUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = objUrl;
      a.download = `${(BRAND.name || 'export')}_${project || 'all'}_${range}.png`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(objUrl);
    } catch (e) {
      setErr('export failed');
    } finally {
      setBusy(false);
    }
  };
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center' }}>
      <button
        className="pp-btn"
        style={{ marginLeft: 12, opacity: busy ? 0.5 : 1 }}
        onClick={onClick}
        disabled={busy}
        title="Download a PNG of this dashboard for the current filters"
      >{busy ? 'rendering…' : 'Export PNG'}</button>
      {err && <span style={{ color: '#ff6b6b', fontSize: 11, marginLeft: 8 }}>{err}</span>}
    </span>
  );
}

// Navigation intersects startup features with guest/backend permissions.
function TopBar({ route, setRoute, isGuest, backendOn, range, project }) {
  return (
    <header className="topbar" data-perf-region="topbar">
      <div className="topbar-left">
        <div className="logo">
          <span className="logo-mark">{'>'}</span>
          <span className="logo-text">{(BRAND.name || '').toUpperCase()}</span>
          <span className="logo-sub">token usage audit{isGuest ? ' · guest' : ''}</span>
        </div>
      </div>
      <nav className="topnav">
        <button aria-current={route === 'dashboard' ? 'page' : undefined} className={'navbtn ' + (route === 'dashboard' ? 'on' : '')} onClick={() => setRoute('dashboard')}>Overview</button>
        {!isGuest && window.featureOn('sessions') && (
          <button aria-current={route === 'sessions' ? 'page' : undefined} className={'navbtn ' + (route === 'sessions' ? 'on' : '')} onClick={() => setRoute('sessions')}>Sessions</button>
        )}
        {backendOn && window.featureOn('cache') && (
          <button aria-current={route === 'cache' ? 'page' : undefined} className={'navbtn ' + (route === 'cache' ? 'on' : '')} onClick={() => setRoute('cache')}>Cache</button>
        )}
        {!isGuest && window.featureOn('inspector') && (
          <button aria-current={route === 'session' ? 'page' : undefined} className={'navbtn ' + (route === 'session' ? 'on' : '')} onClick={() => setRoute('session')}>Inspector</button>
        )}
      </nav>
      <div className="topbar-right">
        {backendOn && !isGuest && window.featureOn('export_png') && (
          <ExportButton range={range} project={project} />
        )}
        <a className="loadbtn logout-btn" href="/logout">Logout</a>
      </div>
    </header>
  );
}


window.ExportButton = ExportButton;
window.TopBar = TopBar;
window.RangePicker = RangePicker;

window.inspectorEmptyGuidance = () => window.featureOn('sessions')
  ? 'Pick a session from the Sessions list to inspect its timeline, tool calls, and context growth.'
  : 'No transcript is currently open.';
