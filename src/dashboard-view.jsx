const { useState, useMemo, useEffect } = React;

function TokenBreakdownPanel({ events }) {
  const [activeModel, setActiveModel] = useState('');

  const modelOpts = useMemo(() => {
    const byModel = {};
    for (const e of events) {
      if (!e.model || e.model === '<synthetic>' || e.model === 'synthetic') continue;
      byModel[e.model] = (byModel[e.model] || 0) + (e.cost_usd || 0);
    }
    return Object.entries(byModel).sort((a, b) => b[1] - a[1]).map(([k]) => k);
  }, [events]);

  const filtered = useMemo(
    () => (activeModel ? events.filter(e => e.model === activeModel) : events),
    [events, activeModel]);
  const { rows, costRows, tokenTotal, costTotal } = useMemo(
    () => window.computeTokenBreakdown(filtered), [filtered]);
  // A free lane (bonsai-2-27b at $0) gives every row cost 0, and
  // computeTokenBreakdown floors costTotal at 1 to keep the division
  // finite — so the bars would all read "$0 (0.0%)" rather than say
  // nothing. Drop the bar instead; the token bar above is unaffected.
  const hasCost = costRows.some(r => r.cost > 0);

  return (
    <div style={{
      background: 'var(--bg-card)', border: '1px solid var(--border)',
      borderRadius: 4, display: 'flex', flexDirection: 'column',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'flex-end',
        flexWrap: 'wrap', gap: 8, padding: '8px 14px 0',
        fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)',
      }}>
        <span>model:</span>
        <select
          aria-label="Token Breakdown model filter"
          value={activeModel}
          onChange={e => setActiveModel(e.target.value)}
          style={{
            background: 'var(--panel-2)', color: 'var(--fg)',
            border: '1px solid var(--border)', borderRadius: 4,
            padding: '3px 6px', fontFamily: 'var(--mono)', fontSize: 11,
            cursor: 'pointer',
          }}>
          <option value="">All</option>
          {modelOpts.map(m => <option key={m} value={m}>{m}</option>)}
        </select>
      </div>
      <window.HBar embedded listPanel
        title="Token Breakdown — by tokens"
        rows={[...rows].sort((a, b) => b.value - a.value)}
        fmt={r => `${window.humanFmt(r.value)} (${(r.value / tokenTotal * 100).toFixed(1)}%)`} />
      {hasCost && (
      <window.HBar embedded listPanel
        title="Token Breakdown — by cost"
        rows={[...costRows].map(r => ({ ...r, value: r.cost })).sort((a, b) => b.value - a.value)}
        fmt={r => `${window.humanCurrency(r.value)} (${(r.value / costTotal * 100).toFixed(1)}%)`} />
      )}
    </div>
  );
}

function Dashboard({ synth, models, backendOn, activeProject, activeRange, dashNonce, dashFetch, Stat }) {
  // Render while `synth` is null so self-fetching panels can request
  // their endpoints in parallel with /api/dashboard.
  const hasData = !!synth;
  // The fetch state, not the data: an empty or failed range has still
  // resolved, and the sections below show their own states then.
  const dashLoading = dashFetch.status === window.dashboardFetch.LOADING;
  const independentReady = hasData || !dashLoading;
  // The invisible reservation below is for the FIRST load only: a
  // later range change re-enters LOADING while the previous range's
  // sections are on screen, and hiding those would blank the page on
  // every filter switch.
  const everLoaded = React.useRef(false);
  useEffect(() => { if (!dashLoading) everLoaded.current = true; }, [dashLoading]);
  // the page is USABLE once the request RESOLVES, an empty range
  // included -- a deploy with no data would otherwise never leave pre-paint.
  // The JOURNEY still closes only on a data-bearing READY: an empty one, or a
  // failed one over stale `synth`, would report a page that rendered nothing.
  useEffect(() => {
    if (dashFetch.status === window.dashboardFetch.LOADING) return;
    window.perf.markUsable();
    if (hasData && dashFetch.status === window.dashboardFetch.READY) {
      window.perf.closeJourney('dashboard_open');
    }
  }, [hasData, dashFetch]);
  const {
    events = [], limitHits: rawLimitHits = [], range: dataRange, costByModel: backendByModel,
    tokensByModel: backendTokensByModel,
    costByProject: backendByProject = [],
    tokensByProject: backendTokensByProject = [],
    sessionsOverride, totalSessions, mainWUsage, mainEmpty, subagentFiles,
    subagentOnlySessions, totalPrompts, totalTurns, responseSizes,
    bucketS, tokenTypes, hideRateLimits: payloadHideRateLimits = false,
  } = synth || {};
  const hideRateLimits = payloadHideRateLimits || !window.featureOn('rate_limits');
  const limitHits = hideRateLimits ? [] : rawLimitHits;
  // Placeholder window so the bin-size maths below stays finite pre-data.
  const range = dataRange || { start: Date.now() - 86400000, end: Date.now() };
  const hasBackendByModel = backendByModel && Object.keys(backendByModel).length > 0;
  const hasBackendTokensByModel = backendTokensByModel && Object.keys(backendTokensByModel).length > 0;
  const computed = useMemo(() => window.computeSessions(events), [events]);
  const sessions = (sessionsOverride && sessionsOverride.length)
    ? sessionsOverride
    : computed.sessions;
  const windowBoundaries = computed.windowBoundaries;
  // Empty-plot suppression: a series that is zero in every bucket gets
  // no grid cell. Backend-declared for token types, summed here for the
  // derived and churn series.
  const panels = useMemo(
    () => window.tokenPanels(events, tokenTypes), [events, tokenTypes]);

  const totals = useMemo(() => {
    const t = { input: 0, output: 0, cc: 0, cr: 0, cost: 0, eph5: 0, eph1h: 0,
                linesAdded: 0, linesDeleted: 0 };
    const byModel = {};
    const tokensByModel = {};
    for (const e of events) {
      t.input += e.input_tokens; t.output += e.output_tokens;
      t.cc += e.cache_create; t.cr += e.cache_read;
      t.cost += e.cost_usd;
      t.eph5 += e.ephemeral_5m; t.eph1h += e.ephemeral_1h;
      // Churn is attached to the first model row of each bucket only
      // (see api_dashboard._attach_churn), so a plain sum over events
      // is already de-duplicated across models.
      t.linesAdded += e.lines_added || 0; t.linesDeleted += e.lines_deleted || 0;
      const label = window.modelProviderLabel(e.model, e.provider);
      byModel[label] = (byModel[label] || 0) + e.cost_usd;
      tokensByModel[label] = (tokensByModel[label] || 0) + e.input_tokens + e.output_tokens + e.cache_create + e.cache_read;
    }
    t.total = t.input + t.output + t.cc + t.cr;
    return {
      ...t,
      byModel: hasBackendByModel ? backendByModel : byModel,
      tokensByModel: hasBackendTokensByModel ? backendTokensByModel : tokensByModel,
    };
  }, [events, backendByModel, hasBackendByModel, backendTokensByModel, hasBackendTokensByModel]);

  // Adaptive to the visible data, but never finer than the aggregation the
  // backend already applied to each returned point.
  const binMs = window.dashboardBinMs(range, bucketS);

  const costByModel = window.modelGroup.group(totals.byModel);
  // Share of the charted total, not of `totals.cost`: the rows above drop
  // zero-cost models, so summing them is what makes the labels add to 100%.
  const costByModelTotal = costByModel.reduce((a, r) => a + r.value, 0);
  const tokensByModel = window.modelGroup.group(totals.tokensByModel);
  const tokensByModelTotal = tokensByModel.reduce((a, r) => a + r.value, 0);

  // One colour for every bar: this is a magnitude comparison, identity is
  // carried by the row label, so NO fixedColors and the same palette
  // colour (the Cost (USD) gold) on each row. The backend already sorted
  // desc, dropped zero-cost rows, and folded the tail into "Other (N
  // projects)". Guests get no cost_by_project key at all (server-side),
  // so the empty list hides the panel for them without an isGuest check.
  const costByProject = backendByProject
    .map(r => ({ label: r.project, value: r.cost_usd, color: window.dashboardCol.costUSD }));
  // Tokens by Project: the Cost by Project treatment on the tokens
  // measure — a VBar whose fmt is value-ONLY, because VBar prints each
  // bar's share itself and a fmt that appends one renders it twice
  // ("35B (18.0%)" above "(18.0%)"). It is NOT gated on cost (a free
  // lane priced $0 is invisible on Cost by Project and visible here),
  // only on having tokens; guests get no tokens_by_project key at all
  // (server-side, like cost_by_project), so the empty list hides the
  // panel for them without an isGuest check.
  const tokensByProject = backendTokensByProject
    .map(r => ({ label: r.project, value: r.total_tokens, color: window.dashboardCol.totalTokens }));

  const totalCostStr = window.humanFmt(totals.cost, true);
  const hasCost = totals.cost > 0;
  const sharedCostEnabled = ['usage_timeseries', 'model_comparison', 'summary'].some(window.featureOn);

  const summary = window.dashboardFetch.summary(dashFetch, hasData);
  return (
    <div className="dashboard" data-perf-region="panel_grid">
      <div className="page-head"><h1>Overview</h1></div>
      {window.featureOn('summary')
        ? <window.OverviewStatus summary={summary} activeRange={activeRange} Stat={Stat} />
        : summary.kind !== 'data' && <div role="status">{summary.text}</div>}
      {window.featureOn('summary') && summary.kind === 'data' && (
      <div className="dash-summary" data-perf-region="summary_strip">
        <Stat label="window" value={<span style={{ whiteSpace: 'normal' }}>{`${window.fmtDate(range.start, {day:true})} – ${window.fmtDate(range.end, {day:true})}`}</span>} />
        <Stat label="main sessions with usage" value={window.humanFmt(mainWUsage != null ? mainWUsage : (totalSessions != null ? totalSessions : (events.reduce((s, e) => s + (e.session_count || 0), 0) || sessions.length)))} />
        {mainEmpty != null && <Stat label="main empty sessions" value={window.humanFmt(mainEmpty)} />}
        {subagentFiles != null && <Stat label="subagent sessions" value={window.humanFmt(subagentFiles)} />}
        {subagentOnlySessions != null && <Stat label="subagent-only sessions" value={window.humanFmt(subagentOnlySessions)} />}
        {(mainWUsage != null || mainEmpty != null || subagentFiles != null) &&
          <Stat label="total" value={window.humanFmt((mainWUsage || 0) + (mainEmpty || 0) + (subagentFiles || 0))} />}
        {totalPrompts != null && <Stat label="prompts" value={window.humanFmt(totalPrompts)} />}
        {totalTurns != null && <Stat label="turns" value={window.humanFmt(totalTurns)} />}
        <Stat label="requests" value={window.humanFmt(events.reduce((s, e) => s + (e.requests == null ? 1 : e.requests), 0))} />
        <Stat label="total tokens" value={window.humanFmt(totals.total)} />
        {window.featureOn('line_churn') && <Stat label="lines added / deleted" value={
          <span style={{ whiteSpace: 'normal' }}>
            <span style={{ color: window.dashboardCol.linesAdded, display: 'inline-block' }}>+{window.humanFmt(totals.linesAdded)}</span>
            <span style={{ color: 'var(--muted-2)' }}> / </span>
            <span style={{ color: window.dashboardCol.linesDeleted, display: 'inline-block' }}>−{window.humanFmt(totals.linesDeleted)}</span>
          </span>
        } />}
        {totals.cost > 0 && <Stat label="total cost" value={totalCostStr} highlight />}
        {!hideRateLimits && <Stat label="rate-limit hits" value={String(limitHits.length)} warn={limitHits.length > 0} />}
      </div>
      )}

      {hasData && (<>
      {/* Read by src/perf.js walking UP from a shifted node: a Layout
          Instability entry names the elements that moved, so the nearest
          marked ancestor is the region it is attributed to. */}
      <div className="dash-grid" data-perf-region="panel_grid">
        {window.featureOn('usage_timeseries') && panels.input && (
        <window.TimeSeriesPanel title="Input Tokens"  events={events} valueKey="input_tokens"
          color={window.dashboardCol.inputTokens} range={range} binMs={binMs} />)}
        {window.featureOn('usage_timeseries') && panels.output && (
        <window.TimeSeriesPanel title="Output Tokens" events={events} valueKey="output_tokens"
          color={window.dashboardCol.outputTokens} range={range} binMs={binMs} />)}
        {window.featureOn('thinking_output') && panels.thinking && (
        <window.TimeSeriesPanel title="Thinking Output" events={events} valueKey="thinking_tokens"
          color={window.dashboardCol.thinkingTokens} range={range} binMs={binMs} />)}
        {window.featureOn('usage_timeseries') && panels.cacheCreate && (
        <window.TimeSeriesPanel title="Cache Create"  events={events} valueKey="cache_create"
          color={window.dashboardCol.cacheCreateTokens} range={range} binMs={binMs} />)}
        {window.featureOn('usage_timeseries') && panels.cacheRead && (
        <window.TimeSeriesPanel title="Cache Read"    events={events} valueKey="cache_read"
          color={window.dashboardCol.cacheReadTokens} range={range} binMs={binMs} />)}
        {window.featureOn('usage_timeseries') && panels.any && (
        <window.TimeSeriesPanel title="Total Tokens"  events={events.map(e => ({...e, _t: e.input_tokens+e.output_tokens+e.cache_create+e.cache_read}))}
          valueKey="_t" color={window.dashboardCol.totalTokens} range={range} binMs={binMs} />)}
        {window.featureOn('usage_timeseries') && window.hasSeries(events, 'cost_usd') && (
        <window.TimeSeriesPanel title="Cost (USD)"    events={events} valueKey="cost_usd"
          color={window.dashboardCol.costUSD} range={range} binMs={binMs} isCurrency />)}
        {window.featureOn('line_churn') && window.hasSeries(events, 'lines_added') && (
        <window.TimeSeriesPanel title="Lines Added"   events={events} valueKey="lines_added"
          color={window.dashboardCol.linesAdded} range={range} binMs={binMs} />)}
        {window.featureOn('line_churn') && window.hasSeries(events, 'lines_deleted') && (
        <window.TimeSeriesPanel title="Lines Deleted" events={events} valueKey="lines_deleted"
          color={window.dashboardCol.linesDeleted} range={range} binMs={binMs} />)}
      </div>

      {window.featureOn('model_comparison') && <div className="dash-grid-2">
        {costByModelTotal > 0 && (
        <window.HBar listPanel
          title="Cost by Model"
          rows={costByModel}
          fixedColors={window.modelColors} groupByModel partFmt={v => window.humanCurrency(v)}
          fmt={r => `${window.humanCurrency(r.value)} (${(r.value / costByModelTotal * 100).toFixed(1)}%)`} />
        )}
        <window.HBar listPanel
          title="Tokens by Model"
          rows={tokensByModel}
          fixedColors={window.modelColors} groupByModel partFmt={v => window.humanFmt(v)}
          fmt={r => `${window.humanFmt(r.value)} (${tokensByModelTotal > 0 ? (r.value / tokensByModelTotal * 100).toFixed(1) : '0.0'}%)`} />
      </div>}

      {window.featureOn('token_breakdown') && <div className="dash-grid-2">
        <TokenBreakdownPanel events={events} />
      </div>}

      {/* Only meaningful with the project filter on "All" — a single
          selected project would make this a one-bar chart. */}
      {window.featureOn('project_comparison') && activeProject === '' && costByProject.length > 0 && (
        <div className="dash-resp">
          <window.VBar
            title="Cost by Project"
            rows={costByProject}
            fmt={r => window.humanCurrency(r.value)} />
        </div>
      )}

      {window.featureOn('project_comparison') && activeProject === '' && tokensByProject.length > 0 && (
        <div className="dash-resp">
          <window.VBar
            title="Tokens by Project"
            rows={tokensByProject}
            fmt={r => window.humanFmt(r.value)} />
        </div>
      )}

      {window.featureOn('cache_ttl') && panels.cacheCreate && (
      <div className="dash-ttl">
        <window.CacheTTLPanel events={events} range={range} binMs={binMs} />
      </div>
      )}

      {window.featureOn('response_sizes') && responseSizes && responseSizes.length > 0 && (
        <div className="dash-resp">
          <window.ResponseSizesPanel data={responseSizes} bucketS={bucketS} />
        </div>
      )}

      </>)}

      {independentReady && window.featureOn('context_growth') && <div className="dash-context">
        <window.ContextGrowthPanel events={events} realSessions={sessionsOverride}
            project={activeProject} range={activeRange} nonce={dashNonce} />
      </div>}

      {hasData && (<>
      {window.featureOn('burn_rate') && <div className="dash-burn">
        <window.BurnRatePanel
          events={events}
          sessions={sessions}
          limitHits={limitHits} hideRateLimits={hideRateLimits}
          range={range}
          windowBoundaries={windowBoundaries} />
      </div>}

      </>)}

      {/* Independent endpoints remain usable without shared hours. With
          hourly data, preserve the cost panel's zero-cost suppression. */}
      {backendOn && independentReady && window.featureOn('context_comparison') && (hasCost || !sharedCostEnabled || !events.length) && (
        <div className="dash-ctx-cost">
          <window.CostByContextPanel
            models={models}
            project={activeProject}
            range={activeRange}
            nonce={dashNonce} />
        </div>
      )}

      {backendOn && independentReady && window.featureOn('context_comparison') && (
        <div className="dash-ctx-cost">
          <window.CostByContextPanel
            measure="tokens"
            models={models}
            project={activeProject}
            range={activeRange}
            nonce={dashNonce} />
        </div>
      )}
      {/* Self-fetching panels: mounted regardless of dashboard data, and
          BELOW every data-gated block; its sections stack with
          .dashboard's own 14px flex gap — a plain block wrapper
          holds its content out of the gap context, flush. Each fills
          when its own response lands; until the first /api/dashboard
          resolves they lay out at final heights, painting nothing. */}
      {backendOn && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14,
          ...(dashLoading && !everLoaded.current ? { visibility: 'hidden' } : {}) }}>
        {window.featureOn('tool_usage') && <div className="dash-tools">
          <window.ToolUsagePanel
            models={models}
            project={activeProject}
            range={activeRange}
            nonce={dashNonce} />
        </div>}

      {backendOn && window.featureOn('reply_latency') && (
        <div className="dash-latency">
          <window.ReplyLatencyPanel
            models={models}
            project={activeProject}
            range={activeRange}
            nonce={dashNonce} />
        </div>
      )}

      {/* Self-fetching like the four above, operator-only. */}
      {backendOn && window.IS_OPERATOR && window.featureOn('page_performance') && (
        <div className="dash-resp">
          {/* No `project`: a beacon carries none. */}
          <window.WebMetricsPanel
            range={activeRange}
            nonce={dashNonce} />
        </div>
      )}

      {backendOn && window.featureOn('tool_error_rate') && (
        <div className="dash-tool-errors">
          <window.ToolErrorRatePanel
            project={activeProject}
            range={activeRange}
            nonce={dashNonce} />
        </div>
      )}

      {backendOn && window.featureOn('agent_comparison') && (
        <div className="dash-resp">
          <window.CostByAgentPanel
            models={models}
            project={activeProject}
            range={activeRange}
            nonce={dashNonce} />
        </div>
      )}

      {backendOn && window.featureOn('activity_heatmap') && (
        <div className="dash-heatmap">
          <window.ActivityHeatmapPanel
            models={models}
            project={activeProject}
            range={activeRange}
            nonce={dashNonce} />
        </div>
      )}


        </div>
      )}
    </div>
  );
}

window.Dashboard = Dashboard;
