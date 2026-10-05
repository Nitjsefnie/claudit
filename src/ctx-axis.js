// The context-size axis rule, shared by every panel that draws context.
//
// Issue #648. The axis used to be a hand-maintained per-model cap table
// (MODEL_CAPS + capForModel, issue #630 moved it here) that named ten
// Claude models and fell back to a name rule for the rest: `gpt*` got
// 272k -- a BILLING threshold, not a window -- `kimi*` got 256k, a name
// containing opus or fable got 1M, everything else 200k. The table was
// stale for every model added after it was written, so a model with a
// larger window drew a cap line below its own data, and the grid and
// the comparison disagreed about a given model's height because they
// scaled against different constants.
//
// The panels now scale from the OBSERVED data, through ONE rule:
//
//   axis top = peak of what is drawn, times HEADROOM
//
// `peak of what is drawn` is the largest context value among the turns
// on the chart, so the tallest point is HEADROOM-1 of the way up the
// plot and nothing is drawn above the axis. Each panel finds that peak
// with the helper that matches the shape it already holds -- ctxPeak for
// the two dashboard panels, which both receive a model's sessions, and a
// plain Math.max over a row's ctx in the per-session view -- and then
// hands it to the SAME ctxAxisTop. That is what makes one model's median
// curve sit at the same height in the grid and in the comparison.
//
// The billing long-context tier is a different thing and lives in
// parser-lanes.js (`window.LONG_CONTEXT_THRESHOLD`); nothing here reads
// or derives it. This module holds no per-model knowledge at all, so a
// model this file has never heard of scales like every other one.
//
// Plain JS, no React, so the arithmetic is drivable from node without a
// browser -- the panels themselves are JSX and node parses none of them.
(function () {
  // How far above the observed peak the axis ends. 1.1 matches what the
  // per-session view already used for its own headroom, so a session's
  // curve keeps the height it had wherever the cap line used to be.
  const HEADROOM = 1.1;

  // The axis top when there is nothing to draw: an all-zero or empty
  // peak would make ctxAxisTop return 0 and every y scale divide by it.
  // This is a division guard, NOT a context window -- it is never drawn
  // as a line and never labelled, and it only binds below HEADROOM-1 of
  // it (about 909 tokens).
  const MIN_TOP = 1000;

  // The largest context among every turn of every session, or 0 when
  // there are none. `sessions` is the shape both dashboard panels are
  // handed for one model: [{ seq: [{ ctx, t, ... }] }].
  function ctxPeak(sessions) {
    let peak = 0;
    for (const s of sessions || []) {
      for (const p of s.seq || []) {
        if (typeof p.ctx === 'number' && p.ctx > peak) peak = p.ctx;
      }
    }
    return peak;
  }

  // The one rule: the observed peak, with headroom, floored so the axis
  // is always a positive number a y scale can divide by.
  function ctxAxisTop(peak) {
    const p = typeof peak === 'number' && peak > 0 ? peak : 0;
    return Math.max(p * HEADROOM, MIN_TOP);
  }

  // ctxAxisTop over a list of session bundles — the peak of all of them.
  //
  // This is what makes the two dashboard panels agree rather than merely
  // resemble each other. The grid passes ONE model's list; the
  // comparison passes every CHECKED model's. Both call this, so with a
  // single model checked the two expressions are the same computation on
  // the same data and return the same axis, and that model's median sits
  // at the same height in both views. Sharing ctxPeak alone would not
  // have bought that: the grid used to fold its own peak loop and the
  // comparison used a p90, so the two axes could not be compared at all.
  function ctxAxisTopFor(sessionLists) {
    let peak = 0;
    for (const sessions of sessionLists || []) {
      const p = ctxPeak(sessions);
      if (p > peak) peak = p;
    }
    return ctxAxisTop(peak);
  }

  // Round tick values from 0 up to (not past) `top`, on the same
  // 1/2/5-per-decade step every one of these panels already used.
  function ctxAxisTicks(top, n) {
    if (!(top > 0)) return [0];
    const want = n > 0 ? n : 4;
    const step0 = top / want;
    const exp = Math.pow(10, Math.floor(Math.log10(step0)));
    const norm = step0 / exp;
    const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * exp;
    const ticks = [];
    for (let v = 0; v <= top; v += step) ticks.push(v);
    return ticks.length ? ticks : [0];
  }

  window.ctxAxis = { HEADROOM, MIN_TOP, ctxPeak, ctxAxisTop, ctxAxisTopFor,
                     ctxAxisTicks };
})();
