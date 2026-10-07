// Per-call context-window size. Mirrors backend/ctx_input.py.
// When usage.iterations has >1 entries (advisor()/sub-agent fan-out), the
// top-level fresh+create+read is the BILLING sum across iterations, not
// the peak single-call window.
// For context-growth views we want the peak: max-of-iteration-totals.
// Exposed at top level (not inside parseTranscript) so the lane delegation
// path, which returns before this body runs, still exposes it.
function usageCtxInput(u) {
  if (!u) return 0;
  const iters = u.iterations;
  if (Array.isArray(iters) && iters.length > 1) {
    let peak = 0;
    for (const it of iters) {
      const t = (it.input_tokens || 0)
              + (it.cache_creation_input_tokens || 0)
              + (it.cache_read_input_tokens || 0);
      if (t > peak) peak = t;
    }
    return peak;
  }
  return (u.input_tokens || 0)
       + (u.cache_creation_input_tokens || 0)
       + (u.cache_read_input_tokens || 0);
}
window.usageCtxInput = usageCtxInput;
