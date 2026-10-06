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

// A user text that OPENS with an XML tag is harness-injected data, not a
// prompt (issue #213) — deny-by-default, so an unknown future harness tag
// is excluded without a parser change; only wrappers around human text
// are kept (<pasted_content> wraps a human paste, which IS a prompt).
// Mirrors backend/parse.py:_is_prompt_text (SV-PARSER-SPEC). A text failing
// the gate is not pushed as a user_message event at all, so the userMsgs
// stat, the ctx-turn boundaries in context-growth-view.jsx and the prompt
// lists in app.jsx all skip it the way backend prompt_count does. The
// interrupt marker mirrors backend/constants.INTERRUPT_MARKER, which the
// backend also denies before a text can anchor or count. This escaped
// whitespace class mirrors backend/prompt_gate.py verbatim: JS \s and
// trim() use a different Unicode repertoire and would drift.
const PROMPT_WS_CLASS = String.raw`\x09-\x0d\x1c-\x1f\x20\x85\xa0\u1680` +
  String.raw`\u2000-\u200a\u2028\u2029\u202f\u205f\u3000`;
const PROMPT_WS_RE = new RegExp('^[' + PROMPT_WS_CLASS + ']+');
const PROMPT_DATA_TAG_RE = new RegExp(
  '^<([A-Za-z][A-Za-z0-9._:-]*)(?:[' + PROMPT_WS_CLASS + '][^<>]*)?>',
);
const PROMPT_HUMAN_TAGS = new Set(['pasted_content']);
const INTERRUPT_MARKER = '[Request interrupted by user';

function isPromptText(text) {
  const stripped = String(text ?? '').replace(PROMPT_WS_RE, '');
  if (!stripped) return false;
  const m = PROMPT_DATA_TAG_RE.exec(stripped);
  if (!m) return true;
  return PROMPT_HUMAN_TAGS.has(m[1]);
}

function shouldPushUserText(text) {
  const s = String(text ?? '');
  const stripped = s.replace(PROMPT_WS_RE, '');
  return isPromptText(s) && !stripped.startsWith(INTERRUPT_MARKER);
}

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
  // Cross-file dedup (src/record-dedup.js): seen map + per-line stamps.
  const seenUuids = (opts && opts.seenUuids) || null;
  const dedupStamps = new Map();

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
      if (!shouldPushUserText(content)) return;
      const refs = detectRefs(content);
      events.push({ line: lineNum, type: 'user_message', ts, detail: content, refs });
      return;
    }
    if (!Array.isArray(content)) return;
    // ONE user_message per record (issue #215): any gate-passing text
    // block or image block makes the record one prompt, mirroring
    // backend parse.py's per-record count. An image inside a
    // tool_result below is result payload and never joins the detail.
    const texts = [];
    let sawImage = false;
    for (const c of content) {
      if (!c || typeof c !== 'object' || Array.isArray(c)) continue;
      if (c.type === 'tool_result') {
        let resultText = '';
        const raw = c.content;
        if (Array.isArray(raw)) {
          resultText = raw
            .filter(x => typeof x === 'string'
              || (x && typeof x === 'object' && !Array.isArray(x)
                && x.type === 'text'))
            .map(x => (typeof x === 'string' ? x : (x.text || '')))
            .join('\n');
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
        if (shouldPushUserText(c.text)) texts.push(c.text);
      } else if (c.type === 'image') {
        sawImage = true;
      }
    }
    if (texts.length || sawImage) {
      const detail = texts
        .concat(sawImage ? ['[image attachment]'] : [])
        .join('\n\n');
      events.push({ line: lineNum, type: 'user_message', ts, detail });
    }
  }

  function pushAssistantContent(content, lineNum, ts) {
    if (!Array.isArray(content)) return;
    for (const c of content) {
      if (!c || typeof c !== 'object' || Array.isArray(c)) continue;
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
          ev.agent_name = (window.canonicalAgentType || (n => n))((c.input && c.input.subagent_type) || (c.input && c.input.name) || '?');
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
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) continue;

    // Cross-file dedup (directory / multi-load mode): the winner rule, the
    // stamps and the retraction live in record-dedup.js (#529, #562, #563).
    if (seenUuids && typeof obj.uuid === 'string' && obj.uuid
        && window.recordDedup.decide(seenUuids, obj, i + 1, dedupStamps) === 'skip') {
      continue;
    }

    // An offset-less ISO timestamp is UTC (issue #376, mirrors parse_common._to_dt): unstamped, Date.parse reads the viewer's zone.
    let ts = obj.timestamp || '';
    if (typeof ts === 'string' && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(ts)) ts += 'Z';
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
      if (usage && typeof usage === 'object' && !Array.isArray(usage)
          && Object.keys(usage).length > 0
          && (m.model || '') !== '<synthetic>') {
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

  // Retract the masked dedup copies before the batch pass judges the survivors.
  if (seenUuids) window.recordDedup.retract(events, meta, dedupStamps, seenUuids);
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

// ─── pricing: tables and resolution ─────────────────────────────────────
// The rate tables are read and validated by src/pricing-loader.js, which
// runs before this script and exposes window.modelRates and its siblings
// (fees included, issue #469), window.FREE_RATES and window.scheduleRatesAt.
// This half resolves a model id against those tables — pricing.resolve's
// mirror — and prices records in computeSessionStats exactly as
// pricing.compute_cost does, so the Inspector and the database agree.


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
  for (const [re, tierKey] of _TIER_FALLBACKS) {
    if (re.test(norm)) return { rates: window.modelRates[tierKey], kind: 'tier', key: null, fee: 0 };
  }
  return { rates: window.modelRates['claude-opus-4-7'], kind: 'default', key: null, fee: 0 };
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
    return window.resolveModelRate(model, ts, provider);
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
    // multiplies, same per-term division — plus the serving host's
    // per-request fee (issue #469), folded once per record exactly as
    // the backend folds it, and rounded per record like the stored
    // cost_usd column: Python's round(x, 6), half-even on the exact
    // expansion (round6HalfEven above). Each record's cost here IS the
    // stored value, not a lookalike.
    stats.cost += round6HalfEven(
      f * r.rates.fresh * lcIn / 1_000_000
      + eph5 * r.rates.c5 * lcIn / 1_000_000
      + (eph1h + unsplit) * r.rates.c1h * lcIn / 1_000_000
      + cr * r.rates.read * lcIn / 1_000_000
      + o * r.rates.out * lcOut / 1_000_000
      + (r.fee || 0)
    );
  }

  stats.totalInput = stats.fresh + stats.create + stats.read;
  stats.hitRate = stats.totalInput ? (stats.read / stats.totalInput) * 100 : 0;
  return stats;
};
