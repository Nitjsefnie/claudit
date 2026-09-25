// JSONL parser for Claude Code transcripts.
// Implements SV-PARSER-SPEC alongside backend/parse.py: extracts
// structured events (user/assistant/tool_call/tool_result/thinking/
// agent_spawn) plus meta events (assistant_usage, system,
// queue-operation, attachment).
//
// Lane transcripts (Codex rollouts, the two Kimi wire formats) are parsed
// by src/parser-lanes.js, which emits the same shapes; parseTranscript
// sniffs the format and delegates. One rate table only — src/pricing.json,
// loaded below.

// Per-call context-window size. Mirrors backend/parse.py:_usage_ctx_input.
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

window.parseTranscript = function parseTranscript(text, opts) {
  // A lane format is parsed by parser-lanes.js (loaded before this file).
  // Without parser-lanes.js — a bare require of parser.js in tests — the
  // Claude path below is the whole parser, as before.
  if (window.sniffTranscriptFormat) {
    const fmt = window.sniffTranscriptFormat(text);
    if (fmt !== 'claude') return window.parseTranscriptLanes(text, opts);
  }

  const events = [];
  const meta = [];
  const lines = text.split('\n');
  const seenReq = new Map(); // merge key -> usage event (for streaming merge)
  const seenUuids = (opts && opts.seenUuids) || null; // optional cross-file dedup

  function mergeUsageMax(existing, incoming) {
    // Recursive max merge: numeric fields take max, nested dicts merge
    // key-by-key, non-numeric fields keep `existing` if present else
    // copy from `incoming`. Mirrors backend/parse.py's _merge_usage_max.
    if (existing == null) return incoming;
    if (incoming == null) return existing;
    if (typeof existing === 'number' && typeof incoming === 'number') {
      return Math.max(existing, incoming);
    }
    if (typeof existing === 'object' && typeof incoming === 'object'
        && !Array.isArray(existing) && !Array.isArray(incoming)) {
      const out = { ...existing };
      for (const k of Object.keys(incoming)) {
        out[k] = (k in out) ? mergeUsageMax(out[k], incoming[k]) : incoming[k];
      }
      return out;
    }
    return existing; // type mismatch — keep existing
  }

  // Sniff a tool_result / user_message body for off-disk references:
  // - <task-notification>…<output-file>…</output-file>…</task-notification>
  //   pointing at a sibling agent-<id>.jsonl (subagent transcript)
  // - tool-results/<id>.<ext> paths (sidecar tool output files)
  // - bare agent-<id> references
  // Returns an array of { kind, ... } records or [] if none found.
  function detectRefs(text) {
    if (!text || typeof text !== 'string') return [];
    const refs = [];
    // Task notifications (subagent finished / event)
    const taskRe = /<task-notification>([\s\S]*?)<\/task-notification>/g;
    let m;
    while ((m = taskRe.exec(text)) !== null) {
      const body = m[1];
      const taskId   = (body.match(/<task-id>([^<]+)<\/task-id>/) || [])[1];
      const toolUse  = (body.match(/<tool-use-id>([^<]+)<\/tool-use-id>/) || [])[1];
      const outFile  = (body.match(/<output-file>([^<]+)<\/output-file>/) || [])[1];
      const event    = (body.match(/<event>([^<]+)<\/event>/) || [])[1];
      refs.push({
        kind: 'task_notification',
        task_id: taskId || '',
        tool_use_id: toolUse || '',
        output_file: outFile || '',
        event: event || '',
      });
    }
    // Sidecar tool-result files
    const fileRe = /(?:^|[^a-zA-Z0-9._-])(tool-results\/[A-Za-z0-9._-]+\.[a-zA-Z]+)/g;
    while ((m = fileRe.exec(text)) !== null) {
      refs.push({ kind: 'tool_result_file', path: m[1] });
    }
    // Bare agent IDs (only for explicit "agent-<hex>" with at least 12 hex chars)
    const agentRe = /\bagent-([a-f0-9]{12,})\b/g;
    const seenAgents = new Set();
    while ((m = agentRe.exec(text)) !== null) {
      if (seenAgents.has(m[1])) continue;
      seenAgents.add(m[1]);
      refs.push({ kind: 'agent_id', agent_id: m[1] });
    }
    return refs;
  }

  function pushUserContent(content, toolUseResult, lineNum, ts) {
    if (typeof content === 'string') {
      const refs = detectRefs(content);
      events.push({ line: lineNum, type: 'user_message', ts, detail: content, refs });
      return;
    }
    if (!Array.isArray(content)) return;
    for (const c of content) {
      if (c.type === 'tool_result') {
        let resultText = '';
        const raw = c.content;
        if (Array.isArray(raw)) {
          resultText = raw.map(x => (x && typeof x === 'object' ? (x.text || '') : String(x))).join('\n');
        } else {
          resultText = String(raw ?? '');
        }
        // Detect referenced sidecar files / subagent links inside the
        // result text (out-of-band attachments that live outside the JSONL).
        const refs = detectRefs(resultText);
        events.push({
          line: lineNum, type: 'tool_result', ts,
          tool_use_id: c.tool_use_id || '',
          is_error: !!c.is_error,
          detail: resultText,
          toolUseResult,
          refs,
        });
      } else if (c.type === 'text') {
        events.push({ line: lineNum, type: 'user_message', ts, detail: c.text });
      } else if (c.type === 'image') {
        events.push({ line: lineNum, type: 'user_message', ts, detail: '[image attachment]' });
      }
    }
  }

  function pushAssistantContent(content, lineNum, ts) {
    if (!Array.isArray(content)) return;
    for (const c of content) {
      if (c.type === 'text') {
        events.push({ line: lineNum, type: 'assistant_text', ts, detail: c.text });
      } else if (c.type === 'tool_use') {
        const ev = {
          line: lineNum,
          type: 'tool_call',
          ts,
          tool_name: c.name || '',
          tool_input: c.input || {},
          tool_use_id: c.id || '',
          detail: '',
        };
        if (c.name === 'Agent' || c.name === 'Task') {
          ev.type = 'agent_spawn';
          ev.agent_name = (c.input && c.input.name) || (c.input && c.input.subagent_type) || '?';
          ev.agent_model = (c.input && c.input.model) || '(default)';
          ev.agent_bg = !!(c.input && c.input.run_in_background);
          ev.agent_team = (c.input && c.input.team_name) || '(none)';
          ev.agent_prompt = (c.input && c.input.prompt) || '';
        }
        events.push(ev);
      } else if (c.type === 'thinking') {
        if (c.thinking) {
          events.push({ line: lineNum, type: 'thinking', ts, detail: c.thinking });
        }
      }
    }
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line) continue;
    let obj;
    try { obj = JSON.parse(line); } catch {
      events.push({ line: i + 1, type: 'parse_error', ts: '', detail: 'Invalid JSON' });
      continue;
    }

    // Cross-file dedup (directory / multi-load mode). Two files holding
    // the SAME API call — typically a session's main jsonl and one of its
    // agent-*.jsonl files — share an inner record `uuid`. Skip the second
    // occurrence so cost / turn / tool counts don't double-count.
    if (seenUuids) {
      const recUuid = obj.uuid;
      if (recUuid) {
        if (seenUuids.has(recUuid)) continue;
        seenUuids.add(recUuid);
      }
    }

    const ts = obj.timestamp || '';
    const msgType = obj.type || '';

    if (msgType === 'progress' || msgType === 'file-history-snapshot') continue;

    if (msgType === 'queue-operation') {
      meta.push({ line: i + 1, type: 'queue-operation', ts, operation: obj.operation || '', content: obj.content || '', raw: obj });
      continue;
    }
    if (msgType === 'system') {
      const content = obj.content || '';
      const subtype = obj.subtype || '';
      meta.push({ line: i + 1, type: 'system', ts, subtype, content, raw: obj });
      // Detect rate-limit hits from system content
      const lower = (content + ' ' + subtype).toLowerCase();
      if (lower.includes('rate limit') || lower.includes('rate_limit') || lower.includes('429')) {
        meta.push({ line: i + 1, type: 'rate_limit', ts, content, raw: obj });
      }
      continue;
    }
    if (msgType === 'attachment') {
      meta.push({ line: i + 1, type: 'attachment', ts, attachment_type: (obj.attachment && obj.attachment.type) || '', raw: obj });
      continue;
    }
    if (msgType === 'permission-mode') {
      meta.push({ line: i + 1, type: 'permission-mode', ts, permissionMode: obj.permissionMode || '', raw: obj });
      continue;
    }

    const m = obj.message || {};
    const role = m.role || '';
    const content = m.content || '';

    if (role === 'user') {
      pushUserContent(content, obj.toolUseResult, i + 1, ts);
    } else if (role === 'assistant') {
      const usage = m.usage;
      // Skip synthetic stubs — Claude Code emits these after `/exit` and
      // for interrupted partial responses with all-zero usage and no
      // requestId. They clobber the `last_usage` walk in any per-turn
      // aggregation. Mirrors backend/parse.py.
      if (usage && (m.model || '') !== '<synthetic>') {
        const reqId = obj.requestId || '';
        // No requestId (a Z.ai-served transcript): the lines of one API
        // message still share message.id. Mirrors backend/parse.py _merge_key.
        const mergeKey = reqId || (typeof m.id === 'string' && m.id ? `msg:${m.id}` : '');
        const ev = {
          line: i + 1, type: 'assistant_usage', ts,
          model: m.model || '(unknown)',
          // The serving host (OpenRouter only). Mirrors parse._provider.
          provider: (typeof m.provider === 'string' && m.provider.trim()) || null,
          requestId: reqId,
          uuid: obj.uuid || '',
          sessionId: obj.sessionId || '',
          usage: { ...usage },
        };
        if (mergeKey && seenReq.has(mergeKey)) {
          // Recursive max merge — handles streaming where output_tokens is
          // reported incrementally, plus nested cache_creation dict.
          const existing = seenReq.get(mergeKey);
          existing.usage = mergeUsageMax(existing.usage, usage);
          if (existing.provider == null) existing.provider = ev.provider;
        } else {
          if (mergeKey) seenReq.set(mergeKey, ev);
          meta.push(ev);
        }
      }
      pushAssistantContent(content, i + 1, ts);
    }
  }

  // Annotate parallel batches: consecutive tool calls < 2s apart
  const toolEvs = events.filter(e => e.type === 'tool_call' || e.type === 'agent_spawn');
  if (toolEvs.length) {
    const batches = [[toolEvs[0]]];
    for (let i = 1; i < toolEvs.length; i++) {
      const prev = batches[batches.length - 1].slice(-1)[0];
      const tp = Date.parse(prev.ts);
      const tc = Date.parse(toolEvs[i].ts);
      if (!isNaN(tp) && !isNaN(tc) && Math.abs(tc - tp) < 2000) {
        batches[batches.length - 1].push(toolEvs[i]);
      } else {
        batches.push([toolEvs[i]]);
      }
    }
    for (const b of batches) {
      b.forEach((e, idx) => { e.batch_size = b.length; e.batch_index = idx + 1; });
    }
  }

  // Link tool_call <-> tool_result
  const callMap = new Map();
  for (const e of events) {
    if ((e.type === 'tool_call' || e.type === 'agent_spawn') && e.tool_use_id) {
      callMap.set(e.tool_use_id, e);
    }
  }
  for (const e of events) {
    if (e.type === 'tool_result' && e.tool_use_id) {
      const call = callMap.get(e.tool_use_id);
      if (call) {
        e.paired_call = call;
        call.paired_result = e;
      }
    }
  }

  return { events, meta };
};

// Every rate lives in src/pricing.json (SV-RATE-DATA), the same file
// backend/pricing.py loads; this file holds resolution logic only. Both
// the Inspector (computeSessionStats below) and the Token Breakdown panel
// (app.jsx) read the derived tables via window.modelRates /
// window.rateForModel.
//
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

// A number under a schedule window's start or end must be spelled a plain
// JSON integer (/^-?\d+$/): Python reads 1400.0 as a float and refuses it
// (pricing._hhmm), while JSON.parse reads it as 1400 and would silently
// price what Python refuses to load. No reviver can catch this — it only
// ever had source-text access under node, so the browser silently accepted
// 1400.0 — so both load paths run this check on the RAW text, before
// parsing. The check is scoped by structure, not the key name: only a
// start/end that is a member of an object which is a direct ELEMENT of the
// array that is the value of a "schedule" key is an HHMM position; a
// fractional start anywhere else (a future openrouter.start, a window's
// rates object) parses untouched. Malformed input bails the scan silently —
// JSON.parse names that error.
function _checkHhmmSpelling(text) {
  const offenses = [];
  let pos = 0;
  const n = text.length;
  const stack = [];
  const top = () => stack[stack.length - 1];

  const skipWs = () => {
    while (pos < n && ' \t\n\r'.includes(text[pos])) pos++;
  };

  // The decoded string literal at pos (pos sits on the opening quote), or
  // null when malformed — malformed input bails the scan.
  const readString = () => {
    let j = pos + 1;
    let out = '';
    while (j < n) {
      const c = text[j];
      if (c === '"') { pos = j + 1; return out; }
      if (c === '\\') {
        const e = text[j + 1];
        if (e === 'u') {
          const hex = text.slice(j + 2, j + 6);
          if (!/^[0-9a-fA-F]{4}$/.test(hex)) return null;
          out += String.fromCharCode(parseInt(hex, 16));
          j += 6;
        } else {
          const esc = { '"': '"', '\\': '\\', '/': '/', b: '\b', f: '\f',
                        n: '\n', r: '\r', t: '\t' }[e];
          if (esc === undefined) return null;
          out += esc;
          j += 2;
        }
      } else if (c < ' ') {
        return null;                 // a raw control character: not JSON
      } else {
        out += c;
        j++;
      }
    }
    return null;                     // unterminated
  };

  // The raw number token at pos, pos advanced past it.
  const readNumber = () => {
    const start = pos;
    if (text[pos] === '-') pos++;
    while (pos < n && text[pos] >= '0' && text[pos] <= '9') pos++;
    if (text[pos] === '.') {
      pos++;
      while (pos < n && text[pos] >= '0' && text[pos] <= '9') pos++;
    }
    if (text[pos] === 'e' || text[pos] === 'E') {
      pos++;
      if (text[pos] === '+' || text[pos] === '-') pos++;
      while (pos < n && text[pos] >= '0' && text[pos] <= '9') pos++;
    }
    return text.slice(start, pos);
  };

  // After a complete value: consume the ',' (more members/elements follow)
  // or the container's closer (pop, repeat for the parent). Sets `dead`
  // when the scan ends — root value completed, or a malformed tail that
  // JSON.parse will name.
  let dead = false;
  const finishValue = () => {
    for (;;) {
      skipWs();
      const t = top();
      if (!t) { dead = true; return; }
      const c = text[pos];
      if (c === ',') { pos++; return; }
      if (t.obj ? c === '}' : c === ']') { pos++; stack.pop(); continue; }
      dead = true;
      return;
    }
  };

  skipWs();
  const first = text[pos];
  if (first === '{') stack.push({ obj: true, key: null, window: false });
  else if (first === '[') stack.push({ obj: false, schedule: false });
  else return;                       // a scalar root: nothing to check
  pos++;

  for (;;) {
    skipWs();
    if (pos >= n || !top()) return;  // truncated, or complete
    const t = top();
    const c = text[pos];
    if (c === (t.obj ? '}' : ']')) {  // an empty container
      pos++;
      stack.pop();
      finishValue();
      if (dead) break;
      continue;
    }
    if (t.obj) {
      if (c !== '"') return;         // an object key must be a string
      const key = readString();
      if (key === null) return;
      t.key = key;
      skipWs();
      if (text[pos] !== ':') return;
      pos++;
      skipWs();                      // whitespace between ':' and the value
    } else if (c === ',') {          // the next element of an array
      pos++;
      continue;
    }
    // A value position:
    const v = text[pos];
    if (v === '{') {
      pos++;
      stack.push({ obj: true, key: null, window: !t.obj && t.schedule });
      continue;
    }
    if (v === '[') {
      pos++;
      stack.push({ obj: false, schedule: t.obj && t.key === 'schedule' });
      continue;
    }
    if (v === '"') {
      if (readString() === null) return;
    } else if (v === '-' || (v >= '0' && v <= '9')) {
      const at = pos;
      const token = readNumber();
      // A malformed token (a bare '-', a leading zero) is not an offense:
      // JSON.parse refuses the file and names it.
      if (!/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(token)) return;
      if (t.obj && t.window && (t.key === 'start' || t.key === 'end')
          && !/^-?\d+$/.test(token)) {
        offenses.push(`offset ${at} spells a schedule ${t.key} as ${token}`);
      }
    } else if (v === 't' && text.startsWith('true', pos)) {
      pos += 4;
    } else if (v === 'f' && text.startsWith('false', pos)) {
      pos += 5;
    } else if (v === 'n' && text.startsWith('null', pos)) {
      pos += 4;
    } else {
      return;                        // unexpected; JSON.parse names it
    }
    finishValue();
    if (dead) break;
  }
  if (offenses.length) {
    throw _pricingError(`${offenses.join('; ')}; a schedule start or end`
      + ' must be spelled a plain JSON integer');
  }
}

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

function _checkHistory(entries, where, mayBegin) {
  if (!entries.length) throw _pricingError(`${where}: empty history`);
  let previous = null;
  const schedules = {};
  entries.forEach((entry, i) => {
    const at = `${where}[${i}]`;
    const fields = Object.keys(entry).filter((k) => !['from', 'note', 'schedule'].includes(k));
    if (fields.sort().join() !== _FIELD_NAMES || !('from' in entry)) {
      throw _pricingError(`${at}: fields ${Object.keys(entry).sort()}`);
    }
    if ('schedule' in entry) {
      if (!mayBegin) throw _pricingError(`${at}: only a provider row carries a schedule`);
      schedules[i] = _checkSchedule(entry.schedule, at);
    }
    const bad = Object.values(_RATE_FIELDS).filter((f) => !_isRate(entry[f]));
    if (bad.length) throw _pricingError(`${at}: ${bad} not a finite non-negative number`);
    if ('note' in entry && typeof entry.note !== 'string') {
      throw _pricingError(`${at}: 'note' is not a string`);
    }
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
  return schedules;
}

// A row's append-only history, oldest first: the newest entry is the list
// price, and each earlier one a window ending where its successor starts.
// A provider row may begin at a time (start); before it, it does not exist.
// Mirrors pricing._history.
function _history(entries, where, mayBegin = false) {
  const schedules = _checkHistory(entries, where, mayBegin);
  return {
    schedules,
    list: _ratesOf(entries[entries.length - 1]),
    windows: entries.slice(0, -1).map((entry, i) => (
      { endExclusive: _instantMs(entries[i + 1].from), rates: _ratesOf(entry) })),
    start: entries[0].from === null ? null : _instantMs(entries[0].from),
  };
}

const _PRICING = _readPricing();
window.modelRates = {};
window.datedRates = {};
for (const [key, entries] of Object.entries(_PRICING.models)) {
  const { list, windows } = _history(entries, key);
  window.modelRates[key] = list;
  if (windows.length) window.datedRates[key] = windows;
}
// Keyed by normalised model id then provider (the transcript's
// message.provider spelling).
window.providerRates = {};
window.providerDatedRates = {};
window.providerStarts = {};
window.providerSchedules = {};
for (const [model, hosts] of Object.entries(_PRICING.providers)) {
  for (const [host, entries] of Object.entries(hosts)) {
    const { list, windows, start, schedules } = _history(entries, `${model} via ${host}`, true);
    if (Object.keys(schedules).length) {
      (window.providerSchedules[model] = window.providerSchedules[model] || {})[host] = schedules;
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

// Every rate an OpenRouter free model carries: zero. Returned for any id
// ending in ':free' or starting with 'stealth/' — see _isFreeModel.
// Deliberately NOT a window.modelRates row, so it is not enumerable as an
// exact key.
window.FREE_RATES = Object.fromEntries(Object.keys(_RATE_FIELDS).map((k) => [k, 0]));

// Family fallbacks for unrecognised Claude models — current-generation
// list rates for the tier, never a dated promotion. The generation is the
// highest-versioned key of the family in the table, so a new model row
// moves its family's fallback with no second edit. Ties keep table order.
const _VERSIONED_KEY = /^claude-([a-z]+)-(\d+(?:-\d+)*)$/;
const _latestKey = (families) => {
  let best = null;
  let bestVersion = null;
  for (const key of Object.keys(window.modelRates)) {
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

// Resolve a model id to rates, reporting how confident the match is:
// 'exact' | 'tier' | 'default'. Anything but 'exact' is an estimate.
// `provider` is the record's serving host: a (model, provider) row wins,
// otherwise (and always without one) the model alone decides.
window.resolveModelRate = function resolveModelRate(model, ts, provider) {
  const norm = _normaliseModel(model);
  if (_isFreeModel(model, norm)) {
    return { rates: window.FREE_RATES, kind: 'exact', key: norm };
  }
  const pkey = provider ? _providerModelKey(norm, provider, ts) : null;
  if (pkey) {
    const windows = (window.providerDatedRates[pkey] || {})[provider];
    const rates = _inWindow(windows, ts, window.providerRates[pkey][provider]);
    const t = _toMillis(ts);
    const entry = (windows || []).filter((w) => w.endExclusive <= t).length;
    const schedule = t == null ? null : ((window.providerSchedules[pkey] || {})[provider] || {})[entry];
    if (!schedule) return { rates, kind: 'exact', key: pkey };
    return { rates: _scheduledRates(schedule, t) || rates, kind: 'exact', key: pkey };
  }
  const key = _matchRateKey(norm);
  if (key) {
    return { rates: _inWindow(window.datedRates[key], ts, window.modelRates[key]),
             kind: 'exact', key };
  }
  for (const [re, tierKey] of _TIER_FALLBACKS) {
    if (re.test(norm)) return { rates: window.modelRates[tierKey], kind: 'tier', key: null };
  }
  return { rates: window.modelRates['claude-opus-4-7'], kind: 'default', key: null };
};

window.rateForModel = function rateForModel(model, ts, provider) {
  return window.resolveModelRate(model, ts, provider).rates;
};

window.computeSessionStats = function (events, meta) {
  const stats = {
    firstTs: null, lastTs: null,
    userMsgs: 0, asstMsgs: 0, thinking: 0,
    toolCalls: 0, toolResults: 0, errorResults: 0,
    parallelBatches: 0, parallelCalls: 0,
    toolCounts: {},
    models: new Set(),
    fresh: 0, create: 0, read: 0, output: 0, eph5: 0, eph1h: 0,
    turns: 0,
    cost: 0,
  };

  for (const e of events) {
    const t = Date.parse(e.ts);
    if (!isNaN(t)) {
      if (stats.firstTs == null || t < stats.firstTs) stats.firstTs = t;
      if (stats.lastTs == null || t > stats.lastTs) stats.lastTs = t;
    }
    if (e.type === 'tool_call') {
      stats.toolCalls++;
      stats.toolCounts[e.tool_name] = (stats.toolCounts[e.tool_name] || 0) + 1;
    } else if (e.type === 'agent_spawn') {
      stats.toolCalls++;
      stats.toolCounts['Agent'] = (stats.toolCounts['Agent'] || 0) + 1;
    } else if (e.type === 'user_message') stats.userMsgs++;
    else if (e.type === 'assistant_text') stats.asstMsgs++;
    else if (e.type === 'tool_result') {
      stats.toolResults++;
      if (e.is_error) stats.errorResults++;
    } else if (e.type === 'thinking') stats.thinking++;
    if (e.batch_size > 1 && e.batch_index === 1) {
      stats.parallelBatches++;
      stats.parallelCalls += e.batch_size;
    }
  }

  function rate(model, ts, provider) {
    return window.rateForModel(model, ts, provider);
  }

  // Python's round(x, 6), which priced the stored cost_usd: round the
  // double's EXACT value to 6 places, ties to EVEN. A digit-window over
  // toFixed() output is not sound for this — near-ties a hair below .5
  // (a value like ...4999999999999999957) can round UP into a clean
  // "5000...0" tail at 20 digits and get misread as an exact tie — so
  // this works on the bits instead: value = mant * 2^exp, and scaling
  // by 10^6 keeps the divisor a pure power of two, which BigInt divides
  // exactly. The decision is then the same one Python makes, because it
  // is made on the same quantity.
  function round6HalfEven(x) {
    if (!Number.isFinite(x)) return x;
    const neg = x < 0;
    const buf = new ArrayBuffer(8);
    const view = new DataView(buf);
    view.setFloat64(0, neg ? -x : x);
    const bits = view.getBigUint64(0);
    const rawExp = Number((bits >> 52n) & 0x7ffn);
    let mant = bits & 0xfffffffffffffn;
    let exp;
    if (rawExp === 0) {
      exp = -1074; // subnormal: no implicit leading bit
    } else {
      mant |= 1n << 52n;
      exp = rawExp - 1075;
    }
    const scaled = mant * 1000000n; // value * 1e6 before the 2^exp shift
    let q;
    if (exp >= 0) {
      q = scaled << exp; // an exact integer: nothing fractional to drop
    } else {
      const den = 2n ** BigInt(-exp);
      q = scaled / den;
      const r = scaled % den;
      const twice = r * 2n;
      // More than half rounds up; an exact tie rounds to even.
      if (twice > den || (twice === den && (q & 1n) === 1n)) q += 1n;
    }
    return (neg ? -1 : 1) * Number(q) / 1e6;
  }

  for (const m of meta) {
    if (m.type !== 'assistant_usage') continue;
    stats.turns++;
    stats.models.add(m.model);
    const u = m.usage;
    const f = u.input_tokens || 0;
    const cc = u.cache_creation_input_tokens || 0;
    const cr = u.cache_read_input_tokens || 0;
    const o = u.output_tokens || 0;
    const eph5 = (u.cache_creation && u.cache_creation.ephemeral_5m_input_tokens) || 0;
    const eph1h = (u.cache_creation && u.cache_creation.ephemeral_1h_input_tokens) || 0;
    stats.fresh += f; stats.create += cc; stats.read += cr; stats.output += o;
    stats.eph5 += eph5; stats.eph1h += eph1h;

    const r = rate(m.model || '', m.ts, m.provider);
    const unsplit = Math.max(0, cc - eph5 - eph1h);
    // Codex long-context meter (mirrors pricing.compute_cost's
    // long_context rule): a record whose prompt exceeded
    // window.LONG_CONTEXT_THRESHOLD bills the WHOLE record at 2x input
    // side and 1.5x output. Lanes set m.long_context at parse time;
    // Claude records never carry it, so the multipliers stay 1.
    const lcIn = m.long_context ? window.LONG_CONTEXT_INPUT_MULT : 1.0;
    const lcOut = m.long_context ? window.LONG_CONTEXT_OUTPUT_MULT : 1.0;
    // pricing.compute_cost's operation order, term for term — same
    // multiplies, same per-term division — and rounded per record like
    // the stored cost_usd column: Python's round(x, 6), half-even on
    // the exact expansion (round6HalfEven above). Each record's cost
    // here IS the stored value, not a lookalike.
    stats.cost += round6HalfEven(
      f * r.fresh * lcIn / 1_000_000
      + eph5 * r.c5 * lcIn / 1_000_000
      + (eph1h + unsplit) * r.c1h * lcIn / 1_000_000
      + cr * r.read * lcIn / 1_000_000
      + o * r.out * lcOut / 1_000_000
    );
  }

  stats.totalInput = stats.fresh + stats.create + stats.read;
  stats.hitRate = stats.totalInput ? (stats.read / stats.totalInput) * 100 : 0;
  return stats;
};
