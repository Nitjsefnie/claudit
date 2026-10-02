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
//                                     unattributed one, whose already-emitted
//                                     events are spliced out of THIS call's
//                                     stream
//   no prev                        -> keep, record the verdict
// A parse call can only retract its OWN events — an earlier call's
// already-returned meta is the caller's — so across calls the map's final
// verdict is the last word: a caller concatenating several files' outputs
// drops each unattributed event whose uuid's verdict ends on true.
(function () {
  'use strict';
  function isAttributed(model) {
    return typeof model === 'string' && model
      && model !== 'unknown' && model !== '(unknown)'
      && model !== '<synthetic>';
  }

  // Decide one uuid-carrying line: 'skip' or 'keep'. `meta` is this call's
  // event stream (spliced on replace); `seen` is the shared Map
  // uuid -> attributed. Mirrors _UNATTRIBUTED in ingest_rollup_state.py.
  window.recordDedup = {
    decide: function (seen, obj, meta) {
      if (!seen || typeof obj.uuid !== 'string' || !obj.uuid) return 'keep';
      const prev = seen.get(obj.uuid);
      const msg = obj.message;
      const model = (msg && typeof msg === 'object') ? msg.model : null;
      const attributed = isAttributed(model);
      if (strictlyLoses(prev, attributed)) return 'skip';
      if (prev === false) {
        // The unattributed copy's events are already in this stream.
        for (let k = meta.length - 1; k >= 0; k--) {
          if (meta[k] && meta[k].uuid === obj.uuid) meta.splice(k, 1);
        }
        seen.set(obj.uuid, true);
      } else if (prev === undefined) {
        seen.set(obj.uuid, attributed === true);
        // prev === true is handled by strictlyLoses above.
      }
      return 'keep';
    },
  };

  // prev === true always loses; prev === false loses only to attributed.
  function strictlyLoses(prev, attributed) {
    return prev === true || (prev === false && attributed !== true);
  }
})();