// A Cost/Tokens by Model row label. A record that named its serving host
// (OpenRouter) is split per host; one that named none keeps the bare model
// label, the lane's own row, with no invented host name.
function modelProviderLabel(model, provider) {
  return provider ? `${model} · ${provider}` : model;
}

function txToDashData(tx) {
  // Convert a real transcript into dashboard-shaped {events, limitHits, range}.
  // Each event = ONE assistant turn (after applying the turn-stats
  // algorithm: user-text boundaries → last usage per turn).
  // Rates come from the shared window.modelRates table (parser.js) so the
  // Inspector, Token Breakdown, and this path can never disagree on pricing.
  const shortM = (model) => window.shortModelName(model || 'unknown');

  const usageBySid = new Map();
  for (const u of tx.meta) {
    if (u.type !== 'assistant_usage') continue;
    const sid = u.sessionId || 'live';
    if (!usageBySid.has(sid)) usageBySid.set(sid, []);
    usageBySid.get(sid).push(u);
  }

  const boundaryLinesBySid = new Map();
  const allEv = tx.eventsBySession ? null : tx.events;
  if (tx.eventsBySession) {
    for (const [sid, evs] of tx.eventsBySession) {
      const lines = [];
      for (const e of evs) {
        if (e.type === 'user_message' && typeof e.detail === 'string' && e.detail.trim()) {
          lines.push(e.line);
        }
      }
      lines.sort((a, b) => a - b);
      boundaryLinesBySid.set(sid, lines);
    }
  } else if (allEv) {
    const byS = new Map();
    for (const e of allEv) {
      if (e.type === 'user_message' && typeof e.detail === 'string' && e.detail.trim()) {
        const sid = e.sessionId || (usageBySid.size === 1 ? [...usageBySid.keys()][0] : 'live');
        if (!byS.has(sid)) byS.set(sid, []);
        byS.get(sid).push(e.line);
      }
    }
    for (const [sid, ls] of byS) { ls.sort((a,b)=>a-b); boundaryLinesBySid.set(sid, ls); }
  }

  const events = [];
  for (const [sid, usages] of usageBySid) {
    usages.sort((a, b) => a.line - b.line);
    const bounds = boundaryLinesBySid.get(sid) || [];
    // Bucket usages into turns: each bound starts a turn.
    // Usages before the first bound form turn 0 (initial system→assistant).
    const turns = [];
    let bi = 0;
    let cur = [];
    for (const u of usages) {
      while (bi < bounds.length && bounds[bi] <= u.line) {
        if (cur.length) turns.push(cur);
        cur = [];
        bi++;
      }
      cur.push(u);
    }
    if (cur.length) turns.push(cur);
    // If no bounds were found (single-file path with all usages, no user_text
    // captured), fall back to one-usage-per-turn so the panel still works.
    const turnUsages = bounds.length ? turns.map(t => t[t.length - 1]) : usages;
    let turnIdx = 0;
    for (const u of turnUsages) {
      const t = Date.parse(u.ts); if (isNaN(t)) continue;
      const us = u.usage || {};
      const inp = us.input_tokens || 0;
      const out = us.output_tokens || 0;
      const cc  = us.cache_creation_input_tokens || 0;
      const cr  = us.cache_read_input_tokens || 0;
      if ((inp + cc + cr) === 0 && (u.web_search_requests || 0) === 0) continue;
      const eph5 = (us.cache_creation && us.cache_creation.ephemeral_5m_input_tokens) || 0;
      const eph1h = (us.cache_creation && us.cache_creation.ephemeral_1h_input_tokens) || 0;
      const res = window.resolveModelRate(u.model, t, u.provider);
      const r = res.rates;
      const unsplit = Math.max(0, cc - eph5 - eph1h);
      // Match pricing.compute_cost's per-model factors; lane meta records
      // carry the flag and the inspector must match stored cost.
      const [lcIn, lcOut] = u.long_context
        ? window.longContextFactorsFor(u.model) : [1.0, 1.0];
      const cost = (inp * r.fresh * lcIn + out * r.out * lcOut + eph5 * r.c5 * lcIn + (eph1h + unsplit) * r.c1h * lcIn + cr * r.read * lcIn) / 1_000_000 + (u.web_search_requests || 0) * (r.search || 0);
      events.push({
        ts: t,
        session_id: sid,
        turn_index: turnIdx++,
        model: shortM(u.model),
        model_id: u.model,
        provider: u.provider || null, web_search_requests: u.web_search_requests || null,
        input_tokens: inp,
        output_tokens: out,
        cache_create: cc,
        cache_read: cr,
        ephemeral_5m: eph5,
        ephemeral_1h: eph1h,
        cost_usd: cost,
        ctx: window.usageCtxInput(us),
      });
    }
  }
  events.sort((a, b) => a.ts - b.ts);
  const limitHits = (window.featureOn('rate_limits') ? tx.meta : [])
    .filter(m => m.type === 'rate_limit')
    .map(m => ({ ts: Date.parse(m.ts), text: m.content || 'rate limit' }))
    .filter(x => !isNaN(x.ts));
  if (!events.length) return null;
  const start = events[0].ts;
  const end = events[events.length - 1].ts;
  const pad = Math.max((end - start) * 0.02, 60_000);
  return { events, limitHits, range: { start: start - pad, end: end + pad } };
}

function backendAggregateRange(events, bucketS) {
  if (!events.length) {
    const now = Date.now();
    return { start: now - 86400000, end: now };
  }
  const first = events[0].ts;
  const last = events[events.length - 1].ts;
  const bucketMs = Number(bucketS) * 1000;
  if (Number.isFinite(bucketMs) && bucketMs > 0) {
    return { start: first - bucketMs / 2, end: last + bucketMs / 2 };
  }
  return { start: first, end: last + 1 };
}

function backendDashToShape(b) {
  // Canonicalize backend snapshot ids with a -YYYYMMDD suffix to bare
  // model names ONCE here, so every downstream
  // consumer (model colors, Cost by Model labels, burn-rate dots) agrees.
  const short = m => window.shortModelName(m || 'unknown');
  const events = (b.hourly || []).map((h, i) => ({
    ts: Date.parse(h.hour),
    session_id: 'backend-h' + i,
    turn_index: 0,
    model: short(h.model),
    // The raw id the backend priced cost_usd by. `model` is for display:
    // a short name matches only its family's tier, i.e. its NEWEST rate.
    model_id: h.model,
    // Every token field is OPTIONAL: the backend drops any type whose
    // total is zero across the range (api_dashboard.drop_zero_token_types),
    // so these keys can be absent and `a + b` on two absent ones is NaN,
    // not 0 — which is what silently poisons a whole panel.
    input_tokens: h.input_tokens || 0,
    output_tokens: h.output_tokens || 0,
    // A SUBSET of output_tokens, plotted on its own panel and never
    // added to a total (see api_dashboard.TOKEN_TYPE_FIELDS).
    thinking_tokens: h.thinking_tokens || 0,
    cache_create: (h.cache_5m_tokens || 0) + (h.cache_1h_tokens || 0),
    cache_read: h.cache_read_tokens || 0,
    ephemeral_5m: h.cache_5m_tokens || 0,
    ephemeral_1h: h.cache_1h_tokens || 0,
    // The Codex long-context meter rides the hourly grain: rows tagged
    // true bill at 2x input side / 1.5x output, which the Token
    // Breakdown must apply or its bars drift from the stored total.
    long_context: !!h.long_context,
    provider: h.provider || null,
    cost_usd: h.cost_usd || 0,
    lines_added: h.lines_added || 0,
    lines_deleted: h.lines_deleted || 0,
    requests: h.requests || 1,
    session_count: h.session_count || 0,
  })).filter(e => !isNaN(e.ts));
  if (!events.length && !['sessions', 'response_sizes', 'cost_by_project', 'tokens_by_project']
      .some(key => (b[key] || []).length)) return null;
  const range = backendAggregateRange(events, b.bucket_s);
  const costByModel = (b.cost_by_model_provider || b.cost_by_model || []).reduce((acc, r) => {
    const key = modelProviderLabel(short(r.model), r.provider);
    acc[key] = (acc[key] || 0) + (r.cost_usd || 0);
    return acc;
  }, {});
  const tokensByModel = (b.cost_by_model_provider || []).reduce((acc, r) => {
    const key = modelProviderLabel(short(r.model), r.provider);
    acc[key] = (acc[key] || 0) + (r.total_tokens || 0);
    return acc;
  }, {});
  const limitHits = (window.featureOn('rate_limits') && !b.hide_rate_limits ? (b.rate_limit_hits || []) : [])
    .map(h => ({ ts: Date.parse(h.ts), text: h.content || 'rate limit' }))
    .filter(h => !isNaN(h.ts));
  const sessions = (b.sessions || []).map(s => {
    const startMs = (s.start_ts || 0) * 1000;
    const endMs = (s.end_ts || s.start_ts || 0) * 1000;
    const synthEvent = {
      ts: startMs,
      session_id: s.session_id,
      turn_index: 0,
      model: short(s.model),
      input_tokens: s.input_tokens,
      output_tokens: s.output_tokens,
      cache_create: s.cache_create_tokens,
      cache_read: s.cache_read_tokens,
      ephemeral_5m: 0,
      ephemeral_1h: 0,
      cost_usd: s.cost_usd,
      requests: s.requests,
    };
    return {
      start: startMs,
      end: endMs,
      events: [synthEvent],
      ctxEnd: s.ctx_at_end != null ? s.ctx_at_end : null,
      session_id: s.session_id,
      requests: s.requests,
      model: short(s.model),
      models_used: (s.models_used || []).map(short),
    };
  });
  return {
    events, limitHits, range, costByModel, tokensByModel,
    costByProject: b.cost_by_project || [],
    tokensByProject: b.tokens_by_project || [],
    sessionsOverride: sessions,
    totalSessions: b.total_sessions,
    mainWUsage: b.main_w_usage,
    mainEmpty: b.main_empty,
    subagentFiles: b.subagent_files,
    subagentOnlySessions: b.subagent_only_sessions,
    totalPrompts: b.total_prompts,
    totalTurns: b.total_turns,
    responseSizes: b.response_sizes || [], hideRateLimits: !!b.hide_rate_limits || !window.featureOn('rate_limits'),
    bucketS: b.bucket_s || 86400,
    // Which token types survived the backend's zero-suppression
    // (api_dashboard.drop_zero_token_types). Absent on the synthetic
    // preview path, where tokenPanels() falls back to summing.
    tokenTypes: b.token_types || null,
  };
}

window.backendDashToShape = backendDashToShape;
window.txToDashData = txToDashData;
window.modelProviderLabel = modelProviderLabel;

// Shared endpoint eligibility mirrors the server's consumer union.
window.dashboardFeaturesOn = () => [
  'usage_timeseries', 'thinking_output', 'line_churn', 'model_comparison',
  'token_breakdown', 'project_comparison', 'cache_ttl', 'response_sizes',
  'burn_rate', 'summary', 'sessions',
].some(window.featureOn);
window.modelFeaturesOn = () => [
  'model_comparison', 'token_breakdown', 'context_growth', 'context_comparison',
  'tool_usage', 'reply_latency', 'page_performance', 'tool_error_rate',
  'agent_comparison', 'activity_heatmap', 'inspector',
].some(window.featureOn);
window.featureRoute = (route, isGuest, backendOn) => {
  const keys = { sessions: 'sessions', cache: 'cache', session: 'inspector' };
  if (keys[route] && (!window.featureOn(keys[route])
      || (isGuest && route !== 'cache') || (route === 'cache' && !backendOn))) return 'dashboard';
  return route;
};
