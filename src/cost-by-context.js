// Both side-by-side panel variants share one endpoint response: the first
// caller starts the fetch and the second joins the in-flight promise.
// Read `fetch` at call time, never at load.
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
