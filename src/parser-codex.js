// Browser parser for the Codex rollout lane (backend/parse_codex.py).
// Split out of src/parser-lanes.js: the codex section is the size
// ratchet's largest JS member and the fork/replay head (issues #653,
// #687) is codex-only. Same lockstep SV-PARSER-SPEC keeps parser-lanes
// and parser.js in with the backend parsers: the emitted shapes are the
// lane shapes parser-lanes.js documents —
//   events: user_message / assistant_text / thinking / tool_call / tool_result
//   meta:   assistant_usage plus rate_limit entries
// with one added, sparse field: a fork's replayed prefix entries carry
// isReplay: true, the copy the canonical rank (src/record-dedup.js)
// demotes behind the parent rollout's originals.

// The shared lane helpers, bound off window (parser-lanes.js loads first
// and exposes them). Top-level var, NOT const: this is a classic script
// sharing the page's global scope with parser-lanes.js, whose same-named
// top-level function declarations are non-configurable global properties.
// A global lexical declaration (const/let) of such a name is a SyntaxError
// that kills this file before its first statement runs (issue #780) - a
// top-level var lands on that same global property instead and cannot
// collide. Guarded by tests/test_classic_script_scope.py, which
// instantiates every classic script index.html loads in one realm.
var laneIsPlainObject = window.laneIsPlainObject;
var laneInt = window.laneInt;
var laneToIso = window.laneToIso;
var laneUsageMeta = window.laneUsageMeta;
var lanePairToolEvents = window.lanePairToolEvents;
// --------------------------------------------------------------------------
// Codex rollout format — backend/parse_codex.py
// --------------------------------------------------------------------------

// `exec` is the only custom tool; the api it calls is the useful name.
const LANE_CODEX_API_RE = /tools\.([A-Za-z_][A-Za-z_0-9]*)\s*\(/g;
// The cumulative counter's fields; all six are differenced so a duplicate
// snapshot is recognised by ALL of them failing to advance.
const LANE_CODEX_USAGE_KEYS = [
  'input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
  'output_tokens', 'reasoning_output_tokens', 'total_tokens',
];
// A tool result whose first line starts with one of these is a failure.
const LANE_CODEX_FAILURE_HEADS = ['Script failed', 'collab spawn failed'];

// A record keeps the model id the transcript names (issue #471) — the
// relabelling map and flagship fallback are gone (backend _codex_model):
// the only rewrites are spelling: lower case and the missing separator
// (gpt5.6-sol -> gpt-5.6-sol); an unpriced id surfaces as estimated_rate.
// A transcript naming no model keeps null — the backend refuses such a
// file outright (issue #653); the browser has no ingest to fail loudly.
function laneCodexModel(raw) {
  const lowered = String(raw || '').trim().toLowerCase();
  if (!lowered) return null;
  return lowered.replace(/^gpt(?=[0-9.])/, 'gpt-');
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
  const text = String(blob);
  const lines = text.split(/\r?\n/);
  const events = [];
  const meta = [];
  const fileKey = (opts && opts.fileKey) || '';

  // Cheap pre-scan (backend codex_fork.head_scan): the FIRST model this
  // file declares, in line order — the model in force where a fork cut,
  // the inherited settings, attributing the replayed prefix (issue #653);
  // a file declaring none keeps null and the backend refuses it. Beside
  // it: whether the first session_meta names a parent thread
  // (forked_from_id — a forked rollout), and that first declaration's
  // line, the replay boundary — a fork's leading lines are its parent's
  // history journalled here, marked isReplay below (issue #687).
  let firstDeclaredModel = null;
  let firstDeclaredLine = null;
  let isFork = false;
  // One needle search keeps the per-line session_meta check off the
  // common non-fork path (backend codex_fork.head_scan).
  let forkSettled = !text.includes('"forked_from_id"');
  for (let ln = 0; ln < lines.length; ln++) {
    const line = lines[ln];
    if (!forkSettled && line.includes('"session_meta"')) {
      let sm = null;
      try { sm = JSON.parse(line); } catch { sm = null; }
      if (laneIsPlainObject(sm) && sm.type === 'session_meta') {
        // The FIRST session_meta decides (the parse loop's own head
        // rule); a non-meta line mentioning the needle just advances.
        const ffid = laneIsPlainObject(sm.payload) ? sm.payload.forked_from_id : null;
        // Mirrors backend _nonempty_str: any non-empty string, no trim.
        isFork = typeof ffid === 'string' && ffid !== '';
        forkSettled = true;
      }
    }
    if (firstDeclaredLine !== null) continue;
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
    if (name && firstDeclaredModel === null) {
      firstDeclaredModel = String(name);
      firstDeclaredLine = ln + 1;
    }
  }
  const st = {
    prevUsage: null,      // previous cumulative snapshot, for differencing
    // Model in force: opened as the file's first declared model (the
    // replayed fork prefix's attribution, issue #653), then the latest
    // declaration wins.
    model: firstDeclaredModel,
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
          const content = Array.isArray(item.content) ? item.content : [];
          for (const chunk of content) {
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

  // The replayed prefix (issue #687): the fork's leading lines, before
  // its own first declaration, are the parent's history — isReplay marks
  // them, mirroring backend codex_fork.mark_replay, for the canonical
  // rank record-dedup.js applies in the multi-load mode.
  if (isFork && firstDeclaredLine !== null) {
    for (const e of meta) e.isReplay = e.line < firstDeclaredLine;
    for (const e of events) e.isReplay = e.line < firstDeclaredLine;
  }

  // Cross-file record dedup (issue #713): the lane's derived record
  // identities join the shared seenUuids map the way parser.js feeds the
  // Claude path's line uuids — decide per assistant_usage entry, then
  // this call's retraction drops the copies a standing winner outranks.
  // Without it a parent and its fork loaded together keep BOTH copies of
  // every replayed record and the Inspector double-counts replayed
  // history. The dedup is record-level: a replayed tool_call's identity
  // (call_id) is the tool_use_id keyspace, which no browser half mirrors
  // yet. The winner's model adoption itself stays DB-side: a lone fork
  // file has no parent to read, so its parse keeps the #653 fallback.
  const seenUuids = (opts && opts.seenUuids) || null;
  if (seenUuids && window.recordDedup) {
    const stamps = new Map();
    for (const m of meta) {
      if (m.type !== 'assistant_usage' || !m.uuid) continue;
      window.recordDedup.decide(seenUuids,
        { uuid: m.uuid, model: m.model, isReplay: m.isReplay === true },
        m.line, stamps);
    }
    window.recordDedup.retract(events, meta, stamps, seenUuids);
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

  // The long-context meter is a property of the model's rate card, not of
  // the plan that served the request — every record is billed as if it
  // were an API call (issue #194), so a subscription rollout above the
  // threshold bills the meter exactly as an API-key one does.
  const record = laneUsageMeta(
    lineNum, tsIso,
    st.sessionId ? `${st.sessionId}:${cumulative.total_tokens}` : `${fileKey}:${lineNum}`,
    laneCodexModel(st.model),
    fresh, create, read, output,
    totalIn > window.LONG_CONTEXT_THRESHOLD,
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


window.parseLaneCodex = parseLaneCodex;
