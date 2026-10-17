// Browser lanes: format sniffing, the shared helpers and the entry point.
// Codex logic is in parser-codex.js; Kimi parsing is in parser-kimi.js.
// Keep lane records and pricing aligned with backend parsing (SV-PARSER-SPEC).
// Global Codex meter defaults; per-model entries can override them.
window.LONG_CONTEXT_THRESHOLD = 272000;
window.LONG_CONTEXT_INPUT_MULT = 2.0;
window.LONG_CONTEXT_OUTPUT_MULT = 1.5;
function _longContextMeterFor(model) {
  const norm = String(model || '').trim().toLowerCase();
  const key = (norm.indexOf('claude') > 0
    ? norm.slice(norm.indexOf('claude')) : norm).replace(/\./g, '-');
  return (window.longContextMeters || {})[key];
}

// Per-model thresholds: mirrors pricing.long_context_threshold.
window.longContextThresholdFor = function (model) {
  const meter = _longContextMeterFor(model);
  return (meter && meter.threshold) || window.LONG_CONTEXT_THRESHOLD;
};
// Per-model factors: omitted fields keep the global defaults.
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

// The shared helpers src/parser-codex.js and src/parser-kimi.js (both
// split out of this file) consume; the window exposure is the seam
// between them, and this file must load first.
window.laneIsPlainObject = window.laneIsPlainObject || laneIsPlainObject;
window.laneInt = window.laneInt || laneInt;
window.laneToIso = window.laneToIso || laneToIso;
window.laneUsageMeta = window.laneUsageMeta || laneUsageMeta;
window.lanePairToolEvents = window.lanePairToolEvents || lanePairToolEvents;

function laneIsPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

// Any timestamp the lane formats carry -> an ISO string ('' when absent or
// unparsable). Numbers are epoch SECONDS (legacy Kimi); strings (Codex,
// legacy ISO variants) go through Date.parse — an offset-less ISO string
// stamped UTC first (mirrors parse_common._to_dt: unstamped,
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
function laneUsageMeta(line, tsIso, uuid, model, fresh, create, read, output,
                       longContext, webSearchRequests = null) {
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
    web_search_requests: webSearchRequests,
  };
}

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

window.parseTranscriptLanes = function parseTranscriptLanes(blob, opts) {
  const fmt = window.sniffTranscriptFormat(blob);
  if (fmt === 'codex') return window.parseLaneCodex(blob, opts);
  if (fmt === 'kimi-code') return window.parseLaneKimiCode(blob, opts);
  if (fmt === 'legacy') return window.parseLaneLegacy(blob);
  return { events: [], meta: [] }; // claude — caller handles that itself
};
