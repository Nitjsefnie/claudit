// Cross-file record dedup — the browser half of the canonical winner rule
// (SV-CANONICAL-FLAG, issues #529 and #687). A forked Codex rollout replays
// its parent's history in its leading lines: under #653 those copies are
// attributed (the fork's first declared model), and under #687 a replayed
// copy is marked isReplay and loses to an original of the same uuid,
// whatever the model and the key order. This module mirrors that rule for
// the browser's directory / multi-load mode, where several files are parsed
// through one shared `seenUuids` map.
//
// Contract, in parse order per line with a uuid — the seen value is the
// standing winner's RANK, LOWER WINS (mirrors recompute_canonical's
// ORDER BY <replay-last>, <unattributed-last>, file_key, line_num, with
// arrival order standing in for file_key):
//   prev absent                    -> keep, record the verdict
//   rank > prev, or rank == prev   -> skip (standing copy outranks or ties)
//   rank < prev                    -> keep: this copy REPLACES the standing one
// parseTranscript stamps every entry it emits for a uuid-bearing line with
// that line's uuid + model + isReplay and retracts the masked copies itself
// at end of parse (#562): a stamp is MASKED when its own rank loses to the
// uuid's final verdict — the loser of the canonical vote, whose entries
// must leave the output. A requestId merge that folded a surviving
// fragment's usage into a superseded line's entry re-points that entry to
// the surviving line at retraction (#568), so the uuid's one usage entry
// survives, attributed to the winner. After all loads a caller
// concatenating several files' outputs finishes the job with dropMasked
// (#563) — the cross-call drop is module code, not a contract the caller
// has to reimplement; the same rank test, judged on the stamps retract
// left on the entries.
(function () {
  'use strict';
  function isAttributed(model) {
    return typeof model === 'string' && model
      && model !== 'unknown' && model !== '(unknown)'
      && model !== '<synthetic>';
  }
  function modelOf(obj) {
    // A raw transcript line names the model inside message; a parsed
    // entry and a stamp carry it at top level.
    const msg = obj.message;
    if (msg && typeof msg === 'object' && typeof msg.model === 'string') {
      return msg.model;
    }
    return typeof obj.model === 'string' ? obj.model : null;
  }

  // The winner rank of one copy, LOWER WINS: an original (isReplay not
  // true) beats a replay, an attributed copy beats an unattributed one.
  // Mirrors _REPLAY_LAST then _UNATTRIBUTED in ingest_rollup_state.py.
  function rankOf(obj) {
    return (obj.isReplay === true ? 2 : 0) + (isAttributed(modelOf(obj)) ? 0 : 1);
  }

  // Decide one uuid-carrying line: 'skip' or 'keep'. `seen` is the shared
  // Map uuid -> standing winner's rank.
  window.recordDedup = {
    decide: function (seen, obj, line, stamps) {
      if (!seen || typeof obj.uuid !== 'string' || !obj.uuid) return 'keep';
      const prev = seen.get(obj.uuid);
      const rank = rankOf(obj);
      if (prev !== undefined && rank >= prev) return 'skip';
      seen.set(obj.uuid, rank);
      if (stamps) {
        const msg = obj.message;
        const mid = (msg && typeof msg === 'object'
                     && typeof msg.id === 'string' && msg.id) ? msg.id : '';
        stamps.set(line, { uuid: obj.uuid, model: modelOf(obj),
          isReplay: obj.isReplay === true,
          // The parser's requestId merge key (parser.js's merge-key shape):
          // retract re-points a merged entry to a surviving line of the
          // SAME key only (issue #568), never across API calls.
          req: obj.requestId || (mid ? 'msg:' + mid : '') });
      }
      return 'keep';
    },

    // End-of-parse retraction (issue #562): drop every entry whose line's
    // stamp is masked — the superseded copy's events leave together with
    // its assistant_usage meta — and stamp every surviving entry with its
    // line's uuid + model, so the caller's cross-call dropMasked can judge
    // entries after concatenation (lines are per-call, entries are not).
    retract: function (events, meta, stamps, seen) {
      for (const list of [events, meta]) {
        for (let k = list.length - 1; k >= 0; k--) {
          const e = list[k];
          if (!e) continue;
          let s = stamps.get(e.line);
          if (e.type === 'assistant_usage' && maskedBy(seen, s) && s.req) {
            // issue #568: a requestId merge folded a surviving fragment's
            // usage into this superseded line's entry. The entry belongs
            // to a surviving line of the SAME merge key -- prefer the
            // entry's own uuid's winner; re-point, never drop. (An empty
            // key never merged, so it never re-points.)
            let same = null, any = null;
            for (const [L, t] of stamps) {
              if (L <= e.line || t.req !== s.req || maskedBy(seen, t)) {
                continue;
              }
              if (t.uuid === s.uuid) same = L;
              any = L;
            }
            const to = same !== null ? same : any;
            if (to !== null) {
              const t = stamps.get(to);
              e.line = to; e.uuid = t.uuid; e.model = t.model; s = t;
            }
          }
          if (maskedBy(seen, s)) { list.splice(k, 1); continue; }
          // Stamp only what the entry lacks: meta already carries its own
          // (fallback-applied) model, and it must not lose it.
          if (s) { if (e.uuid === undefined) e.uuid = s.uuid;
                   if (e.model === undefined) e.model = s.model; }
        }
      }
    },

    // Cross-call drop (issue #563): drop entries a later parse call
    // replaced. An entry with no uuid (never deduped — the NULL-uuid rows
    // are always canonical) and an entry whose uuid is absent from `seen`
    // (a single-load parse's output, where no dedup ran) are kept.
    dropMasked: function (list, seen) {
      for (let k = list.length - 1; k >= 0; k--) {
        const e = list[k];
        if (e && maskedBy(seen, e)) list.splice(k, 1);
      }
      return list;
    },

    // Tool-call identity (issue #766): the same winner rule keyed on
    // tool_use_id — the canonical pass's tool_uses partition (mirrors
    // ingest_rollup_state.py's PARTITION BY tool_use_id with the same
    // replay-last, unattributed-last order). The CALL owns the identity
    // (the DB stores one tool_uses row per call; results settle onto it),
    // so decide runs per tool_call only and the paired tool_result is
    // judged by the same id's verdict through dropMaskedTools. An empty
    // tool_use_id (the NULL rows) is never recorded and never deduped;
    // arrival order stands in for file_key, as above.
    decideTool: function (seen, obj) {
      if (!seen) return 'keep';
      const id = obj.tool_use_id;
      if (typeof id !== 'string' || !id) return 'keep';
      const prev = seen.get(id);
      const rank = rankOf(obj);
      if (prev !== undefined && rank >= prev) return 'skip';
      seen.set(id, rank);
      return 'keep';
    },

    // The tool-side drop: splice every tool event whose id's standing
    // winner outranks it (per-file tail and the caller's cross-call drop
    // both run this). Tool events carry no model, so the rank's
    // attribution term is constant here — the replay flag decides, which
    // is the only original-vs-replay shape the keyspace holds.
    dropMaskedTools: function (list, seen) {
      for (let k = list.length - 1; k >= 0; k--) {
        const e = list[k];
        if (e && typeof e.tool_use_id === 'string' && e.tool_use_id
            && seen.get(e.tool_use_id) !== undefined
            && rankOf(e) > seen.get(e.tool_use_id)) list.splice(k, 1);
      }
      return list;
    },
  };

  function maskedBy(seen, e) {
    return !!e && !!e.uuid && seen.get(e.uuid) !== undefined
      && rankOf(e) > seen.get(e.uuid);
  }
})();
