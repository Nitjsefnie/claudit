// Per-call context-window size. Mirrors backend/ctx_input.py.
// When usage.iterations has >1 entries (advisor()/sub-agent fan-out), the
// top-level fresh+create+read is the BILLING sum across iterations, not
// the peak single-call window.
// For context-growth views we want the peak: max-of-iteration-totals.
// Lives in its own file (loaded by public/index.html) so every consumer —
// the dashboard projection and the Inspector's turn stats — reaches it
// regardless of which parse path produced the trace.
function usageCtxInput(u) {
  if (!u) return 0;
  const raw = u.iterations;
  // Mirror backend json_shape.dict_list: only object entries count, so a
  // malformed entry (null, array, scalar) is skipped rather than thrown on.
  const iters = Array.isArray(raw)
    ? raw.filter((it) => it && typeof it === 'object' && !Array.isArray(it))
    : [];
  if (iters.length > 1) {
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
