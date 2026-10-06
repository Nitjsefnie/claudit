// Browser performance telemetry (issue #436): journey timings, layout
// stability and main-thread blocking, batched into POST /api/metrics.
//
// Three journeys are timed, each as a total with a fetch/client split so
// a slow server can be told apart from a slow browser, and two
// PerformanceObservers carry the observed metrics.
//
// Plain JS on purpose -- no React, no JSX -- so node can execute it and
// tests/test_perf_js.py drives the real thing against faked browser
// globals. Nothing here may ever throw into a render or surface to the
// user: every entry point is guarded and a batch that cannot be sent is
// dropped, not raised.
//
// Two properties of the sink shape every line below. `metric` x `part`
// is a CLOSED table and an out-of-range value is CLAMPED, never refused
// -- so a beacon that would be rejected is not sent at all rather than
// counted on the server to catch it. And a part that was never timed is
// never sent: the panel renders a missing part as an em dash, while a
// fabricated 0 would read as a measurement nobody took.
(function () {
  const ENDPOINT = '/api/metrics';
  // The sink's per-request beacon ceiling.
  const MAX_BATCH = 50;
  const FLUSH_MS = 3000;

  // Written by the sign-in page on submit (see backend/login_page.py) and
  // consumed here on the document the 303 lands on. The two documents
  // have different time origins, so the elapsed journey is only knowable
  // by bridging them through wall-clock epoch ms -- which is what the
  // origin half of the marker carries.
  const SIGNIN_START = 'claudit.signin.start';
  const SIGNIN_ORIGIN = 'claudit.signin.origin';

  // The journey names the sink's table admits. A name outside this set
  // is a 400, so a typo in a call site reports nothing rather than
  // sending a batch that takes every other beacon in it down.
  const JOURNEYS = ['dashboard_open', 'inspector_open', 'signin'];
  const REGIONS = ['panel_grid', 'inspector', 'signin', 'other'];

  let phase = 'pre_paint';
  const open = {};   // journey name -> { t0, fetch }
  let buffer = [];
  let timer = null;

  // Epoch ms, so a journey can be measured across the sign-in redirect:
  // two documents, two time origins, one clock. Durations are differences,
  // so the origin itself never leaks into a reported value.
  function now() {
    const p = typeof performance !== 'undefined' ? performance : null;
    if (p && typeof p.now === 'function') {
      if (typeof p.timeOrigin === 'number') return p.timeOrigin + p.now();
      return p.now();
    }
    return Date.now();
  }

  function ok(v) { return typeof v === 'number' && isFinite(v) && v >= 0; }

  // --- transport -----------------------------------------------------

  // Same-origin throughout: the sink sits behind the auth middleware, and
  // a cross-origin beacon would be refused anyway.
  function send(batch) {
    const body = JSON.stringify({ beacons: batch });
    let blob = null;
    try {
      blob = new Blob([body], { type: 'application/json' });
    } catch (_) { blob = null; }
    const nav = typeof navigator !== 'undefined' ? navigator : null;
    if (blob && nav && typeof nav.sendBeacon === 'function') {
      try {
        // sendBeacon answers false when its queue is full; the fetch
        // fallback below then carries the batch rather than losing it.
        if (nav.sendBeacon(ENDPOINT, blob)) return;
      } catch (_) { /* fall through */ }
    }
    if (typeof fetch === 'function') {
      try {
        // keepalive is what lets the batch outlive the document on
        // pagehide; the catch keeps an aborted request off the console.
        fetch(ENDPOINT, {
          method: 'POST',
          credentials: 'same-origin',
          keepalive: true,
          body: body,
          headers: { 'Content-Type': 'application/json' },
        }).catch(function () {});
        return;
      } catch (_) { /* dropped below */ }
    }
    // Neither transport: the batch is dropped. Telemetry never breaks a
    // panel, so there is nothing left to do.
  }

  function flush() {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
    // An empty buffer sends nothing: the sink requires a non-empty list,
    // so an empty POST would be a 400 on every single page view.
    if (!buffer.length) return;
    const batch = buffer;
    buffer = [];
    for (let i = 0; i < batch.length; i += MAX_BATCH) {
      send(batch.slice(i, i + MAX_BATCH));
    }
  }

  // A page can produce a burst of shifts during progressive load, so
  // beacons are buffered and flushed on a short timer -- and immediately
  // when the page hides, since there is no later tick to flush on.
  function push(beacon) {
    buffer.push(beacon);
    if (buffer.length >= MAX_BATCH) { flush(); return; }
    if (timer === null) timer = setTimeout(flush, FLUSH_MS);
  }

  // --- journeys ------------------------------------------------------

  function openJourney(name) {
    if (JOURNEYS.indexOf(name) < 0) return;
    open[name] = { t0: now(), fetch: null };
  }

  function closeFetch(name) {
    const j = open[name];
    if (!j) return;
    j.fetch = now() - j.t0;
  }

  function closeJourney(name) {
    const j = open[name];
    if (!j) return;               // never opened: not a measurement
    delete open[name];
    // Past the first paint by definition, so an SSE-phase observation is
    // over and the page is simply usable again.
    phase = 'post_usable';
    const total = now() - j.t0;
    if (!ok(total)) return;
    const rows = [{ metric: name, part: 'total', value: total }];
    if (j.fetch !== null) {
      // The split is the whole point of issue #436 item (1): a total that
      // regressed while its client share did not is a different problem.
      // A fetch that outlives its own total is dropped rather than sent
      // for the server to clamp into a negative duration.
      if (ok(j.fetch) && j.fetch <= total) {
        rows.push({ metric: name, part: 'fetch', value: j.fetch });
        rows.push({ metric: name, part: 'client', value: total - j.fetch });
      }
    }
    for (const row of rows) push(row);
  }

  function markUsable() {
    if (phase === 'pre_paint') phase = 'post_usable';
    // The sign-in journey spans the 303, so it ends here: the signed-in
    // page has become usable.
    if (open.signin) closeJourney('signin');
  }

  function sseUpdate() { phase = 'sse_update'; }

  // --- the sign-in journey across the redirect -----------------------

  // The new document's own Navigation Timing is a real measurement of the
  // same navigation -- time from navigation start to first byte -- so the
  // sign-in fetch part is read from it rather than guessed at. It is only
  // consulted when the login page's marker says a sign-in really happened:
  // Navigation Timing exists on every navigation, including a plain load.
  //
  // It is NOT the same interval as the other two journeys' `fetch`, and the
  // panel's three rows sit side by side, so say so here where a reader of
  // this function will see it: this one spans the whole chain (the POST,
  // the 303, and the GET of `/` that follows it), where a dashboard or
  // Inspector `fetch` is one endpoint's wait. Comparing a sign-in fetch
  // against the other two compares a chain with a request. When Navigation
  // Timing is absent the part is simply not sent, and the panel's em dash
  // says "not measured" rather than charging the network to the client.
  function navigationFetchMs() {
    const p = typeof performance !== 'undefined' ? performance : null;
    if (!p || typeof p.getEntriesByType !== 'function') return null;
    let list;
    try { list = p.getEntriesByType('navigation') || []; } catch (_) { return null; }
    const nav = list[0];
    if (!nav) return null;
    const v = nav.responseStart - nav.startTime;
    return ok(v) ? v : null;
  }

  function adoptSigninStart() {
    let raw = null;
    let origin = null;
    try {
      if (typeof sessionStorage === 'undefined') return;
      raw = sessionStorage.getItem(SIGNIN_START);
      origin = sessionStorage.getItem(SIGNIN_ORIGIN);
      // Consumed, so a reload does not report the same journey twice.
      sessionStorage.removeItem(SIGNIN_START);
      sessionStorage.removeItem(SIGNIN_ORIGIN);
    } catch (_) { return; }
    if (raw === null || origin === null) return;
    const start = Number(raw);
    const base = Number(origin);
    if (!isFinite(start) || !isFinite(base)) return;
    open.signin = { t0: base + start, fetch: navigationFetchMs() };
  }

  // --- the observed metrics ------------------------------------------

  // The nearest ancestor (or the node itself) naming a region. An
  // attribute value outside the closed set is NOT echoed: it would be a
  // 400 that takes the whole batch with it.
  //
  // #647: only the grid and the Inspector carry `data-perf-region`, so a
  // live node inside one panel — under no named region — used to land in
  // `other`, and `other` is what most production shifts carried. The
  // second walk reads the nearest `[data-panel]` ancestor the panels
  // already render and attributes it as `panel_grid`. The closed
  // vocabulary does not take the panel NAME, so the name is read only to
  // decide that the node IS in a panel; the term keeps meaning 'outside
  // the panel grid' for whatever remains.
  function region(node) {
    let el = node;
    while (el && typeof el.getAttribute === 'function') {
      const r = el.getAttribute('data-perf-region');
      if (r && REGIONS.indexOf(r) >= 0) return r;
      el = el.parentElement;
    }
    el = node;
    while (el && typeof el.getAttribute === 'function') {
      if (el.getAttribute('data-panel')) return 'panel_grid';
      el = el.parentElement;
    }
    return 'other';
  }

  function phaseNow() { return phase; }

  // Registration is per-observer and per-type: `observe()` throws in a
  // browser that lacks the entry type, and one refusal must not take the
  // other metric down with it. `buffered` replays entries emitted before
  // this script finished loading, which is where the first-paint shifts
  // are.
  function observe(type, handler) {
    if (typeof PerformanceObserver !== 'function') return;
    try {
      const po = new PerformanceObserver(function (list) {
        let entries;
        try { entries = list.getEntries(); } catch (_) { return; }
        for (const entry of entries) {
          try { handler(entry); } catch (_) { /* one entry, one skip */ }
        }
      });
      po.observe({ type: type, buffered: true });
    } catch (_) { /* this entry type is unavailable here */ }
  }

  observe('layout-shift', function (entry) {
    // hadRecentInput: a shift the user caused by scrolling or clicking is
    // excluded from the Layout Instability score by the API's own rule.
    // Reporting it would make every interaction read as a defect.
    if (entry.hadRecentInput) return;
    const sources = entry.sources || [];
    // One shift's `sources` name SEVERAL moved nodes, and the first does
    // not always resolve: #643's production shifts reported `other`
    // because sources[0] was a bare section div whose panel moved with
    // it. Walk every source and take the first attribution that names a
    // region, so a shift lands `other` only when NO source sits inside
    // one. Sources may be detached (the re-render that moved them has
    // already run); the walk reads parentElement, which a detached tree
    // still carries.
    let where = 'other';
    for (let i = 0; i < sources.length && where === 'other'; i++) {
      where = region(sources[i] && sources[i].node);
    }
    push({
      metric: 'layout_shift', part: 'shift', value: entry.value,
      region: where,
      phase: phaseNow(),
    });
  });

  observe('longtask', function (entry) {
    // No region: the sink's table forbids one on a long task.
    push({ metric: 'longtask', part: 'block', value: entry.duration,
           phase: phaseNow() });
  });

  // --- install -------------------------------------------------------

  function onHidden() { flush(); }
  try {
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', function () {
        try {
          if (document.visibilityState === 'hidden') onHidden();
        } catch (_) { onHidden(); }
      });
    }
    if (typeof window !== 'undefined' && window.addEventListener) {
      // A bfcache eviction fires pagehide, not visibilitychange.
      window.addEventListener('pagehide', onHidden);
    }
  } catch (_) { /* no lifecycle events: the timer still flushes */ }

  adoptSigninStart();

  window.perf = {
    openJourney: function (name) {
      try { openJourney(name); } catch (_) {}
    },
    closeFetch: function (name) {
      try { closeFetch(name); } catch (_) {}
    },
    closeJourney: function (name) {
      try { closeJourney(name); } catch (_) {}
    },
    markUsable: function () {
      try { markUsable(); } catch (_) {}
    },
    sseUpdate: function () {
      try { sseUpdate(); } catch (_) {}
    },
    phase: function () {
      try { return phaseNow(); } catch (_) { return 'other'; }
    },
    region: function (node) {
      try { return region(node); } catch (_) { return 'other'; }
    },
  };
})();