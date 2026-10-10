// Defer a non-urgent transform out of the commit that produced it
// (issue #838). The Per-Session Context Growth panel fetches its own
// traces and then builds per-model groups over every trace — a transform
// that ran in the same commit that landed the fetch and blocked the frame.
// Routing the result through this helper moves the transform to an idle
// callback (or the next macrotask where requestIdleCallback is absent), so
// the fetch's commit stays cheap.
//
// Plain JS on purpose — no React, no JSX — so node can execute it and
// tests/test_deferred_js.py drives the real thing. The browser globals it
// touches (requestIdleCallback / cancelIdleCallback / setTimeout /
// clearTimeout) are read per CALL, never at load, so requiring this file
// in node is safe.
(function () {
  function run(value, cb) {
    // Decide the scheduling path per call: a page that gains or loses
    // requestIdleCallback (a polyfill loading late, a browser quirk) must
    // not be stuck with the decision made at load.
    const handle = { kind: null, id: null, cancelled: false };
    const fire = () => { if (!handle.cancelled) cb(value); };
    if (typeof requestIdleCallback === 'function') {
      handle.kind = 'idle';
      handle.id = requestIdleCallback(fire);
    } else {
      handle.kind = 'timeout';
      handle.id = setTimeout(fire, 0);
    }
    return handle;
  }

  // Cancel a pending run. The cancelled flag makes a callback that already
  // fired, or one the platform refuses to unschedule, a no-op.
  function cancel(handle) {
    if (!handle || handle.cancelled) return;
    handle.cancelled = true;
    if (handle.kind === 'idle') {
      if (typeof cancelIdleCallback === 'function') cancelIdleCallback(handle.id);
    } else {
      clearTimeout(handle.id);
    }
  }

  window.deferred = { run: run, cancel: cancel };
})();
