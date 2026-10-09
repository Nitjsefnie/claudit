// Claude usage normalization shared by the browser parser.
// Mirrors backend/parse.py's _flatten_usage and _merge_usage_max.
window.parserUsage = (() => {
  function asRecord(value) {
    return value && typeof value === 'object' && !Array.isArray(value)
      ? value : {};
  }

  function flattenUsage(usage) {
    if (!usage || typeof usage !== 'object' || Array.isArray(usage)) {
      return usage;
    }
    const rawIterations = usage.iterations;
    if (!Array.isArray(rawIterations) || rawIterations.length === 0) {
      return usage;
    }
    const iterations = rawIterations.filter((item) => item
      && typeof item === 'object' && !Array.isArray(item));
    if (iterations.length === 0) return usage;

    const out = { ...usage };
    for (const key of [
      'input_tokens', 'cache_creation_input_tokens',
      'cache_read_input_tokens', 'output_tokens',
    ]) {
      out[key] = iterations.reduce(
        (total, iteration) => total + (iteration[key] || 0), 0);
    }
    for (const key of ['cache_creation', 'server_tool_use']) {
      const merged = {};
      for (const iteration of iterations) {
        for (const [name, value] of Object.entries(asRecord(iteration[key]))) {
          if (!Number.isInteger(value) && typeof value !== 'boolean') continue;
          merged[name] = (merged[name] || 0) + Number(value);
        }
      }
      if (Object.keys(merged).length) out[key] = merged;
    }
    return out;
  }

  function mergeUsageMax(existing, incoming) {
    if (existing == null) return incoming;
    if (incoming == null) return existing;
    if (typeof existing === 'number' && typeof incoming === 'number') {
      return Math.max(existing, incoming);
    }
    if (typeof existing === 'object' && typeof incoming === 'object'
        && !Array.isArray(existing) && !Array.isArray(incoming)) {
      const out = { ...existing };
      for (const key of Object.keys(incoming)) {
        out[key] = (key in out)
          ? mergeUsageMax(out[key], incoming[key]) : incoming[key];
      }
      return out;
    }
    return existing;
  }

  function searchRequests(usage) {
    const iterations = Array.isArray(usage.iterations)
      ? usage.iterations.filter((item) => item && typeof item === 'object'
        && !Array.isArray(item)) : [];
    let nestedServerUsage = false;
    let total = 0;
    let found = false;
    for (const source of iterations) {
      const server = source.server_tool_use;
      if (!server || typeof server !== 'object' || Array.isArray(server)) {
        continue;
      }
      for (const [key, value] of Object.entries(server)) {
        if (!Number.isInteger(value) && typeof value !== 'boolean') continue;
        nestedServerUsage = true;
        if (key === 'web_search_requests') {
          total += typeof value === 'boolean' ? Number(value) : value;
          found = true;
        }
      }
    }
    if (iterations.length && nestedServerUsage) {
      return found && total >= 0 ? total : null;
    }
    const server = usage.server_tool_use;
    const count = server && typeof server === 'object'
      ? server.web_search_requests : null;
    if (Number.isInteger(count) && count >= 0) return count;
    return found ? total : null;
  }

  return { flattenUsage, mergeUsageMax, searchRequests };
})();
