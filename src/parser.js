// JSONL parser for Claude Code transcripts.
// Mirrors the shapes from parse_session.py: extracts structured events
// (user/assistant/tool_call/tool_result/thinking/agent_spawn) plus meta
// events (assistant_usage, system, queue-operation, attachment).
//
// Lane transcripts (Codex rollouts, the two Kimi wire formats) are parsed
// by src/parser-lanes.js, which emits the same shapes; parseTranscript
// sniffs the format and delegates. One rate table only — the one below.

// Per-call context-window size. Mirrors backend/parse.py:_usage_ctx_input
// and canonical parse_session.py 1.20.6. When usage.iterations has >1
// entries (advisor()/sub-agent fan-out), the top-level fresh+create+read
// is the BILLING sum across iterations, not the peak single-call window.
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
    // copy from `incoming`. Mirrors parse_session.py's _merge_usage_max.
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
      // aggregation. Mirrors parse_session.py 1.20.4 / backend/parse.py.
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

// Compute session-level stats
// Mirrors backend/pricing.py — order matters (most-specific first
// so 'claude-opus-4-7' doesn't misroute to 'claude-opus-4').
// Single source of truth for in-browser cost computation; both the
// Inspector (computeSessionStats below) and the Token Breakdown panel
// (app.jsx) read these via window.modelRates / window.rateForModel.
window.modelRates = {
  // bonsai-2-27b is served by a local llama.cpp — no price; listed so it
  // does not fall to the DEFAULT (Opus list) fallback.
  'bonsai-2-27b':      { fresh: 0,    c5: 0,     c1h: 0,     read: 0,     out: 0 },
  // GLM (Z.ai): cache WRITES are free and reads are 0.2x input, so the
  // Anthropic 1.25x/2x/0.1x relations do not hold; explicit numbers.
  'glm-5-3-flash':     { fresh: 0.15, c5: 0,     c1h: 0,     read: 0.03,  out: 0.5 },
  // Codex (OpenAI) and Kimi lanes, merged into claudit's one table from
  // codexmeter's pricing (D4/D6): every rate explicit. A cache write
  // prices at ONE rate whatever TTL the record declares (or fails to
  // declare), so c5 and c1h carry the same value. Kimi bills cache_create
  // at a flat ZERO; Codex cache writes at 1.25x uncached input, reads at
  // 0.1x. GPT-6 Sol and Luna are not in codexmeter (D6). Keys are in
  // _normaliseModel form (dots folded to dashes): 'gpt-5.6-sol'
  // normalises to 'gpt-5-6-sol' before matching.
  'kimi-k3':           { fresh: 3,    c5: 0,     c1h: 0,     read: 0.3,   out: 15 },
  'kimi-k2-7-code':    { fresh: 0.95, c5: 0,     c1h: 0,     read: 0.19,  out: 4 },
  'kimi-k2-6':         { fresh: 0.95, c5: 0,     c1h: 0,     read: 0.16,  out: 4 },
  'gpt-6-astra':       { fresh: 10,   c5: 12.5,  c1h: 12.5,  read: 1,     out: 50 },
  'gpt-6-sol':         { fresh: 2,    c5: 2.5,   c1h: 2.5,   read: 0.2,   out: 10 },
  'gpt-6-luna':        { fresh: 0.1,  c5: 0.125, c1h: 0.125, read: 0.01,  out: 0.5 },
  'gpt-5-6-sol':       { fresh: 4,    c5: 5,     c1h: 5,     read: 0.4,   out: 20 },
  'gpt-5-6-terra':     { fresh: 2,    c5: 2.5,   c1h: 2.5,   read: 0.2,   out: 12 },
  'gpt-5-6-luna':      { fresh: 0.2,  c5: 0.25,  c1h: 0.25,  read: 0.02,  out: 1.2 },
  // Fable 5.1 / Mythos 5.1 price cache HITS at 0.025x base input, not the
  // 0.1x every other model uses — reads are 0.25, a quarter of Fable 5's.
  'claude-fable-5-1':  { fresh: 10,   c5: 12.5,  c1h: 20,   read: 0.25, out: 50 },
  'claude-mythos-5-1': { fresh: 10,   c5: 12.5,  c1h: 20,   read: 0.25, out: 50 },
  'claude-fable-5':    { fresh: 10,   c5: 12.5,  c1h: 20,   read: 1,    out: 50 },
  'claude-mythos-5':   { fresh: 10,   c5: 12.5,  c1h: 20,   read: 1,    out: 50 },
  // Opus 5.5 prices cache HITS at 0.05x base input (0.20 on a 4.00 base).
  'claude-opus-5-5':   { fresh: 4,    c5: 5,     c1h: 8,    read: 0.2,  out: 20 },
  'claude-opus-5':     { fresh: 5,    c5: 6.25,  c1h: 10,   read: 0.5,  out: 25 },
  'claude-opus-4-8':   { fresh: 5,    c5: 6.25,  c1h: 10,   read: 0.5,  out: 25 },
  'claude-opus-4-7':   { fresh: 5,    c5: 6.25,  c1h: 10,   read: 0.5,  out: 25 },
  'claude-opus-4-6':   { fresh: 5,    c5: 6.25,  c1h: 10,   read: 0.5,  out: 25 },
  'claude-opus-4-5':   { fresh: 5,    c5: 6.25,  c1h: 10,   read: 0.5,  out: 25 },
  'claude-opus-4-1':   { fresh: 15,   c5: 18.75, c1h: 30,   read: 1.5,  out: 75 },
  'claude-opus-4':     { fresh: 15,   c5: 18.75, c1h: 30,   read: 1.5,  out: 75 },
  'claude-sonnet-5':   { fresh: 2,    c5: 2.5,   c1h: 4,    read: 0.2,  out: 10 },
  'claude-sonnet-4-6': { fresh: 3,    c5: 3.75,  c1h: 6,    read: 0.3,  out: 15 },
  'claude-sonnet-4-5': { fresh: 3,    c5: 3.75,  c1h: 6,    read: 0.3,  out: 15 },
  'claude-sonnet-4':   { fresh: 3,    c5: 3.75,  c1h: 6,    read: 0.3,  out: 15 },
  'claude-haiku-4-5':  { fresh: 1,    c5: 1.25,  c1h: 2,    read: 0.1,  out: 5 },
  'claude-3-7-sonnet-':{ fresh: 3,    c5: 3.75,  c1h: 6,    read: 0.3,  out: 15 },
  'claude-3-5-sonnet-':{ fresh: 3,    c5: 3.75,  c1h: 6,    read: 0.3,  out: 15 },
  'claude-3-5-haiku-': { fresh: 0.8,  c5: 1.0,   c1h: 1.6,  read: 0.08, out: 4 },
  'claude-3-opus-':    { fresh: 15,   c5: 18.75, c1h: 30,   read: 1.5,  out: 75 },
  'claude-3-haiku-':   { fresh: 0.25, c5: 0.30,  c1h: 0.50, read: 0.03, out: 1.25 },
};

// Every rate an OpenRouter free model carries: zero. Returned for any id
// ending in ':free' or starting with 'stealth/' — see _isFreeModel.
// Mirrors backend/pricing.py FREE_RATES (SV-PARSER-SPEC); deliberately
// NOT a window.modelRates row, so it is not enumerable as an exact key.
window.FREE_RATES = { fresh: 0, c5: 0, c1h: 0, read: 0, out: 0 };
// Dated overrides, per exact key. Mirrors pricing.DATED_RATES.
// GLM-5.3-Flash launch promotion: 50% off list through 2026-09-09 24:00
// UTC+8 (= 16:00 UTC); month is 0-based in Date.UTC.
//
// The GPT-5.6 repricings are ported from codexmeter, as frozen UTC
// instants — NOT live expressions (month is 0-based in Date.UTC):
//   JUL30_CUT 2026-07-30 18:12 UTC — luna -80%, terra -20%; sol untouched
//   AUG21_CUT 2026-08-21 19:40 UTC — sol -20% in / -33% out; rest untouched
window.datedRates = {
  'glm-5-3-flash': [
    { endExclusive: Date.UTC(2026, 8, 9, 16, 0, 0),
      rates: { fresh: 0.075, c5: 0, c1h: 0, read: 0.015, out: 0.25 } },
  ],
  'gpt-5-6-sol': [
    { endExclusive: Date.UTC(2026, 7, 21, 19, 40, 0),
      rates: { fresh: 5, c5: 6.25, c1h: 6.25, read: 0.5, out: 30 } },
  ],
  'gpt-5-6-terra': [
    { endExclusive: Date.UTC(2026, 6, 30, 18, 12, 0),
      rates: { fresh: 2.5, c5: 3.125, c1h: 3.125, read: 0.25, out: 15 } },
  ],
  'gpt-5-6-luna': [
    { endExclusive: Date.UTC(2026, 6, 30, 18, 12, 0),
      rates: { fresh: 1, c5: 1.25, c1h: 1.25, read: 0.1, out: 6 } },
  ],
};
// Per-provider rates, keyed by normalised model id then provider (the
// transcript's message.provider spelling). Mirrors pricing.PROVIDER_RATES:
// OpenRouter's endpoints API, fetched 2026-09-24 22:03:13 UTC, promotional
// discounts already applied ("N% off"), none with a published end date.
// Rows are [model, provider, input, cache_read, output]. Every endpoint
// lists cache_write 0 (no separate write price), so both create buckets
// carry the input rate. A provider with two differently priced endpoints
// carries the dearer one: the transcript names only the host.
window.providerRates = {};
for (const [model, host, fresh, read, out] of [
  // z-ai/glm-5.3-flash
  ['z-ai/glm-5-3-flash', 'InferenceNet', 0.045, 0.01, 0.14],  // 50% off
  ['z-ai/glm-5-3-flash', 'Sail Research', 0.045, 0.0285, 0.6],
  ['z-ai/glm-5-3-flash', 'Relace', 0.07, 0.02, 0.28],
  ['z-ai/glm-5-3-flash', 'DeepInfra', 0.075, 0.015, 0.25],  // 50% off
  ['z-ai/glm-5-3-flash', 'Wafer', 0.089, 0.03, 0.35],
  ['z-ai/glm-5-3-flash', 'GMICloud', 0.09, 0.018, 0.3],  // 40% off
  ['z-ai/glm-5-3-flash', 'Morph', 0.098, 0.0196, 0.343],  // 2% off
  ['z-ai/glm-5-3-flash', 'OpenInference', 0.1, 0.025, 0.5],
  ['z-ai/glm-5-3-flash', 'Decart', 0.1275, 0.0255, 0.425],  // 15% off
  ['z-ai/glm-5-3-flash', 'Phala', 0.1275, 0.0255, 0.425],  // 15% off
  ['z-ai/glm-5-3-flash', 'Novita', 0.132, 0.0264, 0.44],  // 12% off
  ['z-ai/glm-5-3-flash', 'StreamLake', 0.141, 0.0282, 0.47],  // 6% off
  ['z-ai/glm-5-3-flash', 'Io Net', 0.1425, 0.0285, 0.475],  // 5% off
  ['z-ai/glm-5-3-flash', 'AtlasCloud', 0.15, 0.03, 0.5],
  ['z-ai/glm-5-3-flash', 'BaseTen', 0.15, 0.03, 0.5],
  ['z-ai/glm-5-3-flash', 'CoreWeave', 0.15, 0.05, 0.5],
  ['z-ai/glm-5-3-flash', 'Crusoe', 0.15, 0.03, 0.5],
  ['z-ai/glm-5-3-flash', 'DigitalOcean', 0.15, 0.03, 0.5],
  ['z-ai/glm-5-3-flash', 'Fireworks', 0.15, 0.03, 0.5],
  ['z-ai/glm-5-3-flash', 'Friendli', 0.15, 0.03, 0.5],
  ['z-ai/glm-5-3-flash', 'Inceptron', 0.15, 0.07, 0.5],
  ['z-ai/glm-5-3-flash', 'Modal', 0.15, 0.03, 0.5],
  ['z-ai/glm-5-3-flash', 'Near AI', 0.15, 0.035, 0.5],
  ['z-ai/glm-5-3-flash', 'Parasail', 0.15, 0.03, 0.5],
  ['z-ai/glm-5-3-flash', 'Reka', 0.15, 0.03, 0.5],
  ['z-ai/glm-5-3-flash', 'SiliconFlow', 0.15, 0.03, 0.5],
  ['z-ai/glm-5-3-flash', 'Together', 0.15, 0.03, 0.5],
  ['z-ai/glm-5-3-flash', 'Venice', 0.15, 0.03, 0.5],
  ['z-ai/glm-5-3-flash', 'Z.AI', 0.15, 0.03, 0.5],
  ['z-ai/glm-5-3-flash', 'NextBit', 0.165, 0.033, 0.55],
  ['z-ai/glm-5-3-flash', 'Cloudflare', 0.3, 0.03, 1.0],
  // deepseek/deepseek-v4.1-flash
  ['deepseek/deepseek-v4-1-flash', 'DekaLLM', 0.04, 0.01, 1.0],
  ['deepseek/deepseek-v4-1-flash', 'Morph', 0.075, 0.0015, 0.3],  // 50% off
  ['deepseek/deepseek-v4-1-flash', 'OpenInference', 0.1, 0.01, 0.5],
  ['deepseek/deepseek-v4-1-flash', 'Relace', 0.1, 0.01, 0.5],
  ['deepseek/deepseek-v4-1-flash', 'Sail Research', 0.13, 0.01, 0.75],
  ['deepseek/deepseek-v4-1-flash', 'DeepInfra', 0.14, 0.0042, 0.42],  // 30% off
  ['deepseek/deepseek-v4-1-flash', 'Alibaba', 0.15, 0.015, 0.6],
  ['deepseek/deepseek-v4-1-flash', 'DeepSeek', 0.15, 0.003, 0.6],
  ['deepseek/deepseek-v4-1-flash', 'StreamLake', 0.165, 0.0033, 0.66],  // 45% off
  ['deepseek/deepseek-v4-1-flash', 'CoreWeave', 0.2, 0.03, 0.65],
  ['deepseek/deepseek-v4-1-flash', 'Wafer', 0.2, 0.006, 0.6],
  ['deepseek/deepseek-v4-1-flash', 'Fireworks', 0.22, 0.007, 0.66],
  ['deepseek/deepseek-v4-1-flash', 'GMICloud', 0.225, 0.0045, 0.9],  // 25% off
  ['deepseek/deepseek-v4-1-flash', 'Krea', 0.225, 0.006, 0.9],
  ['deepseek/deepseek-v4-1-flash', 'Phala', 0.276, 0.00552, 1.104],  // 20% off
  ['deepseek/deepseek-v4-1-flash', 'Novita', 0.285, 0.0057, 1.14],  // 5% off
  ['deepseek/deepseek-v4-1-flash', 'AtlasCloud', 0.3, 0.03, 1.2],
  ['deepseek/deepseek-v4-1-flash', 'BaseTen', 0.3, 0.007, 1.2],
  ['deepseek/deepseek-v4-1-flash', 'DigitalOcean', 0.3, 0.006, 1.2],
  ['deepseek/deepseek-v4-1-flash', 'Makora', 0.3, 0.006, 1.2],
  ['deepseek/deepseek-v4-1-flash', 'Modal', 0.3, 0.03, 1.2],
  ['deepseek/deepseek-v4-1-flash', 'NextBit', 0.3, 0.006, 1.2],
  ['deepseek/deepseek-v4-1-flash', 'Parasail', 0.3, 0.006, 1.2],
  ['deepseek/deepseek-v4-1-flash', 'SiliconFlow', 0.3, 0.006, 1.2],
  ['deepseek/deepseek-v4-1-flash', 'Together', 0.3, 0.006, 1.2],
  ['deepseek/deepseek-v4-1-flash', 'Venice', 0.375, 0.0075, 1.5],
  // stealth/space-bunny-alpha
  ['stealth/space-bunny-alpha', 'Stealth', 0.0, 0.0, 0.0],
  // deepseek/deepseek-v4-flash-0731
  ['deepseek/deepseek-v4-flash-0731', 'Relace', 0.03, 0.016, 0.32],
  ['deepseek/deepseek-v4-flash-0731', 'Sail Research', 0.038, 0.0228, 0.55],
  ['deepseek/deepseek-v4-flash-0731', 'StreamLake', 0.0528, 0.00168, 0.1584],  // 88% off
  ['deepseek/deepseek-v4-flash-0731', 'DeepInfra', 0.06, 0.015, 0.18],
  ['deepseek/deepseek-v4-flash-0731', 'Wafer', 0.08, 0.02, 0.35],
  ['deepseek/deepseek-v4-flash-0731', 'Inceptron', 0.0828, 0.06, 0.4138],
  ['deepseek/deepseek-v4-flash-0731', 'Reka', 0.088, 0.0056, 0.528],  // 20% off
  ['deepseek/deepseek-v4-flash-0731', 'Makora', 0.09, 0.0196, 0.195],
  ['deepseek/deepseek-v4-flash-0731', 'DigitalOcean', 0.119, 0.0238, 0.238],
  ['deepseek/deepseek-v4-flash-0731', 'BaseTen', 0.13, 0.028, 0.26],
  ['deepseek/deepseek-v4-flash-0731', 'CoreWeave', 0.13, 0.07, 0.28],
  ['deepseek/deepseek-v4-flash-0731', 'Cohere', 0.14, 0.07, 0.28],
  ['deepseek/deepseek-v4-flash-0731', 'Nebius', 0.14, 0.0, 0.28],
  ['deepseek/deepseek-v4-flash-0731', 'OpenInference', 0.14, 0.03, 0.7],
  ['deepseek/deepseek-v4-flash-0731', 'Parasail', 0.14, 0.05, 0.28],
  ['deepseek/deepseek-v4-flash-0731', 'Together', 0.14, 0.03, 0.28],
  ['deepseek/deepseek-v4-flash-0731', 'Morph', 0.141953, 0.035937, 0.399625],
  ['deepseek/deepseek-v4-flash-0731', 'Venice', 0.175, 0.035, 0.35],
  ['deepseek/deepseek-v4-flash-0731', 'Alibaba', 0.176, 0.0176, 0.528],
  ['deepseek/deepseek-v4-flash-0731', 'Mancer 2', 0.2, 0.0, 0.6],
  ['deepseek/deepseek-v4-flash-0731', 'Fireworks', 0.22, 0.007, 0.66],
  ['deepseek/deepseek-v4-flash-0731', 'SiliconFlow', 0.22, 0.028, 0.66],
  ['deepseek/deepseek-v4-flash-0731', 'GMICloud', 0.286, 0.0091, 0.858],  // 35% off
  ['deepseek/deepseek-v4-flash-0731', 'Phala', 0.308, 0.0196, 0.924],  // 30% off
  ['deepseek/deepseek-v4-flash-0731', 'NextBit', 0.352, 0.012, 1.056],
  ['deepseek/deepseek-v4-flash-0731', 'Novita', 0.4092, 0.02604, 1.2276],  // 7% off
  ['deepseek/deepseek-v4-flash-0731', 'AtlasCloud', 0.44, 0.028, 1.32],
  ['deepseek/deepseek-v4-flash-0731', 'Baidu', 0.44, 0.014, 1.32],
  ['deepseek/deepseek-v4-flash-0731', 'Cloudflare', 0.44, 0.014, 1.32],
  // deepseek/deepseek-v4-flash
  ['deepseek/deepseek-v4-flash', 'Relace', 0.05, 0.01, 0.25],
  ['deepseek/deepseek-v4-flash', 'StreamLake', 0.06398, 0.012796, 0.12796],  // 54% off
  ['deepseek/deepseek-v4-flash', 'Baidu', 0.06538, 0.013076, 0.13076],  // 53% off
  ['deepseek/deepseek-v4-flash', 'DeepInfra', 0.09, 0.018, 0.18],
  ['deepseek/deepseek-v4-flash', 'GMICloud', 0.091, 0.0182, 0.182],  // 35% off
  ['deepseek/deepseek-v4-flash', 'Venice', 0.0966, 0.0196, 0.1925],  // 30% off
  ['deepseek/deepseek-v4-flash', 'DigitalOcean', 0.098, 0.0196, 0.196],
  ['deepseek/deepseek-v4-flash', 'SiliconFlow', 0.13, 0.028, 0.28],
  ['deepseek/deepseek-v4-flash', 'Alibaba', 0.134, 0.0268, 0.268],
  ['deepseek/deepseek-v4-flash', 'AtlasCloud', 0.14, 0.028, 0.28],
  ['deepseek/deepseek-v4-flash', 'Novita', 0.14, 0.028, 0.28],
  ['deepseek/deepseek-v4-flash', 'OpenInference', 0.14, 0.03, 0.7],
  ['deepseek/deepseek-v4-flash', 'Parasail', 0.14, 0.07, 0.28],
  ['deepseek/deepseek-v4-flash', 'NextBit', 0.15, 0.035, 0.3],
  ['deepseek/deepseek-v4-flash', 'Mancer 2', 0.19, 0.0, 0.5],
  ['deepseek/deepseek-v4-flash', 'Azure', 0.21, 0.031, 0.56],

]) {
  (window.providerRates[model] = window.providerRates[model] || {})[host] =
    { fresh, c5: fresh, c1h: fresh, read, out };
}
// Dated overrides per model then provider. Mirrors
// pricing.PROVIDER_DATED_RATES (empty); a window added here also adds its
// boundary to window.rateEpochs below.
window.providerDatedRates = {};
window.rateEpochs = [
  Date.UTC(2026, 6, 30, 18, 12, 0),
  Date.UTC(2026, 7, 21, 19, 40, 0),
  Date.UTC(2026, 8, 9, 16, 0, 0),
];

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

function _matchRateKey(norm) {
  for (const k of Object.keys(window.modelRates)) {
    if (!norm.startsWith(k)) continue;
    const rest = norm.slice(k.length);
    if (rest === '' || rest[0] === '[' || rest[0] === '@' || _SNAPSHOT_SUFFIX.test(rest)) return k;
  }
  return null;
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
// the same model as its slug ('deepseek/deepseek-v4-flash-0731'). Exact
// match only, never _SNAPSHOT_SUFFIX: that would read the permaslug as the
// UNDATED model. Mirrors pricing._provider_key.
const _PERMASLUG_DATE = /-20\d{2}(\d{4})$/;
function _providerModelKey(norm, provider) {
  for (const m of [norm, norm.replace(_PERMASLUG_DATE, '-$1')]) {
    const hosts = window.providerRates[m];
    if (hosts && Object.prototype.hasOwnProperty.call(hosts, provider)) return m;
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
  const pkey = provider ? _providerModelKey(norm, provider) : null;
  if (pkey) {
    const windows = (window.providerDatedRates[pkey] || {})[provider];
    return { rates: _inWindow(windows, ts, window.providerRates[pkey][provider]),
             kind: 'exact', key: pkey };
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
