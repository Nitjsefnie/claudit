// The Overview's /api/dashboard request state, split from its data
// (issue #394).
//
// The fetch used to answer the panel set through the DATA alone: a null
// aggregate (an empty range) and a failed request both left the summary
// on its "loading…" placeholder, forever, because a null payload was
// read as "still in flight". Three outcomes have to be told apart, and
// only the request knows which one it is.
//
// Plain JS on purpose — no React, no JSX — so node can execute it and
// tests/test_dashboard_fetch_js.py drives the real thing.
(function () {
  const LOADING = 'loading';
  const READY = 'ready';
  const ERROR = 'error';

  // The empty summary reads as every other panel's: "no … data in range".
  const EMPTY_TEXT = 'no usage data in range';

  function start() { return { status: LOADING, detail: '' }; }

  function loaded() { return { status: READY, detail: '' }; }

  function failed(detail) {
    const text = detail === undefined || detail === null ? '' : String(detail).trim();
    return { status: ERROR, detail: text || 'request failed' };
  }

  // What the summary block renders, decided from the STATE first and the
  // rendered shape second:
  //
  //   loading            → the placeholder, while the request is in flight
  //   ready   + no shape → the empty state (a range with no rows)
  //   ready   + a shape  → the stat block
  //   error              → the status, plus a detail line naming it
  //   error + a shape    → the stat block AND the error: a refetch that
  //                        fails leaves the previous response's numbers
  //                        on screen, and quietly keeping them is worse
  //                        than saying they are stale
  //
  // `hasData` is what the panels actually rendered (backendDashToShape
  // returns null for a payload with no usable hour buckets): the shape
  // answers WHETHER there is data, and the state answers WHICH of the
  // other three states the caller is in when there is not.
  function summary(state, hasData) {
    if (state && state.status === ERROR) {
      const detail = state.detail;
      return hasData
        ? { kind: 'data', text: '', detail: detail, error: true }
        : { kind: 'error', text: 'error', detail: detail, error: true };
    }
    if (hasData) return { kind: 'data', text: '', detail: '', error: false };
    if (state && state.status === LOADING) {
      return { kind: 'loading', text: 'loading…', detail: '', error: false };
    }
    return { kind: 'empty', text: EMPTY_TEXT, detail: '', error: false };
  }

  // GET /api/dashboard → the parsed payload, or throw naming the status.
  // A failed request is not JSON by default (a 500 is text/plain), so the
  // body is read defensively for a `detail` field and the status stands
  // alone when there is none — the same shape the transcript loader uses.
  function load(url, init, fetchImpl) {
    const opts = Object.assign({ credentials: 'same-origin' }, init || {});
    return (fetchImpl || fetch)(url, opts).then(r => {
      if (r.ok) return r.json();
      return r.json().then(
        body => {
          const detail = body && typeof body.detail === 'string' ? body.detail : '';
          throw new Error('HTTP ' + r.status + (detail ? ': ' + detail : ''));
        },
        () => { throw new Error('HTTP ' + r.status); },
      );
    });
  }

  window.dashboardFetch = {
    LOADING: LOADING, READY: READY, ERROR: ERROR,
    EMPTY_TEXT: EMPTY_TEXT,
    start: start, loaded: loaded, failed: failed,
    summary: summary, load: load,
  };
})();
