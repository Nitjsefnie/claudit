// The Tool Error Rate panel's numeric core, as plain JS (issue #652).
//
// The rate sequences and their EMA are the part of the panel a test can
// reach without a browser — the JSX around them is not — so the math
// lives here, node-runnable, and the panel calls it. Everything below
// works on one model's grouped data, the shape the panel's `byModel`
// memo builds:
//
//   { buckets: sorted bucket timestamps (ms),
//     perBucketTool: Map<ts, Map<tool, {n_total, n_error}>>,
//     totalsByTool: Map<tool, n_total> }
(function () {
  // Bucket-wise rate sequences per series key: the aggregate over ALL
  // tools under '__AGG__', one entry per visible tool, and the fold of
  // `otherTools` under `otherKey` (skipped when otherTools is empty).
  // Only non-sparse buckets are kept — a bucket with no settled calls
  // for the series has no rate.
  function buildModelSeries(md, visibleTools, otherTools, otherKey) {
    const perKey = new Map();
    const agg = [];
    for (const ts of md.buckets) {
      const bucket = md.perBucketTool.get(ts);
      let aT = 0, aE = 0;
      for (const v of bucket.values()) { aT += v.n_total; aE += v.n_error; }
      if (aT > 0) agg.push({ t_ms: ts, rate: aE / aT, n_total: aT, n_error: aE });
    }
    perKey.set('__AGG__', agg);
    for (const tool of visibleTools) {
      const arr = [];
      for (const ts of md.buckets) {
        const v = md.perBucketTool.get(ts).get(tool);
        if (v && v.n_total > 0) {
          arr.push({ t_ms: ts, rate: v.n_error / v.n_total,
                     n_total: v.n_total, n_error: v.n_error });
        }
      }
      perKey.set(tool, arr);
    }
    if (otherTools && otherTools.length) {
      const arr = [];
      for (const ts of md.buckets) {
        const bucket = md.perBucketTool.get(ts);
        let oT = 0, oE = 0;
        for (const tool of otherTools) {
          const v = bucket.get(tool);
          if (v) { oT += v.n_total; oE += v.n_error; }
        }
        if (oT > 0) arr.push({ t_ms: ts, rate: oE / oT, n_total: oT, n_error: oE });
      }
      perKey.set(otherKey, arr);
    }
    return perKey;
  }

  // Exponential moving average over each sequence in place, first point
  // carried (no warm-up): ema[0] = rate[0], then
  // ema[i] = a * rate[i] + (1 - a) * ema[i-1].
  function emaSeries(perKey, alpha) {
    for (const [k, arr] of perKey) {
      if (!arr.length) continue;
      let prev = arr[0].rate;
      for (let i = 0; i < arr.length; i++) {
        prev = i === 0 ? arr[0].rate : alpha * arr[i].rate + (1 - alpha) * prev;
        arr[i] = { ...arr[i], ema: prev };
      }
      perKey.set(k, arr);
    }
    return perKey;
  }

  // The panel's `drawn` memo, hoisted here so the rate logic stays
  // node-testable and the JSX stays layout-only (#690 moved it: the
  // panel file sat one line under its size ceiling and an outgrown
  // rework moves code into a new module).
  function drawnLines(models, selModels, byModel, visibleTools,
      otherTools, otherKey) {
    const out = [];
    for (const m of models) {
      if (!selModels.has(m.model)) continue;
      const perKey = buildModelSeries(
        byModel[m.model], visibleTools, otherTools, otherKey);
      emaSeries(perKey, 0.15);
      out.push({ model: m.model, perKey });
    }
    return out;
  }

  window.rateSeries = { buildModelSeries, emaSeries, drawnLines };
})();
