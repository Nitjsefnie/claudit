// Browser parsers for the lane transcript formats: Codex rollouts and the
// two Kimi wire formats. Ported from backend/parse_codex.py,
// backend/parse_kimi.py and backend/parse_lanes.py so the Inspector's
// in-browser parse of a session transcript yields the same per-record
// token totals (and, through src/parser.js's shared rate table, the same
// cost) as backend.parse.parse_file — the same lockstep SV-PARSER-SPEC
// keeps src/parser.js in with backend/pricing.py.
//
// The lane parsers emit the SAME shapes the Claude parser emits, so every
// Inspector consumer (computeSessionStats, ContextGrowthView, txToDashData,
// detail-pane.jsx) works unchanged:
//   events: user_message / assistant_text / thinking / tool_call / tool_result
//   meta:   assistant_usage (usage.{input,cache_creation,cache_read,output}_tokens)
//           plus rate_limit entries
// Fields the Claude shape has and a lane format does not express (refs,
// toolUseResult, agent_spawn, iterations) are simply absent — every
// consumer treats them as falsy already.

// Codex bills a request whose prompt exceeds this many tokens on its
// long-context meter (2x input-side, 1.5x output — applied by
// src/parser.js's cost path and app.jsx's Token Breakdown off the
// row's long_context flag). Mirror pricing.LONG_CONTEXT_THRESHOLD and
// the LONG_CONTEXT_*_MULT pair;
// tests/test_parser_js_lanes.py asserts all three agree.
window.LONG_CONTEXT_THRESHOLD = 272000;
window.LONG_CONTEXT_INPUT_MULT = 2.0;
window.LONG_CONTEXT_OUTPUT_MULT = 1.5;

// --------------------------------------------------------------------------
// Shared helpers
// --------------------------------------------------------------------------

function laneIsPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

// Any timestamp the lane formats carry -> an ISO string ('' when absent or
// unparsable). Numbers are epoch SECONDS (legacy Kimi); strings (Codex,
// legacy ISO variants) go through Date.parse. kimi-code's epoch-ms `time`
// is converted by its caller before reaching this.
function laneToIso(ts) {
  if (ts == null || ts === '') return '';
  const ms = typeof ts === 'number' ? ts * 1000 : Date.parse(ts);
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
      const ms = Date.parse(ts);
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
      if (!tu) continue;
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

function laneKcResultDetail(result) {
  if (!laneIsPlainObject(result)) return String(result || '');
  const output = result.output;
  if (Array.isArray(output)) {
    return output.map(x => (laneIsPlainObject(x) ? (x.text || '') : String(x))).join('\n');
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
        const msgToolCalls = Array.isArray(msg.toolCalls) ? msg.toolCalls : [];
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
        const detail = content
          .map(p => (laneIsPlainObject(p) ? (p.text || '') : '')).join('');
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
// Codex rollout format — backend/parse_codex.py
// --------------------------------------------------------------------------

// `exec` is the only custom tool; the api it calls is the useful name.
const LANE_CODEX_API_RE = /tools\.([A-Za-z_][A-Za-z_0-9]*)\s*\(/g;
const LANE_CODEX_FLAGSHIP = 'gpt-6-astra';
// Substring needles, in order, most specific first: `gpt-6-sol` contains
// `sol`, so a later generic needle must not claim it.
const LANE_CODEX_MODEL_MAP = [
  ['astra', 'gpt-6-astra'],
  ['gpt-6-sol', 'gpt-6-sol'],
  ['gpt-6-luna', 'gpt-6-luna'],
  ['terra', 'gpt-5.6-terra'],
  ['luna', 'gpt-5.6-luna'],
  ['sol', 'gpt-5.6-sol'],
];
// The cumulative counter's fields; all six are differenced so a duplicate
// snapshot is recognised by ALL of them failing to advance.
const LANE_CODEX_USAGE_KEYS = [
  'input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
  'output_tokens', 'reasoning_output_tokens', 'total_tokens',
];
// A tool result whose first line starts with one of these is a failure.
const LANE_CODEX_FAILURE_HEADS = ['Script failed', 'collab spawn failed'];

// An unrecognised model bills at the flagship: a visible overcount beats a
// silent undercount (backend _codex_model).
function laneCodexModel(raw) {
  const lowered = String(raw || '').toLowerCase();
  for (const [needle, label] of LANE_CODEX_MODEL_MAP) {
    if (lowered.includes(needle)) return label;
  }
  return LANE_CODEX_FLAGSHIP;
}

// Flatten a *_call_output payload into one string (backend _codex_output_text).
function laneCodexOutputText(payload) {
  const out = payload.output;
  if (typeof out === 'string') return out;
  if (Array.isArray(out)) {
    const parts = [];
    for (const chunk of out) {
      if (laneIsPlainObject(chunk) && chunk.text) parts.push(String(chunk.text));
      else if (typeof chunk === 'string') parts.push(chunk);
    }
    return parts.join('\n');
  }
  return '';
}

function parseLaneCodex(blob, opts) {
  const lines = String(blob).split(/\r?\n/);
  const events = [];
  const meta = [];
  const fileKey = (opts && opts.fileKey) || '';

  // Cheap pre-scan: every model this file declares (backend
  // _codex_declared_models). A fork replays history before the new thread
  // declares a model; where the file declares exactly one there is only
  // one answer for those leading requests.
  const declared = new Set();
  for (const line of lines) {
    if (!line.includes('"turn_context"') && !line.includes('"thread_settings_applied"')) continue;
    let obj;
    try { obj = JSON.parse(line); } catch { continue; }
    if (!laneIsPlainObject(obj)) continue;
    const payload = laneIsPlainObject(obj.payload) ? obj.payload : {};
    let name = null;
    if (obj.type === 'turn_context') name = payload.model;
    else if (payload.type === 'thread_settings_applied') {
      name = (laneIsPlainObject(payload.thread_settings) ? payload.thread_settings : {}).model;
    }
    if (name) declared.add(String(name));
  }
  const st = {
    prevUsage: null,      // previous cumulative snapshot, for differencing
    model: null,          // model in force, from the latest declaration
    // The file's only declared model, when it declares exactly one.
    // Attributes the token_count records that precede the first
    // turn_context.
    soleModel: declared.size === 1 ? [...declared][0] : null,
    subscription: false,  // sticky: any plan_type on a token_count payload
    sessionId: null,      // this thread's id, from session_meta
    lastRlKind: null,     // last rate-limit condition booked
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line) continue;
    let obj;
    try { obj = JSON.parse(line); } catch { continue; }
    if (!laneIsPlainObject(obj)) continue;
    const payload = laneIsPlainObject(obj.payload) ? obj.payload : {};
    const tsIso = laneToIso(obj.timestamp);
    const lineNum = i + 1;

    const rtype = obj.type || '';
    if (rtype === 'event_msg') {
      const ptype = payload.type || '';
      if (ptype === 'token_count') {
        laneCodexRateLimit(st, meta, lineNum, tsIso, payload);
        laneCodexTokenCount(st, meta, fileKey, lineNum, tsIso, payload);
      } else if (ptype === 'agent_message') {
        events.push({ line: lineNum, type: 'assistant_text', ts: tsIso, detail: String(payload.message || '') });
      } else if (ptype === 'thread_settings_applied') {
        // A model switch can be carried by the settings record alone;
        // update the model in force from it exactly as from a
        // turn_context (backend _codex_event_msg), non-empty only.
        const settings = laneIsPlainObject(payload.thread_settings)
          ? payload.thread_settings : {};
        if (settings.model) st.model = String(settings.model);
      } else if (ptype === 'item_completed') {
        const item = laneIsPlainObject(payload.item) ? payload.item : {};
        if (item.type === 'AgentMessage') {
          const parts = [];
          for (const chunk of (item.content || [])) {
            if (laneIsPlainObject(chunk) && chunk.text) parts.push(String(chunk.text));
          }
          events.push({ line: lineNum, type: 'assistant_text', ts: tsIso, detail: parts.join('\n') });
        }
      }
    } else if (rtype === 'response_item') {
      const ptype = payload.type || '';
      if (ptype === 'custom_tool_call' || ptype === 'function_call') {
        let name;
        let toolInput;
        if (ptype === 'custom_tool_call') {
          const program = String(payload.input || '');
          LANE_CODEX_API_RE.lastIndex = 0;
          const apis = [];
          let m;
          while ((m = LANE_CODEX_API_RE.exec(program)) !== null) apis.push(m[1]);
          name = apis.length ? apis[0] : String(payload.name || '');
          toolInput = { _raw: program };
        } else {
          name = String(payload.name || '');
          toolInput = laneIsPlainObject(payload.input) ? payload.input
            : (payload.input == null ? {} : { _raw: payload.input });
        }
        events.push({
          line: lineNum, type: 'tool_call', ts: tsIso,
          tool_name: name,
          tool_input: toolInput,
          tool_use_id: String(payload.call_id || ''),
        });
      } else if (ptype === 'custom_tool_call_output' || ptype === 'function_call_output') {
        const text = laneCodexOutputText(payload);
        const head = text ? text.split('\n', 1)[0] : '';
        events.push({
          line: lineNum, type: 'tool_result', ts: tsIso,
          tool_use_id: String(payload.call_id || ''),
          // No status field: failure is announced in the output's first line.
          is_error: LANE_CODEX_FAILURE_HEADS.some(h => head.startsWith(h)),
          detail: text,
        });
      }
    } else if (rtype === 'turn_context') {
      if (payload.model) st.model = String(payload.model);
    } else if (rtype === 'session_meta') {
      // First one wins: a rollout declares its own thread once, at the head.
      if (st.sessionId === null && payload.session_id) st.sessionId = String(payload.session_id);
    }
    // world_state / compacted / inter_agent_communication_metadata: no
    // billing or tool consequence.
  }

  lanePairToolEvents(events);
  return { events, meta };
}

// Book ONE billing record per real request (backend _codex_token_count,
// traps 1-4).
function laneCodexTokenCount(st, meta, fileKey, lineNum, tsIso, payload) {
  const info = laneIsPlainObject(payload.info) ? payload.info : {};
  const total = laneIsPlainObject(info.total_token_usage) ? info.total_token_usage : {};
  if (Object.keys(total).length === 0) return;
  const cumulative = {};
  for (const k of LANE_CODEX_USAGE_KEYS) cumulative[k] = laneInt(total[k]);
  if (st.prevUsage === null) {
    // Trap 1: the inherited baseline is the first snapshot minus the
    // request that produced it, so the parent's millions never enter.
    const last = laneIsPlainObject(info.last_token_usage) ? info.last_token_usage : {};
    st.prevUsage = {};
    for (const k of LANE_CODEX_USAGE_KEYS) {
      st.prevUsage[k] = cumulative[k] - laneInt(last[k]);
    }
  }
  const delta = {};
  for (const k of LANE_CODEX_USAGE_KEYS) delta[k] = cumulative[k] - st.prevUsage[k];
  st.prevUsage = cumulative;
  if (LANE_CODEX_USAGE_KEYS.every(k => delta[k] <= 0)) return; // Trap 2

  // Trap 3: cached and cache-write inputs are SUBSETS of input_tokens.
  const totalIn = Math.max(0, delta.input_tokens);
  const read = Math.max(0, delta.cached_input_tokens);
  const create = Math.max(0, delta.cache_write_input_tokens);
  const fresh = Math.max(0, totalIn - read - create);
  const output = Math.max(0, delta.output_tokens);
  const reasoning = Math.max(0, delta.reasoning_output_tokens);

  // Trap 5: the long-context meter is an API-billing tier, not a property
  // of the request. A ChatGPT-plan rollout bills flat whatever the prompt.
  const rl = laneIsPlainObject(payload.rate_limits) ? payload.rate_limits : {};
  if (rl.plan_type) st.subscription = true;

  const record = laneUsageMeta(
    lineNum, tsIso,
    st.sessionId ? `${st.sessionId}:${cumulative.total_tokens}` : `${fileKey}:${lineNum}`,
    laneCodexModel(st.model || st.soleModel),
    fresh, create, read, output,
    !st.subscription && totalIn > window.LONG_CONTEXT_THRESHOLD,
  );
  record.thinking_tokens = reasoning;
  meta.push(record);
}

// Book a rate-limit hit, if this token_count reports one (backend
// _codex_rate_limit). A condition spans many token_count events; only the
// first of a run books one.
function laneCodexRateLimit(st, meta, lineNum, tsIso, payload) {
  const rl = laneIsPlainObject(payload.rate_limits) ? payload.rate_limits : {};
  const kind = rl.rate_limit_reached_type;
  if (!kind && !rl.spend_control_reached) {
    st.lastRlKind = null;
    return;
  }
  const key = kind ? String(kind) : 'spend_control_reached';
  if (key === st.lastRlKind) return;
  st.lastRlKind = key;
  const primary = laneIsPlainObject(rl.primary) ? rl.primary : {};
  meta.push({
    line: lineNum, type: 'rate_limit', ts: tsIso,
    content: (
      `${key} (plan=${rl.plan_type}, `
      + `primary ${primary.used_percent}% of ${primary.window_minutes}m window)`
    ).slice(0, 500),
  });
}

// --------------------------------------------------------------------------
// Entry points wired into parseTranscript
// --------------------------------------------------------------------------

window.parseTranscriptLanes = function parseTranscriptLanes(blob, opts) {
  const fmt = window.sniffTranscriptFormat(blob);
  if (fmt === 'codex') return parseLaneCodex(blob, opts);
  if (fmt === 'kimi-code') return parseLaneKimiCode(blob, opts);
  if (fmt === 'legacy') return parseLaneLegacy(blob);
  return { events: [], meta: [] }; // claude — caller handles that itself
};
