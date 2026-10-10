// The shared in-flight /api/cost-by-context fetch. Moved verbatim out of
// src/dashboard-charts-extra.jsx (issue #839) so a plain-JS test can drive
// it through node — nothing else in the suite could see that the shared
// fetch in `fetchCostByContext` had been reverted.
//
// app.jsx renders the tokens and the cost variants of this panel side by
// side; both draw from ONE endpoint response, but each instance used to
// GET the same URL. The first caller to ask for a URL starts the fetch,
// the second joins the in-flight promise (issue #644).
//
// Plain JS on purpose — no React, no JSX — so node can execute it and
// tests/test_dashboard_latency_pins.py drives the real thing. `fetch` is
// read at call time, never at load.
(function () {
  const pending = new Map();

  function fetchCostByContext(url) {
    let p = pending.get(url);
    if (!p) {
      p = fetch(url, { credentials: 'same-origin' }).then(r => r.json());
      pending.set(url, p);
      // Drop the entry once settled: a later refetch (a range or model
      // change) must re-request, not read a stale body.
      p.finally(() => { if (pending.get(url) === p) pending.delete(url); });
    }
    return p;
  }

  window.costByContext = { fetch: fetchCostByContext };
})();
