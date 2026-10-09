// Browser parsers for Codex and both Kimi wire formats. Codex logic is in
// parser-codex.js; this file owns sniffing, shared helpers and Kimi parsing.
// Keep lane records and pricing aligned with backend parsing (SV-PARSER-SPEC).
//
// Global Codex meter defaults; per-model entries can override them.
window.LONG_CONTEXT_THRESHOLD = 272000;
window.LONG_CONTEXT_INPUT_MULT = 2.0;
window.LONG_CONTEXT_OUTPUT_MULT = 1.5;
// The model's complete meter entry, shared by threshold and factor lookups.
function _longContextMeterFor(model) {
  const norm = String(model || '').trim().toLowerCase();
  const key = (norm.indexOf('claude') > 0
    ? norm.slice(norm.indexOf('claude')) : norm).replace(/\./g, '-');
  return (window.longContextMeters || {})[key];
}

// Per-model thresholds (issue #765): mirrors pricing.long_context_threshold.
window.longContextThresholdFor = function (model) {
  const meter = _longContextMeterFor(model);
  return (meter && meter.threshold) || window.LONG_CONTEXT_THRESHOLD;
};
// Per-model factors (issue #878): omitted fields keep the global defaults.
window.longContextFactorsFor = function (model) {
  const meter = _longContextMeterFor(model) || {};
  return [
    meter.input_mult ?? window.LONG_CONTEXT_INPUT_MULT,
    meter.output_mult ?? window.LONG_CONTEXT_OUTPUT_MULT,
  ];
};

// Context Growth drops cumulative counters above this derived-series bound.
// Mirror backend.constants.MAX_PLAUSIBLE_CTX; tests pin both values.
window.MAX_PLAUSIBLE_CTX = 2000000;

// The shared helpers src/parser-codex.js (the codex lane, split out of
// this file) consume; the window exposure is the seam between the two
// files, and this file must load first.
window.laneIsPlainObject = window.laneIsPlainObject || laneIsPlainObject;
window.laneInt = window.laneInt || laneInt;
window.laneToIso = window.laneToIso || laneToIso;
window.laneUsageMeta = window.laneUsageMeta || laneUsageMeta;
window.lanePairToolEvents = window.lanePairToolEvents || lanePairToolEvents;

// --------------------------------------------------------------------------
// Shared helpers
// --------------------------------------------------------------------------

function laneIsPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

// Any timestamp the lane formats carry -> an ISO string ('' when absent or
// unparsable). Numbers are epoch SECONDS (legacy Kimi); strings (Codex,
// legacy ISO variants) go through Date.parse — an offset-less ISO string
// stamped UTC first (issue #376, mirrors parse_common._to_dt: unstamped,
// Date.parse reads the viewer's zone); kimi-code's epoch-ms `time` is
// converted by its caller upstream.
function laneToIso(ts) {
  if (ts == null || ts === '') return '';
  const ms = typeof ts === 'number' ? ts * 1000 : Date.parse(/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(ts) ? ts + 'Z' : ts);
  return Number.isNaN(ms) ? '' : new Date(ms).toISOString();
}

// A token count the way the backend lane parsers read one:
// int(payload.get(key) or 0) — a missing or non-numeric bucket is 0,
// never NaN or undefined.
function laneInt(v) {
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}

// One billing record, in the Claude parser's meta shape. `uuid` mirrors
// the backend's records.uuid (used only for optional cross-file dedup —
// the Inspector loads one file at a time).
function laneUsageMeta(line, tsIso, uuid, model, fresh, create, read, output, longContext) {
  return {
    line,
    type: 'assistant_usage',
    ts: tsIso,
    model,
    requestId: '',
    uuid: uuid || '',
    sessionId: '',
    usage: {
      input_tokens: fresh,
      cache_creation_input_tokens: create,
      cache_read_input_tokens: read,
      output_tokens: output,
    },
    long_context: !!longContext,
  };
}

// Link tool_call <-> tool_result the way the Claude parser does, so
// ToolCallDetail shows a call's result and eventOneLine previews it.
function lanePairToolEvents(events) {
  const callMap = new Map();
  for (const e of events) {
    if (e.type === 'tool_call' && e.tool_use_id) callMap.set(e.tool_use_id, e);
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
}

// --------------------------------------------------------------------------
// Format sniffing — backend/parse_lanes.sniff_format, rung for rung
// --------------------------------------------------------------------------

const LANE_CODEX_RECORD_TYPES = {
  session_meta: true,
  turn_context: true,
  event_msg: true,
  response_item: true,
  world_state: true,
  compacted: true,
  inter_agent_communication_metadata: true,
};
const LANE_KIMI_CODE_TYPES = [
  'context.append_message', 'context.append_loop_event',
  'usage.record', 'turn.prompt', 'turn.steer',
];
const LANE_LEGACY_MSG_TYPES = ['StatusUpdate', 'TurnBegin', 'ToolCall', 'ContentPart'];
const LANE_CLAUDE_UNKEYED_TYPES = ['file-history-snapshot', 'file-history-delta'];

window.sniffTranscriptFormat = function sniffTranscriptFormat(blob) {
  const lines = String(blob).split(/\r?\n/);
  for (const line of lines) {
    if (!line.trim()) continue;
    let obj;
    try { obj = JSON.parse(line); } catch { continue; }
    if (!laneIsPlainObject(obj)) continue;
    // Codex first: its records also carry a "timestamp", so a later rung
    // must not claim one. The pairing of a record type with a dict payload
    // is what identifies the format.
    if (LANE_CODEX_RECORD_TYPES[obj.type] && laneIsPlainObject(obj.payload)) return 'codex';
    if (obj.type === 'metadata') {
      // `in`, not a null check: a null created_at still names the
      // kimi-code format (backend parse_lanes.py).
      return 'created_at' in obj ? 'kimi-code' : 'legacy';
    }
    if (LANE_KIMI_CODE_TYPES.includes(obj.type)) return 'kimi-code';
    // The isinstance guard matters: kimi-code's llm.error carries a STRING
    // message, and a Claude line's message can be a string too.
    if (laneIsPlainObject(obj.message)
        && LANE_LEGACY_MSG_TYPES.includes(obj.message.type)) return 'legacy';
    // Last rung, backend order kept: a Claude line carries no lane marker.
    if (LANE_CLAUDE_UNKEYED_TYPES.includes(obj.type)
        || typeof obj.sessionId === 'string') return 'claude';
  }
  // No rung identified a lane format: the claude path parses the blob.
  return 'claude';
};

// --------------------------------------------------------------------------
// The Kimi model ladder — backend/parse_kimi.py, verbatim
// --------------------------------------------------------------------------

// 2026-06-11 22:30:35 UTC  k2-6 -> k2-7-code; 2026-07-16 14:45:55 UTC the
// earliest observed k3 usage.record (that rung only applies to records with
// no wire model string).
const LANE_MODEL_CUTOFF_EPOCH = 1781217035;
const LANE_K3_CUTOFF_EPOCH = 1784213155;
// Only ids that identify a pricing model on their own; "kimi-for-coding"
// spans both k2 generations and deliberately does not.
const LANE_WIRE_MODEL_MAP = { k3: 'kimi-k3' };

function laneCanonicalModel(wireModel) {
  if (!wireModel) return null;
  const tail = String(wireModel).split('/').pop();
  return LANE_WIRE_MODEL_MAP[tail] || null;
}

// Wire first, dates only for what the wire cannot express. Per RECORD, so a
// session that switches model mid-flight splits correctly.
function laneModelFor(wireModel, epochSec) {
  const canonical = laneCanonicalModel(wireModel);
  if (canonical !== null) return canonical;
  if (epochSec == null) return 'kimi-k2-7-code';
  if (epochSec < LANE_MODEL_CUTOFF_EPOCH) return 'kimi-k2-6';
  // The wire named a model and it is not k3, so the date may only separate
  // the k2 generations — never promote an unrecognized id to k3.
  if (wireModel) return 'kimi-k2-7-code';
  if (epochSec < LANE_K3_CUTOFF_EPOCH) return 'kimi-k2-7-code';
  return 'kimi-k3';
}

// --------------------------------------------------------------------------
// Legacy kimi-cli wire format — backend/parse_kimi.parse_legacy
// --------------------------------------------------------------------------

function parseLaneLegacy(blob) {
  const lines = String(blob).split(/\r?\n/);
  const events = [];
  const meta = [];
  let firstEventTs = null; // epoch seconds; a record with no ts borrows it

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line) continue;
    let obj;
    try { obj = JSON.parse(line); } catch { continue; }
    if (!laneIsPlainObject(obj)) continue;

    const ts = obj.timestamp;
    // An unparsable timestamp is NO timestamp (backend _to_dt returns
    // None), so the model ladder falls back — NaN must not slip through
    // and date-compare false against every cutoff.
    let epochSec;
    if (typeof ts === 'number') {
      epochSec = ts;
    } else if (ts) {
      const ms = Date.parse(laneToIso(ts));
      epochSec = Number.isNaN(ms) ? null : ms / 1000;
    } else {
      epochSec = null;
    }
    if (epochSec != null && firstEventTs == null) {
      firstEventTs = epochSec;
    }
    const tsIso = laneToIso(ts);
    const lineNum = i + 1;
    const msg = laneIsPlainObject(obj.message) ? obj.message : {};
    const msgType = msg.type || '';
    const payload = laneIsPlainObject(msg.payload) ? msg.payload : {};

    if (msgType === 'TurnBegin') {
      let userInput = payload.user_input || '';
      if (Array.isArray(userInput)) {
        userInput = userInput
          .map(p => (laneIsPlainObject(p) ? (p.text || '') : '')).join(' ');
      }
      events.push({ line: lineNum, type: 'user_message', ts: tsIso, detail: String(userInput) });
      continue;
    }
    // TurnEnd: a bookkeeping boundary the Inspector has no row for.
    if (msgType === 'TurnEnd') continue;
    if (msgType === 'ContentPart') {
      if (payload.type === 'text') {
        events.push({ line: lineNum, type: 'assistant_text', ts: tsIso, detail: String(payload.text || '') });
      } else if (payload.type === 'think') {
        events.push({ line: lineNum, type: 'thinking', ts: tsIso, detail: String(payload.think || '') });
      }
      continue;
    }
    if (msgType === 'ToolCall') {
      const func = laneIsPlainObject(payload.function) ? payload.function : {};
      // Backend _args_to_dict: a dict passes through as is; a string is
      // JSON.parse'd (failure, or a parsed non-dict, becomes {}); any
      // other shape is {}.
      let toolInput = {};
      if (laneIsPlainObject(func.arguments)) {
        toolInput = func.arguments;
      } else if (typeof func.arguments === 'string' && func.arguments) {
        try {
          const parsed = JSON.parse(func.arguments);
          if (laneIsPlainObject(parsed)) toolInput = parsed;
        } catch { /* unparsable arguments: an empty input, not a dropped call */ }
      }
      events.push({
        line: lineNum, type: 'tool_call', ts: tsIso,
        tool_name: func.name || '',
        tool_input: toolInput,
        tool_use_id: String(payload.id || ''),
      });
      continue;
    }
    if (msgType === 'ToolResult') {
      const rv = laneIsPlainObject(payload.return_value) ? payload.return_value : {};
      const out = rv.output;
      let detail = '';
      if (Array.isArray(out)) {
        detail = out.map(x => (laneIsPlainObject(x) ? (x.text || '') : String(x))).join('\n');
      } else {
        detail = String(out || '');
      }
      events.push({
        line: lineNum, type: 'tool_result', ts: tsIso,
        tool_use_id: String(payload.tool_call_id || ''),
        is_error: !!rv.is_error,
        detail,
      });
      continue;
    }
    if (msgType === 'StatusUpdate') {
      const tu = laneIsPlainObject(payload.token_usage) ? payload.token_usage : null;
      if (!tu || Object.keys(tu).length === 0) continue;
      // Legacy transcripts carry no model string anywhere; dates decide.
      const model = laneModelFor(null, epochSec != null ? epochSec : firstEventTs);
      meta.push(laneUsageMeta(
        lineNum, tsIso, payload.message_id || null, model,
        laneInt(tu.input_other), laneInt(tu.input_cache_creation),
        laneInt(tu.input_cache_read), laneInt(tu.output), false));
    }
  }

  lanePairToolEvents(events);
  return { events, meta };
}

// --------------------------------------------------------------------------
// kimi-code wire format — backend/parse_kimi.parse_kimi_code
// --------------------------------------------------------------------------

function laneKcParseToolCall(tc) {
  if (!laneIsPlainObject(tc) || tc.type !== 'function') return { name: '', args: null, id: '' };
  const id = tc.id || '';
  if ('name' in tc) return { name: tc.name || '', args: tc.arguments, id };
  const fn = laneIsPlainObject(tc.function) ? tc.function : {};
  return { name: fn.name || '', args: fn.arguments, id };
}

// Backend _kc_args_to_input: an unparsable string is carried as {_raw}.
function laneKcArgsToInput(args) {
  if (args == null) return {};
  if (laneIsPlainObject(args)) return args;
  if (typeof args === 'string') {
    try { return args ? JSON.parse(args) : {}; }
    catch { return { _raw: args }; }
  }
  return { _raw: args };
}

function laneResultTextParts(parts) {
  const text = [];
  for (const part of parts) {
    if (typeof part === 'string') {
      text.push(part);
    } else if (laneIsPlainObject(part) && part.type === 'text') {
      text.push(String(part.text || ''));
    }
  }
  return text;
}

function laneKcResultDetail(result) {
  if (!laneIsPlainObject(result)) return String(result || '');
  const output = result.output;
  if (Array.isArray(output)) {
    return laneResultTextParts(output).join('\n');
  }
  return String(output || '');
}

function parseLaneKimiCode(blob, opts) {
  const lines = String(blob).split(/\r?\n/);
  const events = [];
  const meta = [];
  const fileKey = (opts && opts.fileKey) || '';
  let firstEventTs = null; // epoch seconds

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line) continue;
    let obj;
    try { obj = JSON.parse(line); } catch { continue; }
    if (!laneIsPlainObject(obj)) continue;

    const typ = obj.type;
    if (typ === 'metadata') {
      if (obj.created_at && firstEventTs == null) firstEventTs = obj.created_at / 1000;
      continue;
    }
    const tsMs = obj.time;
    const epochSec = tsMs ? tsMs / 1000 : null;
    if (epochSec != null && firstEventTs == null) firstEventTs = epochSec;
    const tsIso = tsMs ? new Date(tsMs).toISOString() : '';
    const lineNum = i + 1;

    if (typ === 'turn.prompt' || typ === 'turn.steer') {
      const inputParts = Array.isArray(obj.input) ? obj.input : [];
      const text = inputParts
        .map(p => (laneIsPlainObject(p) ? (p.text || '') : '')).join('');
      events.push({ line: lineNum, type: 'user_message', ts: tsIso, detail: text });
      continue;
    }

    if (typ === 'context.append_message') {
      const msg = laneIsPlainObject(obj.message) ? obj.message : {};
      const role = msg.role;
      const content = Array.isArray(msg.content) ? msg.content : [];
      if (role === 'assistant') {
        for (const p of content) {
          if (!laneIsPlainObject(p)) continue;
          if (p.type === 'text') {
            events.push({ line: lineNum, type: 'assistant_text', ts: tsIso, detail: String(p.text || '') });
          } else if (p.type === 'think') {
            events.push({ line: lineNum, type: 'thinking', ts: tsIso, detail: String(p.think || '') });
          }
        }
        // The wire's parallel-call representation: one assistant message
        // carrying several toolCalls. Annotated so computeSessionStats'
        // parallel-batches count sees real data.
        const msgToolCalls = Array.isArray(msg.toolCalls)
          ? msg.toolCalls.filter(laneIsPlainObject) : [];
        msgToolCalls.forEach((tc, tcIdx) => {
          const { name, args, id } = laneKcParseToolCall(tc);
          const ev = {
            line: lineNum, type: 'tool_call', ts: tsIso,
            tool_name: name,
            tool_input: laneKcArgsToInput(args),
            tool_use_id: String(id || ''),
          };
          if (msgToolCalls.length > 1) {
            ev.batch_size = msgToolCalls.length;
            ev.batch_index = tcIdx + 1;
          }
          events.push(ev);
        });
      } else if (role === 'tool') {
        const detail = laneResultTextParts(content).join('');
        events.push({
          line: lineNum, type: 'tool_result', ts: tsIso,
          tool_use_id: String(msg.toolCallId || ''),
          is_error: !!msg.isError,
          detail,
        });
      } else if (role === 'user') {
        const text = content
          .map(p => (laneIsPlainObject(p) ? (p.text || '') : '')).join('');
        events.push({ line: lineNum, type: 'user_message', ts: tsIso, detail: text });
      }
      continue;
    }

    if (typ === 'context.append_loop_event') {
      const ev = laneIsPlainObject(obj.event) ? obj.event : {};
      const et = ev.type;
      if (et === 'content.part') {
        const part = laneIsPlainObject(ev.part) ? ev.part : {};
        if (part.type === 'text') {
          events.push({ line: lineNum, type: 'assistant_text', ts: tsIso, detail: String(part.text || '') });
        } else if (part.type === 'think') {
          events.push({ line: lineNum, type: 'thinking', ts: tsIso, detail: String(part.think || '') });
        }
      } else if (et === 'tool.call') {
        events.push({
          line: lineNum, type: 'tool_call', ts: tsIso,
          tool_name: ev.name || '',
          tool_input: laneKcArgsToInput(ev.args),
          tool_use_id: String(ev.toolCallId || ''),
        });
      } else if (et === 'tool.result') {
        const res = laneIsPlainObject(ev.result) ? ev.result : {};
        events.push({
          line: lineNum, type: 'tool_result', ts: tsIso,
          tool_use_id: String(ev.toolCallId || ''),
          // isError only — the wire spells it one way (parse_kimi.py).
          is_error: !!res.isError,
          detail: laneKcResultDetail(res),
        });
      }
      continue;
    }

    if (typ === 'usage.record') {
      const usage = laneIsPlainObject(obj.usage) ? obj.usage : {};
      const model = laneModelFor(obj.model || null, epochSec != null ? epochSec : firstEventTs);
      meta.push(laneUsageMeta(
        lineNum, tsIso, `${fileKey}:${lineNum}`, model,
        laneInt(usage.inputOther), laneInt(usage.inputCacheCreation),
        laneInt(usage.inputCacheRead), laneInt(usage.output), false));
      continue;
    }

    // kimi-code journals every non-aborted provider failure as llm.error;
    // only a hard quota stop is a rate-limit hit (mirrors _kc_llm_error).
    if (typ === 'llm.error' && obj.kind === 'quota_exhausted') {
      meta.push({
        line: lineNum, type: 'rate_limit', ts: tsIso,
        content: String(obj.message || '').slice(0, 500),
      });
    }
  }

  lanePairToolEvents(events);
  return { events, meta };
}

// --------------------------------------------------------------------------
// Entry points wired into parseTranscript
// --------------------------------------------------------------------------

window.parseTranscriptLanes = function parseTranscriptLanes(blob, opts) {
  const fmt = window.sniffTranscriptFormat(blob);
  if (fmt === 'codex') return window.parseLaneCodex(blob, opts);
  if (fmt === 'kimi-code') return parseLaneKimiCode(blob, opts);
  if (fmt === 'legacy') return parseLaneLegacy(blob);
  return { events: [], meta: [] }; // claude — caller handles that itself
};
