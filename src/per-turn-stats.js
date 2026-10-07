// Per-turn percentile stats for the Per-Session Context Growth
// comparison (issue #644: extracted verbatim from
// dashboard-charts-extra.jsx, which outgrew its size budget — no code
// changed, not even a name). Plain JS so node can execute it.
//
// `sessions` is a list of {seq: [{t, ctx}, ...]} traces; the result
// carries, per turn index from 0 to the largest observed, the sorted
// nearest-rank median/p25/p75/p90 over every trace's ctx at that turn
// (`arr[Math.floor(n * q)]`, median `arr[Math.floor(n / 2)]`) and the
// count of traces reaching it. Turn indexes at or beyond CTX_TURN_CAP
// end a trace's contribution.
(function () {
  const CTX_TURN_CAP = Infinity;

  function perTurnStats(sessions) {
    const empty = { turns: [], median: [], p25: [], p75: [], p90: [], count: [], maxT: 0 };
    if (!sessions || !sessions.length) return empty;
    const byTurn = new Map();
    for (const s of sessions) {
      for (const p of s.seq) {
        if (p.t >= CTX_TURN_CAP) break;
        if (!byTurn.has(p.t)) byTurn.set(p.t, []);
        byTurn.get(p.t).push(p.ctx);
      }
    }
    if (!byTurn.size) return empty;
    const maxT = Math.max(...byTurn.keys());
    const turns = [], median = [], p25 = [], p75 = [], p90 = [], count = [];
    const pick = (arr, q) => arr[Math.min(arr.length - 1, Math.floor(arr.length * q))];
    for (let t = 0; t <= maxT; t++) {
      const vals = byTurn.get(t);
      if (!vals || vals.length < 1) {
        turns.push(t);
        median.push(null); p25.push(null); p75.push(null); p90.push(null);
        count.push(0);
        continue;
      }
      vals.sort((a, b) => a - b);
      turns.push(t);
      median.push(vals[Math.floor(vals.length / 2)]);
      p25.push(pick(vals, 0.25));
      p75.push(pick(vals, 0.75));
      p90.push(pick(vals, 0.9));
      count.push(vals.length);
    }
    return { turns, median, p25, p75, p90, count, maxT };
  }

  window.perTurnStats = perTurnStats;
})();
