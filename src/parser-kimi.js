// Browser parser for both Kimi wire formats (backend/parse_kimi.py):
// the legacy kimi-cli wire and kimi-code. Split out of
// src/parser-lanes.js, which keeps sniffing, the shared helpers and
// the lane entry that dispatches each blob here by sniffed format.
// The same lockstep SV-PARSER-SPEC binds this file as it binds
// parser-lanes.js: the emitted shapes are the lane shapes —
//   events: user_message / assistant_text / thinking / tool_call / tool_result
//   meta:   assistant_usage plus rate_limit entries

// The shared lane helpers, bound off window (parser-lanes.js loads first
// and exposes them). Top-level var, NOT const: this is a classic script
// sharing the page's global scope with parser-lanes.js, whose same-named
// top-level function declarations are non-configurable global properties.
// A global lexical declaration (const/let) of such a name is a SyntaxError
// that kills this file before its first statement runs - a
// top-level var lands on that same global property instead and cannot
// collide. Guarded by tests/test_classic_script_scope.py, which
// instantiates every classic script index.html loads in one realm.
var laneIsPlainObject = window.laneIsPlainObject;
var laneInt = window.laneInt;
var laneToIso = window.laneToIso;
var laneUsageMeta = window.laneUsageMeta;
var lanePairToolEvents = window.lanePairToolEvents;
// The Kimi model ladder — backend/parse_kimi.py, verbatim

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
  if (window.resolveCacheWrites) window.resolveCacheWrites(meta);
  return { events, meta };
}

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
  let firstEventTs = null;

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
  if (window.resolveCacheWrites) window.resolveCacheWrites(meta);
  return { events, meta };
}

window.parseLaneLegacy = parseLaneLegacy;
window.parseLaneKimiCode = parseLaneKimiCode;
