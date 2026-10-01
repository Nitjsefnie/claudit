// Which Overview panels have anything to draw, moved out of app.jsx so
// that file does not grow (issue #436). No React and no fetch glue: each
// is a plain function of its arguments, so it belongs here rather than
// in the shell that happens to call it today.
//
// Relocation only -- each function is the one app.jsx already held,
// behaviour for behaviour; only the surrounding spacing differs. The
// call sites stay in app.jsx and read the globals below.
(function () {
  // Which of the four token panels to draw.
  //
  // A panel whose series is zero in every bucket is noise: it occupies a
  // grid cell to say nothing. The backend already decided this and sent
  // `token_types`, so mirror that when it is present rather than
  // second-guessing it; the synthetic preview has no such list, so fall
  // back to summing the events.
  //
  // Cache Create is the one panel that is not 1:1 with a wire field — it
  // plots cache_5m + cache_1h — so it survives if EITHER half did.
  function tokenPanels(events, tokenTypes) {
    const live = tokenTypes
      ? new Set(tokenTypes)
      : new Set(['input_tokens', 'output_tokens', 'thinking_tokens',
                 'cache_5m_tokens', 'cache_1h_tokens', 'cache_read_tokens']
        .filter(f => {
          const key = { input_tokens: 'input_tokens', output_tokens: 'output_tokens',
                        thinking_tokens: 'thinking_tokens',
                        cache_5m_tokens: 'ephemeral_5m', cache_1h_tokens: 'ephemeral_1h',
                        cache_read_tokens: 'cache_read' }[f];
          return events.some(e => (e[key] || 0) !== 0);
        }));
    return {
      input: live.has('input_tokens'),
      output: live.has('output_tokens'),
      // Subset of output: its own panel, never part of `any` arithmetic
      // beyond deciding whether to draw it.
      thinking: live.has('thinking_tokens'),
      cacheCreate: live.has('cache_5m_tokens') || live.has('cache_1h_tokens'),
      cacheRead: live.has('cache_read_tokens'),
      any: live.size > 0,
    };
  }

  // Non-token series (churn, cost) carry no backend declaration, so the
  // same "all zero across the range" rule is applied here. A project that
  // never edits a file otherwise gets two permanently flat churn panels.
  function hasSeries(events, key) {
    return events.some(e => (e[key] || 0) !== 0);
  }

  window.tokenPanels = tokenPanels;
  window.hasSeries = hasSeries;
})();