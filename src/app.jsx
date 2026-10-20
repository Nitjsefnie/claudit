const { useState, useEffect, useMemo, useRef, useId } = React;

// One effect run owns one request so a slow stale response cannot
// overwrite a fresher one. mintRunSignal pairs an AbortController with
// an isCurrent() check: every handler checks the run before applying a
// result. Abort is cancellation, not failure, and the effect's cleanup
// aborts the superseded request when the dependencies change.
function mintRunSignal() {
  const ctrl = new AbortController();
  return {
    signal: ctrl.signal,
    isCurrent: () => !ctrl.signal.aborted,
    abort: () => ctrl.abort(),
  };
}

async function fetchTranscriptText(sessionId, fetchImpl = fetch) {
  const r = await fetchImpl(`/api/sessions/${sessionId}/transcript`,
                           { credentials: 'same-origin' });
  if (!r.ok) {
    let message = 'HTTP ' + r.status;
    try {
      const body = await r.json();
      if (body && typeof body.detail === 'string') message += ': ' + body.detail;
    } catch (_) {}
    throw new Error(message);
  }
  return r.text();
}

function makeTranscriptLoader({ fetchText, parse, onStart, onSuccess, onError }) {
  let latestRequest = 0;
  return async function load(sessionId) {
    const request = ++latestRequest;
    onStart();

    let text;
    try {
      text = await fetchText(sessionId);
    } catch (err) {
      if (request !== latestRequest) return;
      onError('fetch', err);
      return;
    }
    if (request !== latestRequest) return;

    let tx;
    try {
      tx = parse(text);
    } catch (err) {
      if (request !== latestRequest) return;
      onError('parse', err);
      return;
    }
    if (request === latestRequest) onSuccess(tx);
  };
}

function App() {
  const [storedRoute, setRoute] = useState('dashboard');
  const [tx, setTx] = useState(null);
  const [transcriptError, setTranscriptError] = useState('');
  const [, setFilename] = useState('');
  const [synth, setSynth] = useState(null);
  const [useSynth, setUseSynth] = useState(true);
  const [backendDash, setBackendDash] = useState(null);
  const [dashFetch, setDashFetch] = useState(() => window.dashboardFeaturesOn()
    ? window.dashboardFetch.start() : window.dashboardFetch.loaded());
  const [projects, setProjects] = useState(null);
  const [activeProject, setActiveProject] = useState('');
  const [activeRange, setActiveRange] = useState('all');
  const [models, setModels] = useState(null);
  // Server injects window.IS_GUEST into index.html so the very first
  // render already hides guest-restricted UI — no flash of the
  // Sessions/Inspector tabs before /api/me resolves.
  const [isGuest, setIsGuest] = useState(!!window.IS_GUEST);

  const backendOn = !!(window.BACKEND_URL && window.BACKEND_URL.length > 0);
  const route = window.featureRoute(storedRoute, isGuest, backendOn);
  useEffect(() => { if (route !== storedRoute) setRoute(route); }, [route, storedRoute]);
  // Bumped by the SSE listener on `ingest_done`; declared up here because
  // the projects fetch below lists it as a dependency.
  const [dashNonce, setDashNonce] = useState(0);
  // Live region: refreshMsg is what the polite region
  // announces; refreshRef holds the pending what-changed line the SSE
  // handler stashed, consumed by the dashboard refetch that completes
  // it. null = nothing pending.
  const [refreshMsg, setRefreshMsg] = useState('');
  const refreshRef = useRef(null);
  const transcriptSessionIdRef = useRef('');
  const transcriptLoaderRef = useRef(null);
  if (!transcriptLoaderRef.current) {
    transcriptLoaderRef.current = makeTranscriptLoader({
      fetchText: fetchTranscriptText,
      parse: text => {
        window.perf.closeFetch('inspector_open');
        const { events, meta } = window.parseTranscript(text);
        const stats = window.computeSessionStats(events, meta);
        return { events, meta, stats };
      },
      onStart: () => {
        setTx(null);
        setTranscriptError('');
      },
      onSuccess: tx => {
        setTx(tx);
        setTranscriptError('');
        setFilename(transcriptSessionIdRef.current);
        setUseSynth(false);
        setRoute('session');
      },
      onError: (kind, err) => {
        const message = err && err.message ? err.message : String(err);
        if (kind === 'parse') {
          console.error('transcript parse failed', err);
          setTranscriptError(`transcript parse failed: ${message}`);
        } else {
          console.error('transcript fetch failed', err);
          setTranscriptError(`transcript fetch failed: ${message}`);
        }
        setRoute('session');
      },
    });
  }

  // Generate synthetic data only for the no-backend demo, so a configured
  // backend never paints fabricated figures while its request is in flight.
  useEffect(() => {
    if (backendOn || !window.dashboardFeaturesOn()) return;
    setSynth(window.generateSyntheticData());
  }, [backendOn]);

  useEffect(() => {
    if (!backendOn) return;
    fetch('/api/me', { credentials: 'same-origin' })
      .then(r => r.json())
      .then(b => setIsGuest(!!b.is_guest))
      .catch(() => {});
  }, [backendOn]);

  // Fetch project list whenever backend mode / guest status / the active
  // range change — the picker's ordering (and which projects even show up)
  // is range-scoped server-side, so a range change must refetch it.
  // Skipped for guests — the endpoint is server-side blocked anyway,
  // and the picker isn't rendered in guest mode.
  // Also re-runs on the SSE ingest nonce: a new project appearing in an
  // ingest otherwise stays missing from the picker until a range change
  // or a full reload.
  useEffect(() => {
    if (!backendOn || isGuest || !window.featureOn('project_picker')) return;
    // Each run owns its request through mintRunSignal, the
    // same guard the dashboard refetch uses: a superseded project list
    // must not overwrite a fresher one.
    const run = mintRunSignal();
    fetch(`/api/projects?range=${encodeURIComponent(activeRange)}`, { credentials: 'same-origin', signal: run.signal })
      .then(r => r.json())
      .then(b => {
        if (!run.isCurrent()) return;
        setProjects(b.projects || []);
      })
      .catch(err => {
        // A superseded run's abort is not a failure: never a log.
        if (!run.isCurrent()) return;
        console.error('projects fetch failed', err);
      });
    return run.abort;
  }, [backendOn, isGuest, activeRange, dashNonce]);

  useEffect(() => {
    if (!backendOn) return;
    if (!window.modelFeaturesOn()) return;
    fetch('/api/models', { credentials: 'same-origin' })
      .then(r => r.json())
      .then(b => setModels(b.models || []))
      .catch(err => console.error('models fetch failed', err));
  }, [backendOn]);

  // Fetch dashboard whenever the active project / range / nonce change.
  // `dashNonce` is a counter bumped by the SSE listener below to trigger
  // a re-fetch without changing project/range. Each run owns its request
  // through mintRunSignal, so a slow stale response can no
  // longer land after a fresher one and overwrite it. An aborted run is
  // not a failure — it applies nothing and leaves the pending
  // announcement line for the run that replaced it; a real failure
  // still drops that line.
  // A superseded run writes nothing: neither its data nor its state.
  useEffect(() => {
    if (!backendOn) return;
    if (!window.dashboardFeaturesOn()) return;
    const run = mintRunSignal();
    const q = activeProject ? `&project=${encodeURIComponent(activeProject)}` : '';
    setDashFetch(window.dashboardFetch.start());
    window.perf.openJourney('dashboard_open');
    window.dashboardFetch.load(`/api/dashboard?range=${activeRange}${q}`, { signal: run.signal })
      .then(b => {
        if (!run.isCurrent()) return;
        window.perf.closeFetch('dashboard_open');
        setBackendDash(b);
        setDashFetch(window.dashboardFetch.loaded());
        // Announce only once the refetch the event asked for has
        // landed: the live region must never describe a load that is
        // still in flight.
        if (refreshRef.current !== null) {
          const what = refreshRef.current;
          refreshRef.current = null;
          setRefreshMsg(`Data refreshed${what ? ` — ${what}` : ''}`);
        }
      })
      .catch(err => {
        // A superseded run's abort is not a failure: the pending line
        // now belongs to the run that replaced this one.
        if (!run.isCurrent()) return;
        console.error('dashboard fetch failed', err);
        // The refetch did not land, so the pending what-changed line
        // must not survive to mislabel the NEXT successful
        // announcement -- drop it and wait for the next event.
        refreshRef.current = null;
        setDashFetch(window.dashboardFetch.failed(err && err.message));
      });
    return run.abort;
  }, [backendOn, activeProject, activeRange, dashNonce]);

  // Live updates: open an SSE stream and bump dashNonce on `ingest_done`.
  // No page reload — only the data refetches. The event's run summary
  // feeds the live region's what-changed line; the announcement itself
  // is set when the refetch completes (the dashboard fetch above).
  useEffect(() => {
    if (!backendOn) return;
    if (!window.featureOn('live_updates')) return;
    const es = new EventSource('/api/events', { withCredentials: true });
    const onIngest = e => {
      window.perf.sseUpdate();
      refreshRef.current = window.ingestChangeSummary(e);
      setDashNonce(n => n + 1);
    };
    es.addEventListener('ingest_done', onIngest);
    es.onerror = () => { /* EventSource auto-reconnects with backoff */ };
    return () => { es.removeEventListener('ingest_done', onIngest); es.close(); };
  }, [backendOn]);

  const liveData = useMemo(() => tx ? window.txToDashData(tx) : null, [tx]);
  // Keep shape identity stable across shell and SSE state changes.
  const dashData = useMemo(
    () => (backendDash
      ? window.backendDashToShape(backendDash)
      : ((!useSynth && liveData) ? liveData : synth)),
    [backendDash, useSynth, liveData, synth],
  );

  function loadFromBackend(sessionId) {
    if (!window.featureOn('inspector') || isGuest) return;
    transcriptSessionIdRef.current = sessionId;
    window.perf.openJourney('inspector_open');
    return transcriptLoaderRef.current(sessionId);
  }

  return (
    <div className="app-root">
      <window.TopBar route={route} setRoute={setRoute} isGuest={isGuest} backendOn={backendOn} range={activeRange} project={activeProject} />
      {/* The page's one main landmark: the pickers and every route's
          content sit inside it; the topbar and the live region stay
          outside. */}
      <main id="main">
        {backendOn && !isGuest && window.featureOn('project_picker') && (
          <window.ProjectPicker
            projects={projects}
            active={activeProject}
            onChange={setActiveProject}
          />
        )}
        {backendOn && (
          <window.RangePicker active={activeRange} onChange={setActiveRange} />
        )}
        {/* Mounted as soon as the backend is known: Dashboard's four
            self-fetching panels must go out in parallel, not after. */}
        {route === 'dashboard' && <window.Dashboard Stat={Stat} synth={dashData} models={models} backendOn={backendOn} activeProject={activeProject} activeRange={activeRange} dashNonce={dashNonce} dashFetch={dashFetch} />}
        {route === 'sessions' && dashData && (
          <window.SessionsList
            synth={dashData}
            onOpen={(sid) => backendOn ? loadFromBackend(sid) : setRoute('session')}
          />
        )}
        {route === 'cache' && backendOn && (
          <div>
            <window.CacheView project={activeProject} range={activeRange} />
            <window.ContextGrowthAgg project={activeProject} range={activeRange} />
          </div>
        )}
        {route === 'session' && (transcriptError
          ? <><div className="page-head"><h1>Inspector</h1></div><div className="err">{transcriptError}</div></>
          : <SessionView tx={tx} />)}
      </main>
      {/* Live region for the SSE ingest_done refresh.
          Mounted here, outside the panel tree, so no re-render can
          unmount it; polite + atomic so it never interrupts or steals
          focus; text set only after the refetch it announces lands. */}
      {window.featureOn('live_updates') && <div aria-live="polite" aria-atomic="true" className="sr-only">{refreshMsg}</div>}
    </div>
  );
}

function Stat({ label, value, highlight, warn }) {
  return (
    <div className={'stat ' + (highlight ? 'stat-hl ' : '') + (warn ? 'stat-warn ' : '')}>
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={{ lineHeight: 1.3 }}>{value}</div>
    </div>
  );
}

function SessionView({ tx }) {
  const [selected, setSelected] = useState(0);
  const [filter, setFilter] = useState({ user: true, asst: true, think: true, tool: true, result: true });
  const [search, setSearch] = useState('');
  const [dense, setDense] = useState(false);
  const [view, setView] = useState('timeline');
  const viewId = useId();
  // the Inspector journey ends when the transcript is on
  // screen. Keyed on the transcript itself — a new object per load — so
  // a re-render cannot re-close a closed journey. Above the `if (!tx)`
  // return below, as hooks must be.
  const renderedTx = useRef(null);
  useEffect(() => {
    if (!tx || renderedTx.current === tx) return;
    renderedTx.current = tx;
    window.perf.closeJourney('inspector_open');
  }, [tx]);

  if (!tx) {
    return (
      <div className="session-empty">
        <div className="drop-card">
          <div className="page-head"><h1>Inspector</h1></div>
          <div className="drop-title">No session selected</div>
          <div className="drop-sub">{window.inspectorEmptyGuidance()}</div>
        </div>
      </div>
    );
  }

  const visible = tx.events.filter(e => {
    if (e.type === 'rate_limit' && !window.featureOn('rate_limits')) return false;
    if (e.type === 'user_message' && !filter.user) return false;
    if (e.type === 'assistant_text' && !filter.asst) return false;
    if (e.type === 'thinking' && !filter.think) return false;
    if ((e.type === 'tool_call' || e.type === 'agent_spawn') && !filter.tool) return false;
    if (e.type === 'tool_result' && !filter.result) return false;
    if (search) {
      const hay = (e.detail || '') + ' ' + (e.tool_name || '') + ' ' + JSON.stringify(e.tool_input || '');
      if (!hay.toLowerCase().includes(search.toLowerCase())) return false;
    }
    return true;
  });

  // The transcript listbox's one clamped index: the detail
  // row, the selected row, the arrow moves and the active option ALL
  // derive from it, so after a search or filter shrinks the timeline
  // under the stored index they still agree — on the last visible row —
  // and ArrowUp moves from THAT row, as a click would.
  const rowId = (idx) => `${viewId}-trow-${idx}`;
  const activeIdx = window.activeTimelineIndex(selected, visible.length);
  const sel = visible[activeIdx] || null;
  // With focus parked on the container (aria-activedescendant), the
  // browser does not scroll the active option into view; do it here so
  // an arrow move past the viewport stays visible.
  const revealRow = (idx) => {
    const el = document.getElementById(rowId(idx));
    if (el) el.scrollIntoView({ block: 'nearest' });
  };
  const onTimelineKeyDown = (ev) => {
    if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      const next = window.moveTimelineIndex(activeIdx, visible.length, 1);
      setSelected(next);
      revealRow(next);
    } else if (ev.key === 'ArrowUp') {
      ev.preventDefault();
      const next = window.moveTimelineIndex(activeIdx, visible.length, -1);
      setSelected(next);
      revealRow(next);
    } else if (ev.key === 'Enter' || ev.key === ' ') {
      ev.preventDefault();
      setSelected(activeIdx);
    }
  };

  return (
    <div className="session-view" data-perf-region="inspector">
      <div className="page-head"><h1>Inspector</h1></div>
      <SessionHeader stats={tx.stats} />
      <div style={{
        display: 'flex', gap: 6, alignItems: 'center',
        padding: '8px 14px', borderBottom: '1px solid var(--border)',
        background: 'var(--bg-soft)',
      }}>
        {[['timeline', 'Timeline'], ['ctx', 'Context growth']].map(([k, lab]) => (
          <button key={k} className={'fchip ' + (view === k ? 'on' : '')}
            onClick={() => setView(k)}>{lab}</button>
        ))}
      </div>
      {view === 'ctx' && <window.ContextGrowthView tx={tx} />}
      {view === 'timeline' && (
      <div className="session-body">
        <aside className="session-side">
          <div className="filterbar">
            <input className="search" placeholder="search transcript…" value={search} onChange={e => setSearch(e.target.value)} />
            <div className="filter-chips">
              {[
                ['user','User'],['asst','Asst'],['think','Think'],['tool','Tools'],['result','Results']
              ].map(([k, lab]) => (
                <button key={k} className={'fchip ' + (filter[k] ? 'on' : '')}
                  onClick={() => setFilter(f => ({ ...f, [k]: !f[k] }))}>{lab}</button>
              ))}
              <span className="spacer"></span>
              <button className={'fchip ' + (dense ? 'on' : '')} onClick={() => setDense(d => !d)}>dense</button>
            </div>
          </div>
          <div className="timeline"
            role="listbox"
            aria-label="Transcript timeline"
            tabIndex={0}
            aria-activedescendant={activeIdx >= 0 ? rowId(activeIdx) : undefined}
            onKeyDown={onTimelineKeyDown}>
            {visible.map((e, idx) => (
              <TimelineRow key={e.line + ':' + idx} rowId={rowId(idx)} e={e} dense={dense}
                selected={idx === activeIdx} onClick={() => setSelected(idx)} />
            ))}
          </div>
        </aside>
        <section className="session-detail" aria-label="Event detail">
          <window.EventDetail event={sel} dense={dense} />
        </section>
      </div>
      )}
    </div>
  );
}

function SessionHeader({ stats }) {
  const dur = stats.lastTs && stats.firstTs ? (stats.lastTs - stats.firstTs)/60000 : 0;
  return (
    <div className="session-header">
      <Stat label="turns"      value={stats.turns} />
      <Stat label="user msgs"  value={stats.userMsgs} />
      <Stat label="tool calls" value={stats.toolCalls} />
      <Stat label="errors"     value={stats.errorResults} warn={stats.errorResults > 0} />
      <Stat label="parallel batches" value={stats.parallelBatches} />
      <Stat label="duration"   value={dur < 60 ? dur.toFixed(0)+'m' : (dur/60).toFixed(1)+'h'} />
      <Stat label="output tokens" value={window.humanFmt(stats.output)} />
      <Stat label="cache hit %" value={stats.hitRate.toFixed(1) + '%'} />
      <Stat label="est. cost"  value={window.humanCurrency(stats.cost)} highlight />
    </div>
  );
}

function TimelineRow({ e, dense, selected, onClick, rowId }) {
  const meta = window.TYPE_META[e.type] || { label: e.type, color: 'var(--fg)', glyph: '·' };
  const oneLine = window.eventOneLine(e).slice(0, 220);
  const toolColor = e.tool_name ? window.TOOL_COLORS[e.tool_name] : null;
  return (
    <div id={rowId}
      className={'trow ' + (selected ? 'sel ' : '') + (dense ? 'dense ' : '') + (e.is_error ? 'err ' : '')}
      role="option"
      aria-selected={selected}
      onClick={onClick}>
      <div className="trow-time mono">{window.shortTime(e.ts)}</div>
      <div className="trow-tag mono" style={{ color: meta.color, borderColor: meta.color + '40' }}>
        {meta.label}
      </div>
      <div className="trow-glyph mono" style={{ color: toolColor || meta.color }}>
        {e.tool_name ? window.toolGlyph(e.tool_name) : meta.glyph}
      </div>
      <div className="trow-body">
        <span className="trow-one">{oneLine}</span>
        {e.batch_size > 1 && <span className="trow-batch">⫶{e.batch_index}/{e.batch_size}</span>}
        {e.is_error && <span className="trow-err">ERR</span>}
      </div>
    </div>
  );
}

window.App = App;
