// The browser's rate resolution — the pricing half of src/parser.js,
// moved out so the parser file stays under its committed size entry (the
// ratchet's "code moves out first"). Holds resolution logic only and reads
// the tables of src/pricing.json (SV-RATE-DATA) through window.modelRates
// and its siblings, which src/pricing-loader.js loads and validates before
// this script runs: the tier-fallback table below is built from
// window.modelRates at load time. resolveModelRate is pricing.resolve's
// mirror — 'exact' | 'tier' | 'default', anything but exact an estimate —
// and rateForModel its rates-only shorthand. computeSessionStats
// (parser.js) and the Token Breakdown panel (app.jsx, token-breakdown.js)
// price every record through them, exactly as pricing.compute_cost does,
// so the Inspector and the database agree.

// Every rate lives in src/pricing.json (SV-RATE-DATA), the same file
// backend/pricing.py loads; this file holds resolution logic only. The
// rate tables are read and validated by src/pricing-loader.js, which runs
// before this script and exposes window.modelRates and its siblings (fees
// included, issue #469), window.FREE_RATES and window.scheduleRatesAt.

// Family fallbacks for unrecognised Claude models — current-generation
// list rates for the tier, never a dated promotion. The generation is the
// highest-versioned key of the family in the MERGED view (models-table
// keys and tracked vendor bare keys — the claude families live in the
// tracked table since the vendor migration), so a new model row moves its
// family's fallback with no second edit. Ties keep table order. Mirrors
// pricing._latest.
const _VERSIONED_KEY = /^claude-([a-z]+)-(\d+(?:-\d+)*)$/;
const _latestKey = (families) => {
  let best = null;
  let bestVersion = null;
  for (const key of [...Object.keys(window.modelRates),
                     ...Object.keys(window.vendorBare)]) {
    const m = _VERSIONED_KEY.exec(key);
    if (!m || !families.includes(m[1])) continue;
    const version = m[2].split('-').map(Number);
    let cmp = 0;
    for (let i = 0; i < Math.max(version.length, bestVersion ? bestVersion.length : 0) && !cmp; i++) {
      cmp = (version[i] || 0) - ((bestVersion && bestVersion[i]) || 0);
    }
    if (best === null || cmp > 0) { best = key; bestVersion = version; }
  }
  return best;
};
const _TIER_FALLBACKS = [
  [/fable|mythos/, _latestKey(['fable', 'mythos'])],
  [/opus/, _latestKey(['opus'])],
  [/sonnet/, _latestKey(['sonnet'])],
  [/haiku/, _latestKey(['haiku'])],
];
// A dated snapshot suffix ('-20250514') is the same model; a short version
// suffix ('-9') or mode suffix ('-fast') is a DIFFERENT model.
const _SNAPSHOT_SUFFIX = /^-?\d{6,8}$/;

function _normaliseModel(model) {
  let m = String(model || '').trim().toLowerCase();
  if (!m) return '';
  const i = m.indexOf('claude');   // strip provider/region routing prefix
  if (i > 0) m = m.slice(i);
  return m.replace(/\./g, '-');
}

// OpenRouter's :free tier and stealth/ preview models are $0; matched on
// the id's shape (the list churns weekly), on the raw id AND the
// normalised one, because _normaliseModel strips everything before
// 'claude' — a 'stealth/claude-…' id loses its prefix there. Mirrors
// backend/pricing.py `_is_free` (SV-PARSER-SPEC).
function _isFreeModel(model, norm) {
  const raw = String(model || '').trim().toLowerCase();
  return raw.endsWith(':free') || raw.startsWith('stealth/')
      || norm.endsWith(':free') || norm.startsWith('stealth/');
}

// The LONGEST key norm names, whatever the table order. Mirrors
// pricing._match_key.
function _matchRateKey(norm) {
  let best = null;
  for (const k of Object.keys(window.modelRates)) {
    if (!norm.startsWith(k) || (best && k.length <= best.length)) continue;
    const rest = norm.slice(k.length);
    if (rest === '' || rest[0] === '[' || rest[0] === '@' || _SNAPSHOT_SUFFIX.test(rest)) best = k;
  }
  return best;
}

// The tracked key whose vendor bare form norm names, by the models
// table's own longest-match and suffix rules (empty rest, '[', '@', a
// snapshot suffix). An id spelled WITH the vendor prefix
// ('z-ai/glm-5-3') matches no bare form: it names the OpenRouter catalog
// model, not the first-party id, and keeps pricing default. Mirrors
// pricing._vendor_match.
function _matchVendorKey(norm) {
  let best = null;
  for (const form of Object.keys(window.vendorBare)) {
    if (!norm.startsWith(form) || (best && form.length <= best.length)) continue;
    const rest = norm.slice(form.length);
    if (rest === '' || rest[0] === '[' || rest[0] === '@' || _SNAPSHOT_SUFFIX.test(rest)) {
      best = form;
    }
  }
  return best === null ? null : window.vendorBare[best];
}

function _toMillis(ts) {
  if (ts == null) return null;
  const t = typeof ts === 'number' ? ts : Date.parse(ts);
  return Number.isNaN(t) ? null : t;
}

function _inWindow(windows, ts, listRates) {
  const t = _toMillis(ts);
  if (windows && t != null) {
    for (const w of windows) {
      if (t < w.endExclusive) return w.rates;
    }
  }
  return listRates;
}

// OpenRouter's dated permaslug ('deepseek/deepseek-v4-flash-20260731') is
// the same model as its slug ('deepseek/deepseek-v4-flash-0731'), and a
// variant suffix (':nitro', ':floor') names a service tier, not a price:
// the tiered id resolves to the bare model's row. Exact match only, never
// _SNAPSHOT_SUFFIX: that would read the permaslug as the UNDATED model.
// Only ':free' changes price (zero), and resolveModelRate prices it
// before this lookup — it never folds. The exact id is tried first, so a
// table row spelled with the suffix still wins over the fold. A row that
// begins at a time does not exist for a record before it. Mirrors
// pricing._provider_key.
const _PERMASLUG_DATE = /-20\d{2}(\d{4})$/;
const _VARIANT_SUFFIX = /:([^:]*)$/;
function _providerModelKey(norm, provider, ts) {
  const t = _toMillis(ts);
  const variant = _VARIANT_SUFFIX.exec(norm);
  const bare = variant && variant[1].toLowerCase() !== 'free'
    ? norm.slice(0, variant.index) : norm;
  for (const m of [norm, norm.replace(_PERMASLUG_DATE, '-$1'), bare]) {
    const hosts = window.providerRates[m];
    if (hosts && Object.prototype.hasOwnProperty.call(hosts, provider)) {
      const start = (window.providerStarts[m] || {})[provider];
      return start !== undefined && t != null && t < start ? null : m;
    }
  }
  return null;
}

// The per-request fee of a row's history entry in force, keyed by entry
// index exactly like the schedules (windows[i] is entry i, the tail entry
// is index windows.length; the list price when ts is null). Mirrors
// pricing._fee_at.
function _feeAt(fees, windows, t) {
  if (!fees) return 0;
  const key = t == null ? (windows ? windows.length : 0) : (windows || []).filter((w) => w.endExclusive <= t).length;
  return fees[key] || 0;
}

// Resolve a model id to rates, reporting how confident the match is:
// 'exact' | 'tier' | 'default'. Anything but 'exact' is an estimate.
// `provider` is the record's serving host: a (model, provider) row wins,
// otherwise (and always without one) the model alone decides. `fee` is
// the serving host's per-request fee in force (issue #469), folded into
// each record's cost beside the tokens.
window.resolveModelRate = function resolveModelRate(model, ts, provider) {
  const norm = _normaliseModel(model);
  if (_isFreeModel(model, norm)) {
    return { rates: window.FREE_RATES, kind: 'exact', key: norm, fee: 0 };
  }
  const pkey = provider ? _providerModelKey(norm, provider, ts) : null;
  if (pkey) {
    const windows = (window.providerDatedRates[pkey] || {})[provider];
    const rates = _inWindow(windows, ts, window.providerRates[pkey][provider]);
    const t = _toMillis(ts);
    const entry = (windows || []).filter((w) => w.endExclusive <= t).length;
    const schedule = t == null ? null : ((window.providerSchedules[pkey] || {})[provider] || {})[entry];
    const fee = _feeAt(((window.providerFees[pkey] || {})[provider]), windows, t);
    if (!schedule) return { rates, kind: 'exact', key: pkey, fee };
    return { rates: window.scheduleRatesAt(schedule, t) || rates, kind: 'exact', key: pkey, fee };
  }
  const key = _matchRateKey(norm);
  if (key) {
    const windows = window.datedRates[key];
    const t = _toMillis(ts);
    return { rates: _inWindow(windows, ts, window.modelRates[key]),
             kind: 'exact', key,
             fee: _feeAt(window.modelFees[key], windows, t) };
  }
  // The vendor bare path: the tracked key the bare form names prices the
  // id from its own (tracked key, host) row's dated windows, fee-free and
  // schedule-free — a host's fee and time-of-day windows are the host's
  // own terms for requests THROUGH it, and a bare id names no host. A row
  // that begins at a time does not exist for a record before it: fall
  // through, exactly as a rowless model does. Mirrors the vendor branch
  // of pricing.resolve.
  const vkey = _matchVendorKey(norm);
  if (vkey) {
    const host = window.vendorHosts[vkey];
    const hosts = window.providerRates[vkey] || {};
    if (Object.prototype.hasOwnProperty.call(hosts, host)) {
      const start = (window.providerStarts[vkey] || {})[host];
      const t = _toMillis(ts);
      if (!(start !== undefined && t != null && t < start)) {
        const windows = (window.providerDatedRates[vkey] || {})[host];
        return { rates: _inWindow(windows, ts, hosts[host]),
                 kind: 'exact', key: vkey, fee: 0 };
      }
    }
  }
  for (const [re, tierKey] of _TIER_FALLBACKS) {
    if (re.test(norm)) return { rates: window.keyListRates(tierKey), kind: 'tier', key: null, fee: 0 };
  }
  return { rates: window.keyListRates('claude-opus-4-7'), kind: 'default', key: null, fee: 0 };
};

window.rateForModel = function rateForModel(model, ts, provider) {
  return window.resolveModelRate(model, ts, provider).rates;
};

// ─── the long-context meter (issue #765) ────────────────────────────────
// Per-model membership and thresholds: pricing.json's long_context_models
// and long_context_meters, both set by pricing-loader.js; a member without
// an entry keeps the meter's global default (window.LONG_CONTEXT_THRESHOLD,
// parser-lanes.js, whose longContextThresholdFor is the lane seam). Mirrors
// pricing.meter_flag, normalising the way resolveModelRate normalises.
window.longContextFlagFor = function longContextFlagFor(model, usage) {
  const key = _normaliseModel(model);
  if (!(window.longContextModels || []).includes(key)) return null;
  const u = usage || {};
  const inWindow = (u.input_tokens || 0) + (u.cache_creation_input_tokens || 0)
      + (u.cache_read_input_tokens || 0);
  return inWindow > window.longContextThresholdFor(model);
};
