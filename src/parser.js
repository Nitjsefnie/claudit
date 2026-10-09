// JSONL parser for Claude Code transcripts.
// Implements SV-PARSER-SPEC alongside backend/parse.py: extracts
// structured events (user/assistant/tool_call/tool_result/thinking/
// agent_spawn) plus meta events (assistant_usage, system,
// queue-operation, attachment).
//
// Lane transcripts (Codex rollouts, the two Kimi wire formats) are parsed
// by src/parser-lanes.js, which emits the same shapes; parseTranscript
// sniffs the format and delegates. One rate table only — src/pricing.json,
// loaded by src/pricing-loader.js, which runs before this script.

// The browser loads parser-usage.js before this file. Node tests that require
// parser.js directly load the sibling helper here, matching that script order.
if (!window.parserUsage && typeof module === 'object' && module.exports) {
  require('./parser-usage.js');
}

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
      const usage = window.parserUsage.flattenUsage(m.usage);
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
          model: m.model || null, // model-less: null, the backend refuses the file (issue #688)
          // The serving host (OpenRouter only). Mirrors parse._provider.
          provider: (typeof m.provider === 'string' && m.provider.trim()) || null,
          requestId: reqId,
          uuid: obj.uuid || '',
          sessionId: obj.sessionId || '',
          // The meter decision (issue #765), recomputed after a streaming
          // merge below — mirrors parse._project_record.
          long_context: window.longContextFlagFor(m.model, usage),
          web_search_requests: window.parserUsage.searchRequests(usage),
          usage: { ...usage },
        };
        if (mergeKey && seenReq.has(mergeKey)) {
          // Recursive max merge — handles streaming where output_tokens is
          // reported incrementally, plus nested cache_creation dict.
          const existing = seenReq.get(mergeKey);
          existing.usage = window.parserUsage.mergeUsageMax(
            existing.usage, usage);
          if (ev.web_search_requests !== null) {
            existing.web_search_requests = Math.max(
              existing.web_search_requests || 0, ev.web_search_requests);
          }
          if (existing.provider == null) existing.provider = ev.provider;
          existing.long_context = window.longContextFlagFor(existing.model, existing.usage);
        } else {
          if (mergeKey) seenReq.set(mergeKey, ev);
          meta.push(ev);
        }
      }
      pushAssistantContent(content, i + 1, ts);
    }
  }

  // Tool-call dedup (issue #795): the Claude path's tail of the codex
  // lane's seenToolIds contract (issue #766) — a compaction sidecar
  // replays the main file's tool_use blocks under new line uuids, so the
  // line-uuid dedup cannot catch them. The tail is module code in
  // record-dedup.js: decide per call event against the shared map, then
  // splice the losing copies, call and result together. It runs BEFORE
  // retract stamps the survivors with their lines' models — a tool event
  // must carry no model here, so the rank's attribution term stays
  // constant and the caller's dropMaskedTools never outranks a winner's
  // own result.
  const seenToolIds = (opts && opts.seenToolIds) || null;
  if (seenToolIds && window.recordDedup)
    window.recordDedup.dedupToolCalls(events, seenToolIds);
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

window.computeSessionStats = function (events, meta) {
  const stats = {
    firstTs: null, lastTs: null,
    userMsgs: 0, asstMsgs: 0, thinking: 0,
    toolCalls: 0, toolResults: 0, errorResults: 0,
    parallelBatches: 0, parallelCalls: 0,
    toolCounts: {},
    models: new Set(),
    fresh: 0, create: 0, read: 0, output: 0, eph5: 0, eph1h: 0,
    webSearchRequests: 0,
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
    const searches = Number.isInteger(m.web_search_requests)
      && m.web_search_requests >= 0 ? m.web_search_requests : 0;
    stats.webSearchRequests += searches;
    const unsplit = Math.max(0, cc - eph5 - eph1h);
    // The model's long-context factors apply to the whole record; lanes
    // set m.long_context at parse time and the inspector must match the DB.
    const [lcIn, lcOut] = m.long_context
      ? window.longContextFactorsFor(m.model || '') : [1.0, 1.0];
    // pricing.compute_cost's operation order, term for term — same
    // multiplies, same per-term division — plus the explicit web-search
    // rate times this record's request count, exactly as
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
      + searches * (r.rates.search || 0)
    );
  }

  stats.totalInput = stats.fresh + stats.create + stats.read;
  stats.hitRate = stats.totalInput ? (stats.read / stats.totalInput) * 100 : 0;
  return stats;
};
