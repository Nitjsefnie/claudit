// The Token Breakdown decomposition (SV-COST-SPLIT): a pure fold over
// dashboard events, priced per record exactly as pricing.compute_cost
// stores it — long-context meter and per-search cost included (issue
// #879) — so the panel's bars always sum to the stored totals they
// decompose. Mirrors backend/api_common.py's fold. Plain JS on window,
// loaded before app.jsx.
window.computeTokenBreakdown = function computeTokenBreakdown(events) {
  const t = { input: 0, output: 0, cc: 0, cr: 0, eph5: 0, eph1h: 0 };
  for (const e of events) {
    t.input += e.input_tokens; t.output += e.output_tokens;
    t.cc += e.cache_create; t.cr += e.cache_read;
    t.eph5 += e.ephemeral_5m; t.eph1h += e.ephemeral_1h;
  }
  const tokenTotal = t.input + t.output + t.cc + t.cr;
  const ccUnsplit = Math.max(0, t.cc - t.eph5 - t.eph1h);

  const c = { input: 0, output: 0, eph5: 0, eph1h: 0, ccUnsplit: 0, cr: 0 };
  let searchRequests = 0;
  let searchCost = 0;
  let tokenCost = 0;
  if (window.rateForModel) {
    for (const e of events) {
      // An event without its raw id is priced by the name it still carries (a Claude short name resolves to its family tier — an estimate); one without either lands on the default row.
      const res = window.resolveModelRate(e.model_id || e.model, e.ts, e.provider);
      const r = res.rates;
      // Apply the model's factors so the Inspector's bucket split sums to
      // the stored long-context cost (SV-DATED-RATES).
      const [lcIn, lcOut] = e.long_context
        ? window.longContextFactorsFor(e.model_id || e.model)
        : [1.0, 1.0];
      const unsplit = Math.max(0, (e.cache_create || 0) - (e.ephemeral_5m || 0) - (e.ephemeral_1h || 0));
      c.input     += (e.input_tokens   || 0) * r.fresh * lcIn;
      c.output    += (e.output_tokens  || 0) * r.out * lcOut;
      c.eph5      += (e.ephemeral_5m   || 0) * r.c5 * lcIn;
      c.eph1h     += (e.ephemeral_1h   || 0) * r.c1h * lcIn;
      c.ccUnsplit += unsplit                  * r.c1h * lcIn; // unsplit at 1h rate
      c.cr        += (e.cache_read     || 0) * r.read * lcIn;
      const searches = e.web_search_requests || 0;
      searchRequests += searches;
      searchCost += searches * (r.search || 0);
    }
    for (const k of Object.keys(c)) c[k] = c[k] / 1_000_000;
    tokenCost = c.input + c.output + c.eph5 + c.eph1h + c.ccUnsplit + c.cr;
  }
  const costTotal = tokenCost + searchCost;

  const rows = [
    { label: 'Input',             value: t.input,  cost: c.input,     color: window.dashboardCol.inputTokens },
    { label: 'Output',            value: t.output, cost: c.output,    color: window.dashboardCol.outputTokens },
    { label: 'Cache Create (5m)', value: t.eph5,   cost: c.eph5,      color: window.dashboardCol.cacheCreateTokens },
    { label: 'Cache Create (1h)', value: t.eph1h,  cost: c.eph1h,     color: '#d488ff' },
    ...(ccUnsplit > 0
      ? [{ label: 'Cache Create (unsplit)', value: ccUnsplit, cost: c.ccUnsplit, color: '#7733aa' }]
      : []),
    { label: 'Cache Read',        value: t.cr,     cost: c.cr,        color: window.dashboardCol.cacheReadTokens },
  ].filter(r => r.value > 0).sort((a, b) => b.cost - a.cost);

  const costRows = [...rows];
  if (searchRequests > 0) {
    costRows.push({
      label: 'Web Search', value: searchRequests, cost: searchCost,
      color: window.dashboardCol.costUSD,
    });
  }
  costRows.sort((a, b) => b.cost - a.cost);

  return { rows, costRows, tokenTotal: tokenTotal || 1,
           costTotal: costTotal || 1, searchRequests };
};
