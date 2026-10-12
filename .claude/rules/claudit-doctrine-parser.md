---
paths:
  - "**/backend/parse*.py"
  - "**/backend/codex_fork.py"
  - "**/backend/kimi_content.py"
  - "**/backend/bash_*.py"
  - "**/backend/turn_flags.py"
  - "**/backend/prompt_gate.py"
  - "**/backend/agent_sidecar.py"
  - "**/backend/target_paths.py"
  - "**/backend/tool_errors.py"
  - "**/backend/json_shape.py"
  - "**/src/parser*.js"
  - "**/src/record-dedup.js"
  - "**/src/ctx-input.js"
  - "**/fixtures/parser/**"
  - "**/fixtures/codex/**"
---

# claudit Doctrine — parser

## Parser-spec ownership (SV-PARSER-SPEC)

This rule is the parse spec. `backend/parse.py` and the in-browser
`src/parser.js` implement it; `fixtures/parser/` (driven by
`tests/test_parse.py`) pins it. Keep both in lockstep on:

- Per-file requestId `_merge_usage_max` at ingest (Phase 1, persisted
  into `records`).
- Cross-file UUID dedup, resolved at ingest into `records.is_canonical`
  (SV-CANONICAL-FLAG): the winner is what
  `DISTINCT ON (uuid) ORDER BY uuid, <replay-last>, <unattributed-last>,
  file_key` picks. No persisted Phase 2 rollup.
- `<task-notification>` ref detection for sub-agent jsonls.
- Sidecar `data/subagents/agent-*.jsonl` resolution.
- Context turns: backend `_build_ctx_turns` / browser `computeTurnStats`
  use line order, keep pre-prompt turns, take last usage, and drop zero /
  implausible context.
- Web-search requests: Claude reads
  `usage.server_tool_use.web_search_requests` per merged reply; Codex counts
  each `response_item/web_search_call` once and ignores its companion
  `web_search_end`; both Kimi wires store NULL because they carry no count.
- Rate resolution (normalisation, key matching, window selection) over
  `src/pricing.json` (SV-RATE-DATA).

Request timing (`reply_latency_s`, `generation_s`, `output_tokens_per_s`) is backend-only, with no browser consumer, so the browser parser does not mirror it.

**Lane parsers are a second lockstep pair.** `parse_file()` sniffs the
format and dispatches Codex rollouts and both Kimi wire formats to
`backend/parse_codex.py` / `backend/parse_kimi.py` (shared machinery in
`backend/parse_common.py`, adapted to claudit's columns by
`backend/parse_lanes.py`). Their browser mirror is `src/parser-lanes.js`:
a backend change to HOW a lane parses — record identity,
cumulative-token differencing, the long-context decision, model
attribution, tool-result settling — REQUIRES the same change there, or
the Inspector disagrees with the database. `tests/test_parser_js_lanes.py`
fails on drift; the lane browser test fails when
`window.LONG_CONTEXT_THRESHOLD` / `LONG_CONTEXT_INPUT_MULT` /
`LONG_CONTEXT_OUTPUT_MULT` stop matching `pricing.LONG_CONTEXT_*`, or
when the loader's `window.longContextModels` / `window.longContextMeters`
stop matching the grouped `long_context_meters` field after folding
(membership is its key set, issue #883), or
when `window.longContextFactorsFor(model)` stops matching
`pricing.long_context_factors(model)`.

Resolve a discrepancy against this spec and the fixtures: fix the side
that departs. If the spec itself is wrong, change it here, in both
implementations, and add a pinning fixture, all in one commit.

The replayed-model adoption is INGEST-level, not parser semantics, and
is deliberately NOT a parser change on either side: a parse sees one
file, and the parent's model in force lives in a different one. A
browser parse of a lone fork file keeps the #653 fallback (the fork's
first declaration) — that is the record the Inspector shows for a
session whose parent never loads with it. What the browser DOES mirror
is the cross-file record dedup: `parseLaneCodex` feeds the lane's
derived record identities through `recordDedup.decide` + `retract` when
a shared `seenUuids` map is passed (issue #713), so with a parent and
its fork both loaded the parent's originals survive and the fork's
replayed records leave — the same winner the DB's canonical rows hold.
The tool_use_id pass is mirrored the same way (issue #766):
`parseLaneCodex` decides each `tool_call` into a shared `seenToolIds` map
through `recordDedup.decideTool` and drops the copies the standing winner
outranks with `recordDedup.dropMaskedTools`, tool_call and tool_result
events together — the same winner the DB's `tool_uses.is_canonical` rows
hold.

## Bash line churn is an estimate from command text (SV-BASH-CHURN)

Count payload lines for heredocs, patches, Python edits and literal
printf/echo/sed output. Python replacements and Edit calls count ONE
occurrence, diffed: `bash_churn.replace_churn` line-diffs old against
new, so context repeated on both sides is not churn, and a payload
ending mid-line changes that whole line (git's count). No match
multiplication; a successful exit does not prove a line changed. A
recognized Bash file write of unknown addition size counts one added
line per CALL, only if no additions were already counted. Known-empty
operations and whole-line deletions add zero; removing text inside a
line is +1/-1. Never infer old contents for copies/overwrites, invent
paths for unresolved targets, execute recorded commands, or infer writes
from opaque script names or arbitrary object methods. Read-only calls,
null sinks and rejected/unlaunched/failed writes get no fallback churn.
Keep the existing heredoc-write-before-later-error rule.

A heredoc inside `for x in W1 W2 …; do … done` whose words are all
literal has its churn multiplied by the product of its enclosing literal
loops (`bash_loops.heredoc_repeats`). An unknowable count — a runtime
word (`$x`, `$(…)`, an unquoted glob, `"$@"`), a bare `for NAME`,
`while`/`until`/`select`, or a command the tokenizer rejects — counts
ONE. Only heredoc bodies repeat; `echo`/`printf` and `python -c` in a
loop count once. Write targets are unchanged: a path built from the loop
variable is a runtime path and yields nothing.

## Context intake is stored per call (SV-CONTEXT-INTAKE)

Five `tool_uses` columns record what a call put INTO the context window
and whether it was already there:

- `result_chars` — tool_result size, IMAGES INCLUDED (base64 is the
  largest payload a result carries); recorded for errored calls too.
- `read_targets` / `write_targets` — TEXT[], since one call can name
  several files (`cat a b`, `diff a b`).
- `read_kind` — `whole` | `slice`. LOAD-BEARING: `grep -n x big.py` and
  `cat big.py` name one file but only the second loads it; scoring them
  alike ranks grep-then-narrow as MORE wasteful than a blind `cat`.
- `is_reread` — set by `parse._resolve_rereads`; NULL unless a settled
  whole-file read.

Targets come from tool arguments for `Read`/`Edit`/`Write` and from
COMMAND TEXT for `Bash` (`backend/bash_reads.py`). Bash write targets
cover `>`/`>>`/`tee`, `sed -i` (a WRITE, never a slice read; its script
operand is never a file), and paths a `python3` `-`/`-c` body opens for
writing (`bash_churn.python_write_paths`). `$VAR` expands only when the
same command assigns it (`S=/tmp/s && cat > $S/f`); any surviving `$`
means a runtime path, which yields nothing. Heredoc bodies are stripped
before the scan.

`is_reread` is a CONSERVATIVE floor. It excludes slices, reads after a
write to the same path, errored reads, and partial overlap (`cat a b`
after only `a`). Scope is ONE jsonl, where a session's context restarts.

These are psql-only, like `error_text`: no endpoint, panel or rollup
(`read_targets` is unbounded). **Do not add a panel** — the raw total
sits in a few image-heavy sessions, so a chart would show an outlier as
a trend. Query it:

    SELECT sum(result_chars) FROM tool_uses WHERE is_reread;
