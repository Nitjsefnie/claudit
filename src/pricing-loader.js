// The pricing.json loader (SV-RATE-DATA): the browser half of
// backend/pricing_load.py. Reads src/pricing.json synchronously —
// window.modelRates and the other derived tables below exist the moment
// this script has run, so the app prices during render with no hook to
// await a load — and refuses a rule-breaking file, naming the row: with
// no valid rates there is no honest price, so nothing resolves over
// nothing. Node (the test suite) reads the file beside this module; the
// browser fetches the URL the page names on this script tag (data-pricing,
// which the backend cache-busts like every /src asset), else the file
// beside this script, and revalidates either way.
//
// Exposes the derived tables (modelRates, datedRates, modelFees,
// providerRates, providerDatedRates, providerStarts, providerSchedules,
// providerFees, rateEpochs, FREE_RATES) and scheduleRatesAt, the weekly
// schedule-window lookup the resolver in src/rates.js uses. Fees (issue
// #469) ride the same tables, parsed from the entry notes exactly as the
// backend loader parses them.

// Read synchronously, so window.rateForModel works the moment this script
// has run — the app prices during render with no hook to await a load.
// Node (the test suite) reads the file beside this module. The browser
// fetches the URL the page names on this script tag (data-pricing, which
// the backend cache-busts like every /src asset), else the file beside this
// script, and revalidates either way.
//
// Any failure throws naming pricing.json: with no valid rates there is no
// honest price, so the resolver is never defined over nothing.
const _pricingError = (detail) => new Error(`pricing.json: ${detail}`);


// The schedule spelling scanner lives in src/hhmm-spelling.js — the
// browser loads it ahead of this script (index.html's tag order) and node
// requires it from here.
/* eslint-disable no-undef */
const _checkHhmmSpelling =
  typeof module !== 'undefined' && typeof module.exports !== 'undefined'
    ? require('./hhmm-spelling.js').checkHhmmSpelling
    : window.checkHhmmSpelling;
/* eslint-enable no-undef */

function _readPricing() {
  if (typeof document === 'undefined') {
    let text;
    try {
      /* eslint-disable no-undef */
      text = require('fs').readFileSync(
        require('path').join(__dirname, 'pricing.json'), 'utf8');
      /* eslint-enable no-undef */
    } catch (e) {
      throw _pricingError(e.message);
    }
    _checkHhmmSpelling(text);
    try {
      return JSON.parse(text);
    } catch (e) {
      throw _pricingError(e.message);
    }
  }
  const script = document.currentScript;
  const url = new URL(script.dataset.pricing || 'pricing.json', script.src).href;
  const xhr = new XMLHttpRequest();
  xhr.open('GET', url, false);
  xhr.setRequestHeader('Cache-Control', 'no-cache');
  xhr.send();
  if (xhr.status !== 200) throw _pricingError(`HTTP ${xhr.status} from ${url}`);
  // Signed out, the request is redirected to the sign-in page, which is a 200.
  if (xhr.responseURL !== url) throw _pricingError(`redirected to ${xhr.responseURL}`);
  _checkHhmmSpelling(xhr.responseText);
  try {
    return JSON.parse(xhr.responseText);
  } catch (e) {
    throw _pricingError(`not JSON (${e.message})`);
  }
}

const _RATE_FIELDS = { fresh: 'fresh', c5: 'create_5m', c1h: 'create_1h', read: 'read', out: 'output' };
const _FIELD_NAMES = Object.values(_RATE_FIELDS).sort().join();
const _ratesOf = (entry) => Object.fromEntries(
  Object.entries(_RATE_FIELDS).map(([js, field]) => [js, entry[field]]));
// The one timestamp spelling both loaders accept, every field in range.
// Mirrors pricing._INSTANT. The instant is computed here, never by
// Date.parse, which rolls 24:00 and 02-30 over where Python refuses them.
const _INSTANT = /^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:Z|([+-])([0-9]{2}):([0-9]{2}))$/;
function _instantMs(stamp) {
  const m = typeof stamp === 'string' ? _INSTANT.exec(stamp) : null;
  if (!m) return NaN;
  const [y, mo, d, h, mi, s] = m.slice(1, 7).map(Number);
  const [oh, om] = [Number(m[8] || 0), Number(m[9] || 0)];
  const wall = new Date(0);
  wall.setUTCFullYear(y, mo - 1, d);
  wall.setUTCHours(h, mi, s, 0);
  const inRange = y >= 1 && oh < 24 && om < 60
    && wall.getUTCFullYear() === y && wall.getUTCMonth() === mo - 1 && wall.getUTCDate() === d
    && wall.getUTCHours() === h && wall.getUTCMinutes() === mi && wall.getUTCSeconds() === s;
  return inRange ? wall.getTime() - (m[7] === '-' ? -1 : 1) * (oh * 60 + om) * 60000 : NaN;
}
const _isRate = (v) => typeof v === 'number' && Number.isFinite(v) && v >= 0;

// Refuses what pricing._history refuses, naming the row the same way.
// A provider entry's weekly UTC schedule, checked and read into windows of
// { days (Set or null), start, end (HHMM or null), rates }. Mirrors
// pricing._schedule, whose docstring states the rules.
const _DAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];
const _isHhmm = (v) => Number.isInteger(v) && v >= 0 && v <= 2359 && v % 100 < 60;
function _checkSchedule(schedule, at) {
  if (!Array.isArray(schedule) || !schedule.length) {
    throw _pricingError(`${at}: schedule is not a non-empty list of windows`);
  }
  return schedule.map((window, j) => {
    const w = `${at}.schedule[${j}]`;
    if (window === null || typeof window !== 'object' || Array.isArray(window) || !('rates' in window)
        || Object.keys(window).some((k) => !['days', 'start', 'end', 'rates'].includes(k))) {
      throw _pricingError(`${w}: a window is {days?, start?, end?, rates}`);
    }
    const { rates, days } = window;
    if (rates === null || typeof rates !== 'object'
        || Object.keys(rates).sort().join() !== _FIELD_NAMES
        || !Object.values(_RATE_FIELDS).every((f) => _isRate(rates[f]))) {
      throw _pricingError(`${w}: rates are not the five finite non-negative rates`);
    }
    if (days !== undefined && !(Array.isArray(days) && days.length
        && days.every((d) => _DAYS.includes(d)) && new Set(days).size === days.length)) {
      throw _pricingError(`${w}: days ${JSON.stringify(days)} are not distinct weekday names`);
    }
    if (('start' in window) !== ('end' in window)) {
      throw _pricingError(`${w}: start and end come together`);
    }
    if ('start' in window && !(_isHhmm(window.start) && _isHhmm(window.end))) {
      throw _pricingError(`${w}: start and end are not HHMM times from 0 to 2359`);
    }
    if ('start' in window && window.start === window.end) {
      throw _pricingError(`${w}: start equals end`);
    }
    return { days: days ? new Set(days) : null,
             start: 'start' in window ? window.start : null,
             end: 'end' in window ? window.end : null, rates: _ratesOf(rates) };
  });
}

// The first window a millisecond instant falls in, by UTC weekday and
// HHMM, or null. Mirrors pricing._scheduled.
function _scheduledRates(schedule, t) {
  const at = new Date(t);
  const day = _DAYS[(at.getUTCDay() + 6) % 7];
  const hhmm = at.getUTCHours() * 100 + at.getUTCMinutes();
  for (const { days, start, end, rates } of schedule) {
    if (days && !days.has(day)) continue;
    if (start === null || (start < end ? start <= hhmm && hhmm < end : hhmm >= start || hhmm < end)) {
      return rates;
    }
  }
  return null;
}

// An entry's oscillating price range, checked and read as {[field]: [min, max]}.
// A band maps each rate field it constrains to a [min, max] pair of finite
// non-negative numbers with min <= max; a field it does not name is
// unconstrained. The five rate fields beside it stay the priced rates — the
// loader reads a band only to refuse a rule-breaking one. Mirrors
// pricing.check_band.
function _checkBand(band, at) {
  if (band === null || typeof band !== 'object' || Array.isArray(band)) {
    throw _pricingError(`${at}: band is not a mapping of rate fields to [min, max]`);
  }
  const out = {};
  for (const [field, span] of Object.entries(band)) {
    const w = `${at}.band[${field}]`;
    if (!Object.values(_RATE_FIELDS).includes(field)) {
      throw _pricingError(`${w}: not one of ${Object.values(_RATE_FIELDS).join(', ')}`);
    }
    if (!Array.isArray(span) || span.length !== 2
        || !span.every((v) => _isRate(v)) || span[0] > span[1]) {
      throw _pricingError(`${w}: not a [min, max] pair of finite non-negative numbers with min <= max`);
    }
    out[field] = [span[0], span[1]];
  }
  return out;
}

function _checkHistory(entries, where, mayBegin) {
  if (!entries.length) throw _pricingError(`${where}: empty history`);
  let previous = null;
  const schedules = {};
  const fees = {};
  entries.forEach((entry, i) => {
    const at = `${where}[${i}]`;
    const fields = Object.keys(entry).filter((k) => !['from', 'note', 'schedule', 'band'].includes(k));
    if (fields.sort().join() !== _FIELD_NAMES || !('from' in entry)) {
      throw _pricingError(`${at}: fields ${Object.keys(entry).sort()}`);
    }
    if ('schedule' in entry) {
      if (!mayBegin) throw _pricingError(`${at}: only a provider row carries a schedule`);
      schedules[i] = _checkSchedule(entry.schedule, at);
    }
    if ('band' in entry) {
      if (!mayBegin) throw _pricingError(`${at}: only a provider row carries a band`);
      _checkBand(entry.band, at);
    }
    const bad = Object.values(_RATE_FIELDS).filter((f) => !_isRate(entry[f]));
    if (bad.length) throw _pricingError(`${at}: ${bad} not a finite non-negative number`);
    if ('note' in entry && typeof entry.note !== 'string') {
      throw _pricingError(`${at}: 'note' is not a string`);
    }
    const fee = _feeOfNote(entry.note || '', at);
    if (fee) fees[i] = fee;
    if (entry.from === null) {
      if (i > 0) throw _pricingError(`${at}: only the first entry has no 'from'`);
      return;
    }
    if (i === 0 && !mayBegin) {
      throw _pricingError(`${at}: this row cannot begin at a time; 'from' must be null`);
    }
    const start = _instantMs(entry.from);
    if (Number.isNaN(start)) {
      throw _pricingError(`${at}: ${JSON.stringify(entry.from)} is not YYYY-MM-DDTHH:MM:SS with Z or ±HH:MM`);
    }
    if (previous !== null && start <= previous) {
      throw _pricingError(`${at}: 'from' is not after the previous entry's`);
    }
    previous = start;
  });
  return { schedules, fees };
}

// A RECORDED_FEE note's per-request fee (issue #469), summed: the refresh
// writes `<fee> $<amount>/request not modelled: per-request, unpriceable
// from token counts` parts into the entry note, joined with "; " beside a
// discount note. A part containing "/request" is fee-shaped and must match
// in full — a fee-shaped part the browser cannot price refuses the file,
// exactly as the backend loader does; anything else parses no fee.
const _FEE_NOTE = /^\w+ \$(\d+(?:\.\d+)?)\/request not modelled: per-request, unpriceable from token counts$/;

function _feeOfNote(note, at) {
  if (!note) return 0;
  let total = 0;
  for (const part of note.split('; ')) {
    if (!part.includes('/request')) continue;
    const m = part.match(_FEE_NOTE);
    if (!m) {
      throw _pricingError(`${at}: note part ${JSON.stringify(part)} names a per-request fee but is not the documented shape`);
    }
    total += Number(m[1]);
  }
  return total;
}

// A row's append-only history, oldest first: the newest entry is the list
// price, and each earlier one a window ending where its successor starts.
// A provider row may begin at a time (start); before it, it does not exist.
// Mirrors pricing._history.
function _history(entries, where, mayBegin = false) {
  const { schedules, fees } = _checkHistory(entries, where, mayBegin);
  return {
    schedules,
    fees,
    list: _ratesOf(entries[entries.length - 1]),
    windows: entries.slice(0, -1).map((entry, i) => (
      { endExclusive: _instantMs(entries[i + 1].from), rates: _ratesOf(entry) })),
    start: entries[0].from === null ? null : _instantMs(entries[0].from),
  };
}

const _PRICING = _readPricing();
window.modelRates = {};
window.datedRates = {};
window.modelFees = {};
for (const [key, entries] of Object.entries(_PRICING.models)) {
  const { list, windows, fees } = _history(entries, key);
  window.modelRates[key] = list;
  if (windows.length) window.datedRates[key] = windows;
  if (Object.keys(fees).length) window.modelFees[key] = fees;
}
// Keyed by normalised model id then provider (the transcript's
// message.provider spelling).
window.providerRates = {};
window.providerDatedRates = {};
window.providerStarts = {};
window.providerSchedules = {};
window.providerFees = {};
for (const [model, hosts] of Object.entries(_PRICING.providers)) {
  for (const [host, entries] of Object.entries(hosts)) {
    const { list, windows, start, schedules, fees } = _history(entries, `${model} via ${host}`, true);
    if (Object.keys(schedules).length) {
      (window.providerSchedules[model] = window.providerSchedules[model] || {})[host] = schedules;
    }
    if (Object.keys(fees).length) {
      (window.providerFees[model] = window.providerFees[model] || {})[host] = fees;
    }
    (window.providerRates[model] = window.providerRates[model] || {})[host] = list;
    if (windows.length) {
      (window.providerDatedRates[model] = window.providerDatedRates[model] || {})[host] = windows;
    }
    if (start !== null) {
      (window.providerStarts[model] = window.providerStarts[model] || {})[host] = start;
    }
  }
}
window.rateEpochs = [...new Set([
  ...[...Object.values(window.datedRates),
      ...Object.values(window.providerDatedRates).flatMap(Object.values)]
    .flat().map((w) => w.endExclusive),
  ...Object.values(window.providerStarts).flatMap(Object.values),
])].sort((a, b) => a - b);

// The vendor tables (SV-RATE-DATA): built by src/vendor-tables.js — the
// browser loads it ahead of this script (index.html's tag order; it also
// installs keyListRates there) and node requires it from here. Mirrors
// pricing_load._vendor_tables.
/* eslint-disable no-undef */
const _vendorTables = (typeof module !== 'undefined'
                       && typeof module.exports !== 'undefined')
  ? require('./vendor-tables.js')
  : window;
const _buildVendorTables = _vendorTables.buildVendorTables;
/* eslint-enable no-undef */
window.vendorBare = {};
window.vendorHosts = {};
_buildVendorTables(_PRICING, window.modelRates, window.vendorBare,
                   window.vendorHosts);
window.vendorPrefixes = [..._PRICING.openrouter.vendor.prefixes];
window.vendorBareForms = Object.keys(window.vendorBare);
window.keyListRates = _vendorTables.keyListRates;

// The default estimate needs its row the moment the first unknown id
// resolves, so a document naming no claude-opus-4-7 row in either table
// refuses at load — the mirror of the backend loader's own refusal. A
// tracked-but-rowless claude-opus-4-7 refuses the same way: the row the
// bare path would read does not exist, and keyListRates would otherwise
// throw a raw TypeError at resolve time.
const _defaultTracked = window.vendorBare['claude-opus-4-7'];
const _defaultRow = window.modelRates['claude-opus-4-7']
  ?? (_defaultTracked === undefined
      ? undefined
      : (window.providerRates[_defaultTracked] || {})[
           window.vendorHosts[_defaultTracked]]);
if (_defaultRow === undefined) {
  throw _pricingError('no claude-opus-4-7 row prices the default estimate');
}

// Every rate an OpenRouter free model carries: zero. Returned for any id
// ending in ':free' or starting with 'stealth/' — see _isFreeModel in
// src/rates.js.
// Deliberately NOT a window.modelRates row, so it is not enumerable as an
// exact key.
window.FREE_RATES = Object.fromEntries(Object.keys(_RATE_FIELDS).map((k) => [k, 0]));
window.scheduleRatesAt = _scheduledRates;

// The long-context meter's membership and per-model entries (issue
// #878): pricing.json's long_context_models and long_context_meters,
// validated like backend/pricing_load.py — a rule-breaking file throws
// naming the key. window.longContextModels is the dashed-key membership
// list; window.longContextMeters maps a member to its complete meter entry.
const _lcMembers = _PRICING.long_context_models;
if (_lcMembers === undefined) {
  throw _pricingError('long_context_models is missing');
}
if (!Array.isArray(_lcMembers)
    || !_lcMembers.every((k) => typeof k === 'string' && k)
    || new Set(_lcMembers).size !== _lcMembers.length) {
  throw _pricingError('long_context_models: not a list of distinct non-empty keys');
}
for (const k of _lcMembers)
  // A member names a models-table key or a tracked key — the merged
  // view's two tables, the same rule the backend loader enforces
  // (issue #851: the first-party members are tracked keys now).
  if (!(k in window.modelRates) && !(k in window.vendorBare))
    throw _pricingError(`long_context_models: ${k} names no models-table or tracked key`);
window.longContextModels = _lcMembers;
window.longContextMeters = {};
const _rawLcMeters = _PRICING.long_context_meters;
const _lcMeters = (Array.isArray(_rawLcMeters) && !_rawLcMeters.length)
  ? {} : (_rawLcMeters || {});
if (typeof _lcMeters !== 'object' || Array.isArray(_lcMeters))
  throw _pricingError('long_context_meters: not a map of member keys to meter entries');
for (const [k, v] of Object.entries(_lcMeters || {})) {
  if (!window.longContextModels.includes(k))
    throw _pricingError(`long_context_meters: ${k} names no long_context_models member`);
  if (!v || typeof v !== 'object' || Array.isArray(v)
      || !Object.prototype.hasOwnProperty.call(v, 'threshold')
      || Object.keys(v).some((field) =>
        !['threshold', 'input_mult', 'output_mult'].includes(field))
      || !Number.isInteger(v.threshold)
      || v.threshold <= 0)
    throw _pricingError(`long_context_meters: ${k} is not a meter entry with a positive integer threshold`);
  for (const field of ['input_mult', 'output_mult']) {
    if (Object.prototype.hasOwnProperty.call(v, field)
        && (typeof v[field] !== 'number' || !Number.isFinite(v[field])
            || v[field] <= 0))
      throw _pricingError(`long_context_meters: ${k} has invalid ${field}; expected a positive finite number`);
  }
  window.longContextMeters[k] = v;
}
