"""The is_reread resolution (SV-CONTEXT-INTAKE), moved out of parse.py —
which sat exactly at its module-size entry — into its own module. Walked
in line order over ONE jsonl: a file boundary is where the context this
measures restarts."""
from __future__ import annotations

from backend.target_paths import target_key


def resolve_rereads(tool_uses: list) -> None:
    """Flag each whole-file read that added nothing new to the context.

    Walked in line order over ONE jsonl, which is the right scope: the
    context this measures is a session's, and a file boundary is where
    that context restarts.

    A read is a re-read when EVERY file it names was already read whole
    in this file and none of them has been written since. Three things
    that look like waste and are not, all excluded here:

    - a SLICE. `sed -n '1,200p' f` then `sed -n '200,400p' f` read
      different halves; only a whole read can be wholly redundant.
    - a read after a WRITE. The bytes changed, so re-reading them is
      the only way to see the new ones.
    - an ERRORED read. It returned a failure, not the file, so it
      neither wasted context nor counts as having seen the file —
      which is why it does not mark its targets either.

    Partial overlap (`cat a b` after only `a`) is NOT flagged: something
    new arrived, so the call was not wasted. The flag stays deliberately
    conservative — it is easier to argue up from a floor than to defend a
    number that counted useful reads.
    """
    seen_whole: set[str] = set()
    for tu in tool_uses:
        targets = [target_key(path) for path in tu.get("read_targets") or []]
        if tu.get("read_kind") == "whole" and targets and not tu["is_error"]:
            tu["is_reread"] = all(path in seen_whole for path in targets)
            seen_whole.update(targets)
        for path in tu.get("write_targets") or []:
            seen_whole.discard(target_key(path))
