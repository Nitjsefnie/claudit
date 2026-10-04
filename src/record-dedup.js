// Cross-file record dedup — the browser half of the canonical winner rule
// (SV-CANONICAL-FLAG, issue #529). A forked Codex rollout replays its
// parent's history with no model declaration in front of it, so its copies
// store model `unknown`, and under a bare file_key ordering they won the
// ingest's dedup; the winner now prefers an attributed copy. This module
// mirrors that rule for the browser's directory / multi-load mode, where
// several files are parsed through one shared `seenUuids` map.
//
// Contract, in parse order per line with a uuid:
//   prev === true                 -> skip (the seen copy is attributed)
//   prev === false, not attributed -> skip (duplicate unattributed copy)
//   prev === false, attributed     -> keep: this copy REPLACES the seen
//                                     unattributed one
//   no prev                        -> keep, record the verdict
// parseTranscript stamps every entry it emits for a uuid-bearing line with
// that line's uuid + model and retracts the masked copies itself at end of
// parse (#562): a stamp is MASKED when its uuid's final verdict is
// attributed and its own model is unattributed — the loser of the
// canonical vote, whose entries must leave the output. A requestId merge
// that folded a surviving fragment's usage into a superseded line's entry
// re-points that entry to the surviving line at retraction (#568), so the
// uuid's one usage entry survives, attributed to the winner. After all
// loads a caller concatenating several files' outputs finishes the job with
// dropMasked (#563) — the cross-call drop is module code, not a contract
// the caller has to reimplement; the same masked test, judged on the
// stamps retract left on the entries.
(function () {
  'use strict';
  function isAttributed(model) {
    return typeof model === 'string' && model
      && model !== 'unknown' && model !== '(unknown)'
      && model !== '<synthetic>';
  }

  // Decide one uuid-carrying line: 'skip' or 'keep'. `seen` is the shared
  // Map uuid -> attributed. Mirrors _UNATTRIBUTED in ingest_rollup_state.py.
  window.recordDedup = {
    decide: function (seen, obj, line, stamps) {
      if (!seen || typeof obj.uuid !== 'string' || !obj.uuid) return 'keep';
      const prev = seen.get(obj.uuid);
      const msg = obj.message;
      const model = (msg && typeof msg === 'object') ? msg.model : null;
      const attributed = isAttributed(model);
      if (strictlyLoses(prev, attributed)) return 'skip';
      if (prev === false) seen.set(obj.uuid, true);
      else if (prev === undefined) seen.set(obj.uuid, attributed === true);
      // prev === true keeps the standing verdict; the winner is unchanged.
      if (stamps) stamps.set(line, { uuid: obj.uuid, model: model,
        // The parser's requestId merge key (parser.js's merge-key shape):
        // retract re-points a merged entry to a surviving line of the
        // SAME key only (issue #568), never across API calls.
        req: obj.requestId || (msg && typeof msg.id === 'string' && msg.id
          ? 'msg:' + msg.id : '') });
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
  };

  function maskedBy(seen, e) {
    return !!e && !!e.uuid && seen.get(e.uuid) === true
      && !isAttributed(e.model);
  }

  // prev === true always loses; prev === false loses only to attributed.
  function strictlyLoses(prev, attributed) {
    return prev === true || (prev === false && attributed !== true);
  }
})();
