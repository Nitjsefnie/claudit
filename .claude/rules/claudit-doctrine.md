# claudit Doctrine

Local rules for the claudit repo.

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
- Rate resolution (normalisation, key matching, window selection) over
  `src/pricing.json` (SV-RATE-DATA).

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
stop matching the file's `long_context_models` / `long_context_meters`
(the per-model thresholds, issue #765).

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

## Cost accounting is split TTL, always (SV-COST-SPLIT)

Every cost computed from `usage` MUST split `cache_creation` into
`ephemeral_5m` (1.25× base input) and `ephemeral_1h` (2× base input).
Tokens with no `ephemeral_*` split are charged at the **1h rate**.
Every site that prices or decomposes cost follows this —
`pricing.compute_cost`, the `/api/cache` fold
(`api_common._accumulate_buckets`), both browser sites in `src/app.jsx`
— or a breakdown stops summing to its stored total.

Single-rate `cache_create` cost is BANNED. A rate change that reprices
stored records bumps `PRICING_VERSION` in `backend/constants.py`; the
next ingest reprices in place — no reparse, no R2 fetch (SV-REPRICE).

## Backend is the ONLY load path (SV-NO-LOCAL-UPLOAD)

No browser ingress for transcripts — no drag-drop, `FileReader`, zip
expansion or JSZip — no upload endpoint, and no server-side parsing of
operator-supplied jsonls. The only jsonls read come from R2, owned by
the same operator. Reintroduce none of them, file picker included.

`src/parser.js` is NOT dead code: `loadFromBackend()` parses
`/api/sessions/{id}/transcript` client-side via `parseTranscript` +
`computeSessionStats`, and the Token Breakdown panel prices rows via
`window.rateForModel`. It reads rates from `src/pricing.json`
(SV-RATE-DATA) and resolves them per SV-PARSER-SPEC.

## Bundle distribution NOT applicable (SV-NO-BUNDLE)

claudit does not ship via `claude-setup.zip`; it ships via git +
`pip install -r backend/requirements.txt`.

## Test fixtures stay small (SV-FIXTURE-SIZE)

- `fixtures/parser/*.jsonl`: hand-crafted single-record samples, each
  under 1 KB.
- `fixtures/codex/`: EXEMPT from the cap, but grep it clean of real
  paths, ids and secrets before committing.
- `fixtures/r2_mini/`: the end-to-end mini mirror (2 projects, 4
  sessions, 1 sidecar, 1 cross-session shared uuid).

Don't grow them. Larger samples stay in a local mirror outside the
working tree, reached via `R2_ENDPOINT`.

## Develop against the full corpus, never the local tree (SV-FULL-CORPUS)

Measurements, probes and panel prototypes read the R2 corpus (or a full
mirror), NEVER Claude Code's live session tree. That tree is pruned, so
a rate derived from it is silently biased. Use `backend/r2.py`
(`list_keys` / `get_stream`) so a probe honours `R2_ENDPOINT`. Stratify
and state the sample size; never present a corpus-wide claim from an
unstated subset.

## No external parser, vendored or invoked (SV-READ-ONLY-CANONICAL)

Parsing is self-contained: the backend never invokes, imports or shells
out to a parser or script outside this repository, and no copy, symlink
or hardlink of one is committed. Drift between the in-repo parsers is
fixed here (SV-PARSER-SPEC).

## Schema is applied at startup, not by a human (SV-SCHEMA-AUTOAPPLY)

`db.apply_schema()` runs the idempotent `backend/schema.sql` when this
build's copy differs from what the database last applied —
content-stamped in `schema_stamp` (the sha256 of the file's exact
bytes, written by the same transaction as the DDL) — before
`schema_check()`, under a Postgres advisory lock. A current schema takes no DDL and
therefore no ACCESS EXCLUSIVE lock anywhere; when the DDL IS due, its
lock waits are bounded by a transaction-local `lock_timeout`, and an
expired wait aborts the boot (atomic — the DDL and the stamp share one
transaction); systemd retries. A manual `psql -f backend/schema.sql` is not load-bearing: a
deploy that adds a column changes the stamp and converges the database
instead of failing every ingest with `UndefinedColumn`. An
out-of-band schema mutation (a hand-dropped column) needs
`DELETE FROM schema_stamp` to force a re-apply — the convention of
`DELETE FROM ingest_derived_state` after out-of-band record mutations.

`ingest_derived_state` is likewise `CREATE TABLE IF NOT EXISTS`. Ingest
commits `complete = FALSE` before mutating `files`, `records` or
`tool_uses`, and marks the fingerprint complete only after every
derived phase, `_close_run`, notification and `warm_common` succeed.
The full-rebuild time (`last_full_at`) advances only if the scope was
full when the first hour-keyed rollup began. A later promotion, or an
aborted, failed or interrupted run, leaves the marker incomplete,
forcing a full derived rebuild next run. Older binaries may ignore the
marker.

ROLLBACK IS ONE-DIRECTIONAL — an older binary runs against a newer
schema. That is safe because every migration is additive and nullable,
with guarded, idempotent exceptions:

- The DO block swapping `usage_rollup`'s primary key to widen the grain
  (`long_context`, then `provider`): the table is derived,
  DELETE+INSERT-rebuilt state, the swap only widens, and an older
  binary's named-column INSERT still satisfies it.
- The int→bigint widening of `user_session.user_id`: not derived state,
  but inert for older binaries — they only wrote INTEGER-sized values
  and read ids into Python ints.
- The int→bigint widening of `web_metrics.user_id`: same shape and same
  reason — the session layer reports the AUTH database's user id, which
  is a BIGINT there, so an older binary reading them is indifferent to
  this column's width.

Older binaries' READS ignore unknown columns, and their INGEST never
reparses a file whose stored `parser_version` is newer than their own,
so newer column values survive a rollback (erasure already done by even
older binaries is not repaired). Any other migration that DROPS or
retypes a column needs a different mechanism, not a quiet exception.

## Schema fail-fast (SV-SCHEMA-FAIL-FAST)

`backend/db.schema_check()` runs at every startup and aborts with a
clear error unless (a) `claudit.files` exists and (b) the auth DB's
`users` table carries every column the app reads — `user_id` (an
integer: the login lookup key) and `config` (JSONB) — each diagnosed
three ways when not visible (wrong database / missing SELECT grant /
genuinely absent). The map of checked columns is `db.AUTH_COLUMNS`;
everything else the login path needs is a JSON key inside config, never
a column, and the map does not grow with one.

## Per-file files+records contract (SV-FILES-RECORDS)

Stored file identity is BUCKET-QUALIFIED: `files.file_key` is
`<bucket>/<object-key>`, the bucket one of `R2_BUCKET`'s names (several
joined by `+`). Transcript and sidecar serving derive the bucket from
the stored `file_key`, never the request; `r2._configured` refuses an
unconfigured bucket. The bucket segment never leaves the server: every
`file_key` in an API response goes through `r2.public_key`, which strips
it (unchanged when the first segment is not a configured bucket). psql
`file_key LIKE` filters over `records`/`tool_uses` start with the bucket.

The same object key in two buckets is two files but one project and
session: rows dedup by uuid where the format has them (Claude, Codex),
and a read picking one main file per session id picks arbitrarily. The
planned `codex+kimi` pairing needs no handling; the same session id in
two buckets with a uuid-less format (kimi-code, legacy Kimi)
double-counts at session level.

One lane session shape is not data (issue #650): a lane session named
exactly `test` — `sessions/<project>/test/…`, subagents included — is
the kimi-code test suite's scratch archive (its runs write under a
session named `test` inside a hash-named project), carries no usage,
and classifies as nothing: `key_layout.classify()` and `sidecar_stem()`
answer None for the subtree, the walk never lists it, and the orphan
sweep deletes rows already stored there.

Project identity follows the directory, not the lane: a lane project
whose project.json marker names a directory is keyed by that directory's
Claude slug (`key_layout.project_slug`), so one directory is ONE project
across buckets; without a marker it keeps its hash. Both routes meet in
`key_layout.canonical_project_id`: a Windows slug (drive letter then
`--`) is lowercased, since Windows paths are case-insensitive; a POSIX
slug (starts with `-`) is never folded.

The schema is per-file, not per-session (see `backend/schema.sql`):

- `files(file_key PK, project_id, session_id, is_main, r2_etag,
  r2_size_bytes, r2_last_modified, parsed_at, parser_version,
  ctx_turns JSONB, turn_count)` — one row per ingested JSONL, with the
  context-growth trace inlined as `ctx_turns`.
- `records(file_key, line_num, uuid, request_id, ts, model,
  fresh_tokens, cache_creation_tokens, cache_read_tokens,
  output_tokens, eph5_tokens, eph1h_tokens, cost_usd, request_fee_usd,
  text_chars, reply_latency_s, stop_reason, effort, thinking_tokens,
  cli_version, turn_flags, turn_tool_results, long_context, provider,
  pricing_version, rate_fingerprint)`
  PK `(file_key, line_num)` — one row per usage-bearing line after
  per-file Phase 1 max-merge on `request_id`.

Cross-file dedup lives in `records.is_canonical` (SV-CANONICAL-FLAG).
There is no `record_uuids`, `session_requests`, per-session rollup,
materialized hourly view or `sessions` table. Reintroducing a persisted
rollup or any cross-session table requires a new migration, not a quiet
code change.

## Dedup is a flag, not a read-time sort (SV-CANONICAL-FLAG)

`records.is_canonical` marks the row
`DISTINCT ON (r.uuid) ORDER BY r.uuid, <replay-last>, <unattributed-last>,
r.file_key` would select. A REPLAYED copy loses to an original of the same
uuid whatever the key order: a forked Codex rollout re-journals its
parent's requests in its replayed prefix, its `subagents/…` key sorts
before the parent's `wire.jsonl`, and once #653 attributed the prefix
(the fork's first declared model) a bare attribution-then-key rank let
the fork's copy win and counted the parent's history as the fork's —
`records.is_replay`/`tool_uses.is_replay`, set by
`codex_fork.mark_replay` on the rows before the fork's own first model
declaration, rank those copies last (issue #687). Then the copy whose
model is attributed beats an unattributed one — the lane fallback
`unknown`, the Claude fallback `(unknown)`, the empty string and NULL
all count unattributed, as does `<synthetic>` (Claude's
harness-fabricated stub model: it names no model at all, issue #563)
(issue #529: the original attribution rank; the #529 outcome is
restored under #653's attribution) — then `r.file_key`; `line_num`
breaks ties within a `file_key`. NULL (every non-Codex row and row
parsed before #687) ranks as an original. NULL-`uuid`
rows (legacy) are always canonical.

Reads MUST filter `WHERE is_canonical` and MUST NOT reintroduce
`DISTINCT ON (uuid)`: that re-sorts the whole table per read.

`ingest.recompute_canonical()` runs after EVERY successful ingest. A
full rebuild ranks the full tables; an incremental run ranks only UUIDs
and tool-use ids contributed by dirty files before or after mutation, so
deleting a former winner still promotes the next row. Dirty files'
NULL-identity rows are set canonical. The UPDATE touches only rows whose
flag flips, and each flip adds its project-hour to the rollup scope via
the UTC-instant normalizer, so both folds of a repeated local hour stay
distinct. The column defaults to TRUE, so a migrated-but-unrecomputed DB
over-counts rather than drops rows.

Changing the winner rule changes `recompute_canonical()` and
`src/record-dedup.js` (the browser dedup parser.js calls) in lockstep
(SV-PARSER-SPEC).

`tool_uses.is_canonical` is the same rule keyed on `tool_use_id`, set in
the same pass — a compaction sidecar (`agent-acompact-*`) replays the
main file's tool_use blocks. NULL-`tool_use_id` rows are canonical.
Every rollup and live read over `tool_uses` MUST filter
`tu.is_canonical`.

A replayed copy that loses also ADOPTS the winner's model when the
winner is an original — `adopt_original_models()`, the canonical pass's
tail (issue #713). The fork's first declaration is the
model at the fork point, and only the parent's own copy carries the
model the parent had in force for the record; the adoption rewrites
`records.model` by uuid and `tool_uses.model` by `tool_use_id`, and
extends `files.models` with the adopted models. With no original of the
identity present, the #653 fallback stands; a NULL winner model is never
adopted; a replay promoted after the original's deletion keeps the model
it adopted while the original was there. The pass rewrites only
non-canonical rows, so no rollup re-scope follows. The browser half is
the codex lane's record dedup (see SV-PARSER-SPEC).

## Foreign models are purged, not filtered (SV-SUPPRESSED-MODELS)

`suppressed_models(pattern, note, added_at)` lists models whose records
must not count. `ingest.purge_suppressed()` runs FIRST in
`_rebuild_derived_state()`, before the canonical pass, and DELETEs
matching `records` plus `tool_uses` whose own `model` matches (and, for
rows predating `tool_uses.model`, those on a matching record's line).

It is a delete, not a read-time predicate: every read and rollup treats
`records` as the truth. The `tool_uses` half matches the call's own
`model`, not a join on `(file_key, line_num)`, because most calls sit
on lines with no record (a Claude tool_use usually follows its
requestId's merged record; a lane call never shares a line with one)
and would keep counting. A full rebuild checks every row; an
incremental run checks only dirty files, since an unchanged suppression
fingerprint means stored rows were already purged.

Matching is `model ILIKE pattern`: `glm-%` covers a family, a bare id
matches exactly. The table ships EMPTY in `schema.sql` and must stay so
— the same code runs as glmmeter over the `zai` bucket, where `glm-%`
would suppress everything. Populate per deploy. Purpose: a lane switch
interleaves another provider's entries into a transcript this bucket
owns — real usage, but not ours, and priced against our table it
invents cost. Editing the table changes the derived-state fingerprint
(full rebuild). Removing a pattern restores rows only on reparse (bump
`PARSER_VERSION`); a derived rebuild cannot restore deleted rows.

`files.models` lists every model that answered in the file, recorded at
parse time so it survives the purge — the only trace of a lane switch.
An analysis that must skip mixed-lane sessions joins it against
`suppressed_models`.

## Project aliases fold identity at ingest (SV-PROJECT-ALIASES)

`project_aliases(pattern, project_id, note, added_at)` ships EMPTY in
`schema.sql` and is populated per deploy. It and `suppressed_models`
are bucket-external state the operator backs up and restores across a
rebuild.

Patterns are case-sensitive SQL `LIKE` (`%`, `_` wildcards, no
`ESCAPE`), because POSIX slugs are case-sensitive. The first match in
lexicographic `pattern` order (the PRIMARY KEY) wins. Each pass
resolves every stored id once against the pre-fold id set; aliases do
not chain within a pass, so a matched target advances one hop per pass.
Repeated passes converge without duplicating or losing files while the
alias set is acyclic on the ids it matches; a cycle is an operator
error (folded ids rotate on each ingest) — fix the table. A pass with
no matching source ids moves nothing; a project whose id equals its
target is never moved or deleted. Every alias-target id is labelled
with its own id on every ingest, regardless of its folded files' paths;
other projects keep their labels.

The fold runs at ingest before rollups rebuild, so every read path —
`/api/projects` included — sees only the target. Adding or editing a
row changes the derived-state fingerprint: full rebuild next ingest,
with no reparse, R2 fetch or `PARSER_VERSION` bump. With an unchanged
table, a fold alone does not force a full rebuild: moved files join the
dirty scope, and both source and target project-hour instants are
UTC-normalized and replaced. The zero-argument `rekey_folded_projects()`
reports moved keys to the active scope itself, preserving the ingest
phase's monkeypatch seam.

Deleting a row stops folding new files. Already-folded rows stay at the
target until another identity pass re-keys them: a reparse derives the
raw id from the object key; marker-backed lane files re-converge next
ingest to the marker slug's bounded alias-chain destination under the
remaining aliases (with none left, the marker slug). The walk stops at
an unmatched id, before a revisited id, or at eight hops, so it may
return an id that still matches an alias.

`/api/projects` keeps its inner join: a project whose files carry zero
usage records is not listed (pinned by test).

## A token type may be a SUBSET (SV-SUBSET-TOKENS)

`records.thinking_tokens` is part of `output_tokens`, not a sixth billed
slice: the API reports it under `usage.output_tokens_details`, and
`pricing` never sees it. It is in `api_dashboard.TOKEN_TYPE_FIELDS`
because that tuple drives zero-suppression and panel order. It must
NEVER enter an arithmetic total: not `t.total` in `src/app.jsx`, not the
Token Breakdown rows, not a cost. `usage_rollup` carries it as a
pre-aggregate — summed, never added to the other types.

## Aggregates are precomputed at ingest (SV-ROLLUP)

`usage_rollup` holds pre-summed usage at grain
`(session_id, hour, model, provider, is_main, long_context)`, rebuilt by
`ingest.rebuild_rollup()` after every successful ingest, AFTER
`recompute_canonical()` (it reads `is_canonical`).

A run's derived scope is the dirty file keys, affected record UUIDs and
tool-use ids, and the union of dirty `(project_id, hour)` keys from
`records` and `tool_uses`. Ingest captures old contributions before
reparse or orphan deletion, and current ones after persistence and
identity moves. Scope stores every hour as a UTC-aware instant, so
distinct fall-back folds survive; full-rebuild grouping uses
`date_trunc` in the DB session timezone. The seven hour-keyed rollups
delete by exact `(project_id, hour)` instant equality; the scoped
source query joins merged, disjoint, widened UTC intervals and requires
each candidate's own `(project_id, date_trunc('hour', ts))` to match a
dirty instant exactly (intervals cannot multiply a row).

`latency_rollup` replaces whole affected display buckets for each dirty
project and the all-projects row, because percentiles need the whole population. Buckets are epoch-aligned; each
dirty-hour instant replaces every bucket overlapping
`[hour, hour + 2 hours)`. Tied outlier latencies order by
`file_key, line_num`. A latency-bearing row with NULL `ts` forces a
full rebuild. Teammate resolution runs its full query every time;
changed files add their record hours to the scope because
`agent_rollup` reads `files.agent_type`.

`ingest_derived_state` stores the fingerprint of the last completed
derived semantics and the last full-rebuild time. The fingerprint covers
`constants.DERIVED_STATE_VERSION`, `PARSER_VERSION`, `PRICING_VERSION`,
the latency/context bucket constants, the default agent type, and the
ordered contents of `suppressed_models` and `project_aliases`. Bump
`DERIVED_STATE_VERSION` whenever rollup SQL semantics change (full
derived rebuild, no reparse). A full rebuild also runs when the state is
missing or incomplete (marker lifecycle: SV-SCHEMA-AUTOAPPLY), the last
full rebuild is over 24 hours old, dirty files exceed the smaller of
2,000 or 20% of the stored corpus, a lane identity rekey moves files, or
repricing changes any row's rate-derived data (a restamp-only pass
changes nothing).

After an out-of-band mutation of `records`, `tool_uses` or `files`, run
`DELETE FROM ingest_derived_state`; the next ingest rebuilds fully.

The grain is load-bearing; do not "simplify" it:

- **HOUR, not session.** A range sums only in-range hours; a session
  grain would count a boundary-straddling session whole.
- **MODEL.** A session's dominant model is `argmax(SUM(requests))` over
  in-range rows — what `MODE() WITHIN GROUP` gave over raw records.
- **`long_context`.** Each row prices by its own meter, so a re-derived
  breakdown reconciles with `cost_usd` (SV-DATED-RATES); a row summed
  across both meters could not.
- **`provider`** (`''` when none): one model prices differently per host
  (SV-PROVIDER-RATES).
- **`first_ts`/`last_ts`.** Burn-rate span `MAX(last_ts) - MIN(first_ts)`
  composes; a stored duration would not.

Only pure sums/counts/min/max may be served from it. **PERCENTILE_CONT
does not compose**, so `response_sizes` stays a live pass over
`records`.

The rollup is valid only for display buckets ≥ 1 hour: `/api/dashboard`
uses it when `bucket_s >= 3600`, otherwise a live subquery with the same
column names, so both paths share one query set. The 24h view (5-minute
buckets) is live.

It is derived state: anything mutating `records` outside ingest must
rebuild or clear it. Its totals (requests, tokens, cost, distinct
sessions) equal the `records` aggregate — keep that true.

`ctx_cost_rollup`, grain `(hour, project_id, model, ctx_bucket)` holding
`requests`, `total_tokens` and `cost_usd`, serves cost-by-context:
`usage_rollup` sums tokens per (session, hour, model), destroying the
per-call window size the x-axis needs. Its columns are pure sums.
`total_tokens` drives the panel's tokens variant — the only measure a
free lane has.

A cost panel is HIDDEN when the range cost nothing, never drawn as
zeros: Cost by Model, Cost by Agent Type, Cost by Context Size, Token
Breakdown's cost half, the total-cost card and the heatmap's cost
metric. Their tokens counterparts always show.

Its bucket edges (`constants.CTX_BUCKET_WIDTH` / `CTX_BUCKET_MAX`) are
baked into stored rows like `LATENCY_BUCKETS`: changing either needs a
rollup rebuild, not a `PARSER_VERSION` bump (context size derives from
stored `fresh + cache_creation + cache_read`).

`records` cascades from `files`, `files` from `projects`. Reparse is
idempotent: deleting a file's `records` and re-ingesting leaves the
table byte-identical.

## Failure causes and dispatches are stored, not re-derived (SV-WHY-COLUMNS)

`tool_uses.is_error` says THAT a call failed. Four columns say why, and
what a dispatch asked for:

- `error_kind` — `rejected` | `tool_error` | `failed`, NULL unless the
  call errored. HARNESS-GENERIC: a PreToolUse hook denial carries the
  deploy's wording, so it lands in `failed`; never add a kind for one
  operator's hooks. `tool_error` means the call RAN (a
  `<tool_use_error>` wrapper, or a Bash result opening `Exit code N`).
  `rejected`/`failed` never ran, so they carry no `write_targets` and no
  churn. Lane wires have no status field, so the lane classifier reads
  the failure text: errored and not a recognizable rejection →
  `tool_error`; errored with no readable text → `failed`.
- `error_text` — the first `parse.ERROR_TEXT_MAX` chars of the failed
  result. Grouping on it separates hook denials from real failures, so
  the parser needs no hook vocabulary.
- `agent_type` / `agent_model` — from `Agent`/`Task` call arguments, a
  Codex `spawn_agent` / `multi_agent_v1__spawn_agent` call's arguments,
  and a Kimi `Agent` call's (`subagent_type`, `model`).
  `files.agent_type` records what RAN (only when the subagent wrote a
  JSONL); these record what was ASKED, so a fileless dispatch is still
  attributable. Codex dispatches keep `dispatch_prompt_chars` /
  `dispatch_brief_ref` NULL: their `message` is encrypted.
  Both sides store the canonical name (`agent_types.canonical_agent_type`,
  issue #650): one role is one name across lanes (Claude's `Explore`,
  Kimi's `explore`, Codex's `explorer` are one), a plugin-namespaced
  type folds to the type (`superpowers:code-reviewer` → `code-reviewer`),
  Kimi's `coder` profile is the implementer role, and Codex's `worker` —
  the lone lane default — is not a role at all: it lands in
  `DEFAULT_AGENT_TYPE`, the bucket a transcript that named nothing shares. A workflow-tree
  sidecar's `agentType: workflow-subagent` is a container marker, not a
  role: it stands only when nothing more specific was recorded, and an
  in-band role outranks it (the standing sidecar precedence).

`tool_error_rollup` and `dispatch_rollup` carry the composable counts.
`error_text` never enters a rollup grain (unbounded cardinality).

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

## The parser and pricing versions are code, never the environment (SV-PARSER-VERSION)

`constants.PARSER_VERSION` invalidates stored PARSE results: bumping it
reparses every stored file, refetching each from R2.
`constants.PRICING_VERSION` invalidates stored PRICES: bumping it
reprices stale records in place from their stored tokens via
`pricing.compute_cost`, batched, no R2 fetch (SV-REPRICE). A
parse-semantics change bumps PARSER_VERSION; a rate-data change bumps
PRICING_VERSION. Both live in code so the bump ships in the same commit
as its cause; never reintroduce an env override.

## Rates are a function of (model, timestamp) (SV-DATED-RATES)

`pricing.rate_for(model, ts)`: a model may carry dated overrides in
`DATED_RATES`, `(end_exclusive_utc, rates)` windows per exact model key,
derived from its history in `src/pricing.json` (SV-RATE-DATA). Price at
the request's timestamp, never render time: `parse.py` passes each
record's own `ts`; omitting `ts` yields LIST price (never a silent
discount). The read-time fold agrees: `rate_epoch_sql` maps a NULL ts
to epoch -1, whose representative instant is None, pricing exactly as
persist and reprice did. The cache view's range predicate admits a
NULL-ts record in every range, so the fold decomposes the same record
its stored cost describes.

An expired window is NEVER dropped. Every `PRICING_VERSION` bump
reprices every record through the windows (SV-REPRICE; a record stamped
by a newer binary is skipped), so removing a window reprices its history
at list. The machinery is tested through `tests/conftest.py`'s
`synthetic_dated_rate` fixture, not only live entries, so it cannot rot
while the table is empty.

A read path that RE-DERIVES rates from summed tokens must group each
record by ITS OWN rate epochs — the instants where
`pricing.resolve(model, ts, provider)` can change for its (model,
provider), listed by `rate_boundaries` — AND by `COALESCE(long_context,
FALSE)` (the Codex meter multiplies the whole input side by 2 and output
by 1.5; a fold ignoring it drifts from `SUM(cost_usd)`). Totals always
come from stored `cost_usd`; never recompute them at read time.

Epochs are per (model, provider), never the global `pricing.RATE_EPOCHS`:
a log-backed provider row adds thousands of boundaries, and a fold over
the global list splits every record at every other row's changes. So
the cache view reads the distinct (model, provider) pairs from
`usage_rollup`, joins each record to its pair's boundary array, and
takes `width_bucket` over it. A pair `usage_rollup` does not list yet
(ingested before its rollup rebuild) falls back to the global list:
exact, only slower.

## Rates are data in one file (SV-RATE-DATA)

Every rate lives in `src/pricing.json`: `models` (normalised key →
history), `providers` (normalised model → provider → history),
`provider_rates_fetched`, and `openrouter` (the account's data region
and each provider-table model's OpenRouter id, SV-RATE-REFRESH).
`backend/pricing.py` and `src/rates.js` hold logic only and both read
it — the backend at import through `backend/pricing_load.py`, which
`pricing.py` re-exports, the browser synchronously before first use
(node reads it beside the module). The browser fetches the URL in the
pricing-loader tag's `data-pricing` attribute in `public/index.html`,
cache-busted like every `/src` asset; the file sits in `src/` because
that is what the app serves. No rate literal belongs in either source
file.

Beside the tables sits `long_context_meters`: a map of
`long_context_models` member keys to exactly `{"threshold": N}` for a
positive integer N — the model's own long-context threshold (issue
#765), an override of `pricing.LONG_CONTEXT_THRESHOLD`; a member absent
from the map keeps the global default, a key naming no member is
refused, and the meter thresholds ride the reprice pass's
rate_fingerprint. Both loaders validate and fold it (the browser to
`window.longContextMeters`).

Each row's history is append-only, oldest first. Every entry carries
five finite non-negative rates (`fresh`, `create_5m`, `create_1h`,
`read`, `output`), an optional string `note`, an optional `schedule`, and
— on a provider entry only — an optional `band`: a subset of the five
rate fields mapped to a `[min, max]` pair of finite non-negative
numbers with `min <= max`. A `band` is a record
of what the host moved inside, never a price: the five rate fields
beside it stay the priced rates, and both loaders check a band and
ignore it (SV-RATE-REFRESH). Nothing writes a `band` beside a
`schedule`, and the loaders do not refuse one if it appears: the band is
read for its shape only, and the schedule still decides which hours
price what. A model row's first
entry has `from: null` (all of time). A provider row's first may name
its start instant; before it, that host's records price by the model
alone, and the start joins `RATE_EPOCHS`. Every other `from` is exactly
`YYYY-MM-DDTHH:MM:SS` plus `Z` or `±HH:MM`, every field in range (a
real calendar day, hour 0-23, minute/second 0-59, offset under 24:00),
strictly after its predecessor. The newest entry is the list price;
each earlier one applies until its successor's `from` (SV-DATED-RATES).

A price change APPENDS `{"from": T, ...}`. An entry is never edited or
removed, except to correct a misstated price or instant (a wrong seeded
price; sampled entries dated at detection where OpenRouter's log has
the real change points — the SV-RATE-REFRESH backfill), and only in a
human commit that bumps `PRICING_VERSION`, which reprices from stored
columns with no reparse. Both loaders refuse a rule-breaking file,
naming the row; the browser throws an error naming `pricing.json` on
any load failure.

A provider entry may carry a weekly UTC `schedule`: a list of windows
`{days?, start?, end?, rates}`.

- `days`: distinct lowercase weekday names; absent means every day.
- `start`/`end`: HHMM JSON integers 0–2359, both or neither (neither is
  the whole day), end-exclusive, wrapping past midnight when `start` is
  later than `end`. A fraction or exponent spelling (`1400.0`) is
  refused on both sides. A wrapped window's `days` are the record's own
  UTC weekday, not the day the window opened.
- `rates`: the five rates.

Both loaders price a record by its UTC weekday and time: the first
window it falls in, else the entry's own rates (also the price with no
timestamp). Windows repeat weekly, so they are not rate epochs. The
browser prices each record exactly. The read-time fold (`api_common`)
cannot: a scheduled row's buckets take their split from the rates at
the epoch's representative time, scaled to stored `cost_usd`. The total
is always exact; the split is exact when every window scales all five
rates alike, as the live schedules do.

A provider entry's `note` may carry a per-request fee the refresh
records instead of refusing (`RECORDED_FEES`, SV-RATE-REFRESH): one
part per fee, `<fee> $<amount>/request not modelled: per-request,
unpriceable from token counts`, joined with `"; "` beside a discount
note. Both loaders parse the note into a per-entry fee and refuse a
fee-shaped part that does not match the shape in full (a real cost is
never dropped in silence — issue #469); a part with no `/request` in
it (a discount) parses no fee. The fee in force resolves exactly like
rates — the entry at the record's own `ts`, the newest entry when `ts`
is absent — and `pricing.compute_cost` folds it in ONCE per call, so
`cost_usd` is what the session cost; `records.request_fee_usd` (psql
only, like `error_text` — no endpoint, panel or rollup) stores it
beside the cost for provenance. `rate_fingerprint` covers the fees,
so a note edit reprices the pair. The read-time fold cannot re-derive
a fee from tokens: a fee row's buckets take their split from the
token rates and are scaled to the stored total, like a scheduled
row's (the total stays exact; the split is exact for the tokens).
Lanes name no serving host, so a lane record's fee is NULL and its
price is unchanged.

The file keeps the `json.dumps(doc, indent=2, sort_keys=True)` layout,
so any writer reproduces it and a one-rate change is a one-line diff.
File order never decides key matching — the LONGEST matching key wins;
order only breaks a family-fallback tie between equal versions (first
key wins).

## Provider rates refresh from OpenRouter (SV-RATE-REFRESH)

`.github/workflows/refresh-pricing.yml` runs
`scripts/ci/refresh_provider_rates.py` hourly (identically on dispatch)
and commits to `master` as `github-actions[bot]` only after the full
suite passes on the new data. A hand edit to a provider row follows the
same rules:

- A moved price is an APPENDED entry, never an edit or deletion. A
  log-backed row dates it at OpenRouter's recorded change point; a
  sampled row at detection time. A first-seen sampled host gets a row
  beginning then; a first-seen log-backed host gets its whole log. A
  host no longer listed keeps its row untouched and is reported.
- Normalisation: USD per token becomes USD per million. The listed
  price already has any promotional discount applied; the discount goes
  in `note` (`N% off`), never a rate. Cache writes take the listed
  write price when nonzero, else the input rate; an unlisted cache-read
  price is 0. A listed `input_cache_write_1h` is the `create_1h` rate,
  the one number the TTL split turns on (SV-COST-SPLIT); absent or
  zero, the 5m tier is it too, so a listing that does not split them
  changes nothing. A host whose listing splits them is sampled, never
  log-backed: the log's five fields carry no 1h tier, so no series can
  be joined to it. Endpoints of one host at one price are one row.
- The account is billed only by endpoints in its data region,
  `openrouter.data_region`: `global` or a lowercase region code.
  - An endpoint tag is `host` or `host/<suffix>[/<suffix>...]`. A
    suffix is a region when it is a known region code (`us`, `eu`,
    `uk`, `ca`, `ap`, `asia` and the others the script lists), alone or
    as `<region>-<area>[-<n>]`, in any case (`us-east-1`). A suffix
    that is neither a region nor a known quantization (`fp4`, `fp8`,
    `nvfp4`, `bf16`, …) is logged in the run's notices and refuses
    nothing.
  - `global` takes the endpoints with no region suffix — every suffix
    that names no region, so `azure/global` and `google-vertex/global`
    are listed and `google-vertex/europe` is not. `global` is
    deliberately NOT a region code: adding it would drop the very
    endpoints that carry it.
  - A host whose endpoints all lie outside the region is not listed for
    the account, so it is reported as vanished.
- Endpoints are grouped by host first, so a malformed endpoint refuses
  only its host.
- **Schedules.** A host's `pricing.overrides` (weekly `utc_days` /
  `utc_start` / `utc_end` windows with the prices they override) become
  its entry's `schedule`. A change to the default rates or to the
  schedule (compared whole) is a move, and the whole entry is appended.
  - **The default.** OpenRouter lists a scheduled host's top-level
    price as the window active at fetch time. So the default comes from
    the top-level price only when the fetch is outside every window;
    inside one, the stored default is kept and only the schedule is
    compared (a whole-week schedule never moves its default). A
    first-seen host fetched inside a window is REFUSED unless its
    schedule covers every instant of the week — then the default prices
    nothing, so the row starts with the listed top-level price and a
    notice reports the seeding; otherwise the next fetch outside every
    window starts the row.
  - A price a window does not name is the entry default (top-level
    outside every window, the kept default inside one).
  - An appended entry whose windows do not each scale all five default
    rates by one factor is reported as a "non-uniform schedule" notice:
    the fold's Token Breakdown split is then approximate (SV-RATE-DATA).
- **Log-backed rows are dated by OpenRouter's own change log**, a
  per-endpoint price history its model pages read and its documented
  API lacks:
  `https://openrouter.ai/api/frontend/v1/stats/listed-pricing?permaslug=<canonical_slug>&variant=standard&shape=v4&range=all`
  (`canonical_slug` from `/api/v1/models`). Per listed endpoint it
  returns `endpointId`, `providerName`, `providerSlug` and, per field
  (`input`, `output`, `cacheRead`, `cacheWrite`, `discount`), change
  points `{at, value}` in USD per million, discount applied, since
  first listing. Every run fetches it with `range=all` for every
  tracked model, beside the endpoints listing.
  - **Joining series to endpoints.** A series carries no tag. A host is
    log-backed only when: all its endpoints share one tag prefix
    (before the first `/`) no other host of the model uses; the log has
    exactly one series per such endpoint; each series' current state
    (newest rates, normalised below) equals exactly one endpoint's
    listed price, no two series matching one endpoint; no endpoint or
    series carries a schedule (`pricing.overrides` or a series
    `schedule`); the host has no `cheapest` resolution; and the
    data-region filter or tag pin selects exactly ONE endpoint. The
    row's history is that endpoint's series. Anything else is
    ambiguous, and the host is sampled, never guessed.
  - **Change points to entries.** A series' state at an instant is each
    field's newest value at or before it, and exists once `input` and
    `output` both have a point. Each instant at which the five rates
    change — normalised like the listing: cache read null/absent is 0,
    cache write null/absent/0 is the input rate, values rounded to 10
    decimal places — is one entry, `from` truncated to whole seconds
    (points in one second collapse to its last state). A discount-only
    change is not an entry; `note` is the discount in force at `from`.
    A `null` `input` or `output` is not a price: the host is sampled.
  - **Hourly append.** For a log-backed host with a row, every entry
    whose `from` is after the row's newest `from` and whose rates
    differ from its predecessor is appended, oldest first, so a flip
    between runs is recorded as its two moves. A first-seen host gets
    the whole series from its first change point. The hourly run never
    rewrites an entry.
  - **The log must match the listing.** The series' state at the fetch
    instant must equal the listed price fetched in the same run: the
    run reads each series truncated at that instant, so a point dated
    after it — an announced change not yet in force — never backs a row
    or enters its history, and lands when a later fetch instant passes
    it (the log is refetched in full each run). Otherwise the host is
    sampled this run, with a notice.
  - **Sampled rows.** A host the log does not back, and every host of a
    model whose log fetch fails (HTTP error, timeout, `canonical_slug`
    missing from `/api/v1/models`, unrecognised shape), refreshes by
    sampling: a move is an entry dated at detection, under the
    alternation rule below. A failed log never guesses or refuses: a
    "listed-pricing log unavailable" notice names the model and reason,
    and the run stays green. A host the log does not back goes in the
    run's report with the reason, not its notices.
- **The one-time backfill.**
  `scripts/ci/backfill_provider_rates.py --as-of <instant>` replaces
  every log-backed row's history with the log's entries up to
  `--as-of`, keeping the row's start: when the old first `from` (`null`
  included) precedes the log's first change point, the new first entry
  takes it. Unbacked rows are left untouched and listed. It is an
  SV-RATE-DATA correction, landing only in a human-reviewed commit that
  bumps `PRICING_VERSION` and names the invocation and each row's added
  entries.
- **Alternating prices are reported, not appended — sampled rows
  only.** On a sampled row, when neither the listing nor the newest
  entry has a schedule and the listed rates equal a non-newest entry
  whose `from` is within 7 days of the detection time (never wall
  clock), the run reports an "alternating price" notice naming host and
  entry, and appends and bumps nothing. A first move away never
  matches; the flip back does. A genuine return to an older price is
  hand-appended, and the next run compares against it.
- **An oscillating host is recorded once, as a BAND (issues #640, #664).**
  A provider entry carries an optional `band`: each rate field it names
  maps to a `[min, max]` pair of FINITE non-negative numbers with
  `min <= max`, the range the host actually moved inside; a field it does
  not name is unconstrained, and a bound that is not finite is refused,
  because `x <= inf` is true of everything and the row would go
  permanently silent. The five rate fields beside a band stay the PRICED
  rates — the time-weighted mean over the window that formed the band, the
  classifier's trailing `days`: the level in force at its open — the last
  entry dated before it, or the row's leading undated entry when nothing
  dated precedes the window, which is the classifier's baseline too (issue
  #836) — plus every entry in it, the mean weighting
  that window so a level predating it weighs only from the window's open
  (issues #663, #665) — so no rate
  math reads the band; the
  loaders accept and check it (`pricing_load.check_band`, mirrored by
  parser.js) and ignore it. Nothing writes a `band` beside a `schedule`,
  and the loaders do not refuse one if it appears: the band is read for
  its shape only, and the schedule still decides which hours price what.
  The hourly run forms a row's band itself, on the log path (issue #664):
  an unbanded row whose window classifies TOGGLE or BAND appends its new
  states plus ONE band entry dated at the detection instant — every
  earlier level stays an actual entry, so a record priced before the
  detection instant prices at the level that held there. A banded row the
  run later SAMPLES falls back to appending every move: the alternation
  rule and the band are independent, and only the log path carries the
  band. `collapse_oscillating_rates.py` remains the one-time,
  human-reviewed whole-history pass: it collapses each row
  `price_band.classify` calls TOGGLE or BAND to the ONE band entry its
  window leaves, leaves STEP and STABLE rows byte-identical, and reports
  the distinct host names it collapsed so the reviewer can run the
  records query over exactly those before merging (a mean is only safe
  while no record has been priced through that host).
- **An escape re-forms the band or follows the step (issue #665).** On the
  LOG path, a row whose newest entry carries a band is appended to at
  most once per run: every new state inside the band makes NO Move at all
  (a Move with no entries in it would still bump the version, write both
  files and commit), and the states outside it append ONE entry. The run
  classifies the window with the new states folded in. Still oscillating,
  the entry RE-FORMS the band — the range the window's levels span,
  priced by their time-weighted mean, dated at the last escape's change
  point, carrying the banded entry's note and schedule. The window no
  longer oscillating (STEP or STABLE), the entry appends the state now in
  force instead, dated at its own change point, so the row follows the
  price.
  The mean's window ends at the detection instant, where the level in
  force holds until it, so a re-formed row tracks the price it names.
  The entry is APPENDED, so an undated banded entry keeps its
  `from: None` and the row keeps covering every record: only a row whose
  FIRST entry names an instant stops existing before it.
- **Unmodelled pricing refuses the host, unless it is a RECORDED fee:**
  an override kind the script does not model (e.g. a `min_prompt_tokens`
  tier), or any other pricing key at a nonzero price. The one exception
  is `web_search`, a per-request fee no token count can price: it
  enters no rate, and is written into the row's `note` with its unit
  beside any discount note, so the row says what it cannot price
  instead of pricing a call that in fact cost more. Two endpoints
  differing only in the fee refuse rather than collapse into one row. A
  fee-carrying host is SAMPLED on both paths: the log's five fields
  carry no per-request cost, so a log-backed row would hold no record
  of it.
- A run that appends bumps `PRICING_VERSION` to one past the value in
  `backend/constants.py` — never a literal — in the same commit
  (records at or after a new `from` ingested before the deploy were
  priced at the old rate), and moves `provider_rates_fetched`. A run
  that appends nothing writes nothing. A new entry adds a rate epoch
  only to its own row's grouping (SV-DATED-RATES).
- Ambiguity is never guessed. Each of these refuses its host or model,
  leaving its rows untouched:
  - a host with two in-region endpoints at different prices (e.g.
    quantization variants) and no resolution, named by tag — an exact
    {`p`, `p`/fast} tag pair excepted: the fast-tier rule (below) takes
    its base endpoint;
  - a stale resolution: a pinned tag not listed, or `cheapest` twins
    that differ beyond price, tie in price order, or have flipped
    order;
  - a resolution keyed on anything else, a price included;
  - an unrecognised response shape;
  - a tracked model still in the catalog with no endpoints, or none in
    the data region. A model gone from /api/v1/models is a delisting,
    not a refusal: the run skips it with a notice, its provider row
    keeps pricing stored records, and it rejoins the refresh on its own
    when the catalog relists it. An unreadable catalog proves nothing,
    so the empty-endpoints refusal stands while it cannot be read.

  A refusal blocks only itself: every other move is appended, tested
  and committed with the bump, then the run exits nonzero, naming each
  refused host. A detection time not after a row's newest entry leaves
  that host untouched with a notice — appending after it would refuse
  the file — and blocks nothing else.

- The fast-tier rule resolves one shape with no human decision: when a
  host's in-region endpoints are exactly one tagged `p` and one tagged
  `p/fast` at different prices, the refresh takes the base `p` endpoint
  automatically and the run's report records it as rule-resolved — a
  `/fast` tier is a distinct offering under its own tag, not a price
  twin. The rule fires only where no resolve entry exists for the host
  (an explicit resolution keeps precedence, refusals included), takes
  the base whichever of the two is cheaper, and never extends to the
  price log's own endpoint selection: a {`p`, `p`/fast} host the log
  would otherwise back needs its base pinned to stay log-backed.
- The fix is a human decision recorded in
  `openrouter.models.<model>.resolve.<host>`, with a `why`:
  - `{"tag": ...}` takes that tag's endpoint, whatever its region.
  - `{"select": "cheapest"}` takes the cheaper of endpoints identical in
    tag, quantization and limits, comparing cache read, then input,
    then output. Region-premium twins are dearer, so under `global` the
    cheaper twin is the one the account reaches. The order survives
    price moves and breaks only when it flips; twins have no identity
    but price, so a flip is seen when the twin still at the row's price
    is no longer cheaper, and is refused. A tracked twin crossing the
    other between runs is indistinguishable from a genuine move and
    shows as a rise (unless the other fell simultaneously), so every
    rise of a `cheapest` row is appended and reported as a "possible
    twin switch".
  - The two combine, the tag narrowing first, whatever the region. The
    choice may record `"ignore": [fields]`: non-tag identity fields
    (quantization, context_length, max_completion_tokens,
    max_prompt_tokens) that a recorded human decision has found to be
    one offering's listing artifact, so `cheapest` compares the rest;
    the tag, the price order and every flip and tie refusal stay.
    Without the record the artifact refuses like any other difference.
  - A log-backed host resolves through the same shapes: a pin carrying
    `select`: `cheapest` — alone, with a tag, or with a recorded
    ignore — has no stable endpoint identity and stays sampled; a bare
    tag pin selects by it, and must name exactly one endpoint.

  A resolution is never a price value: a price pin breaks the moment
  the price moves.

## First-party vendor rates refresh from OpenRouter (SV-VENDOR-RATES)

The same hourly run refreshes the four vendors' first-party list prices
(`src/pricing.json`'s `models` table) and `long_context_models` membership,
selected by VENDOR PREFIX over OpenRouter's catalog — every id under
`anthropic/*`, `openai/*`, `moonshotai/*` and `z-ai/*` — at the vendor's own
first-party endpoint (the endpoint whose tag prefix is the vendor's own
namespace), never a third-party host. No per-model allowlist: a model the
vendor adds under its prefix is picked up on the next run. The pass lives
in `scripts/ci/refresh_vendor_rates.py` and runs inside
`refresh_provider_rates.main()`: one report, one commit, refusals block
only their own model. PRICING_VERSION bumps once, one past the file, when
either pass moved.

- **Variants are never rows.** A catalog id with a `:<suffix>` variant
  (a `:batch` discounted tier, a `:free` tier — priced at zero by resolve()
  before any table — or any other suffix) is skipped: the bare id is the
  model.
- **The vendor endpoint.** The BARE tag (namespace, no suffix) is the list
  price; the vendor's other tags are its own service tiers (fast, flex) or
  quantizations. With no bare tag, the vendor-prefix endpoints must agree
  on one price (equal prices collapse); several prices refuse, and a
  `{"tag": ..., "why": ...}` pin recorded in
  `openrouter.vendor.resolve.<derived key>` takes one. No vendor-prefix
  endpoint at all is a NOTICE + skip (the model is offered only through
  third-party hosts; any row's own history stands).
- **The derived key** is the slug, dot-folded (`openai/gpt-5.5` →
  `gpt-5-5`) — the normalisation resolve() applies to a transcript naming
  the bare first-party id. The parity is a bare-id claim: a transcript
  spelling the vendor prefix is OpenRouter provider-row traffic, priced by
  that table by design.
- **A listed price change APPENDS**, never rewrites: a hand-curated row
  whose vendor source has moved on gets the appended entry too (the listing
  governs a row the refresh owns — where a vendor's real first-party tier
  differs from OpenRouter's listing shape, the listing still wins; the
  correction is a human commit). Rows with no vendor source (aliases, the
  delisted) stand untouched, and a quiet source writes nothing.
- **The long-context band folds to the meter.** A `min_prompt_tokens`
  override whose multipliers equal the meter's (`INPUT_MULT` /
  `OUTPUT_MULT`) is the meter at the band's own threshold (issue #765):
  it sets `long_context_models` membership and lands the threshold in
  `long_context_meters` (a member key's `{"threshold": N}` override of
  the global default; a member absent from the map keeps
  `pricing.LONG_CONTEXT_THRESHOLD`), and contributes NO rates: the five
  stored rates stay the sub-threshold listing, and compute_cost applies
  the meter above the model's own threshold. Membership and threshold
  follow the listing for vendor-tracked keys; non-vendor keys stand
  untouched; a threshold move rewrites the meter. A band departing the
  meter's multipliers is NOT TRACKED — the notice rule below.
- **An unmodelled shape is a NOTICE, never red.** The pass notices it as
  "not tracked: <reason>" and moves on: no row is created and no existing
  row is touched. Red is reserved for ambiguity a human must resolve
  (multi-price without a pin, a stale or malformed recorded pin) and for
  broken or unrecognised fetches — each clearing on a human action or a
  retry, never standing every hour.
- **Fees are provenance notes, never the priced fee shape.** A RECORDED_FEE
  (web_search) at a nonzero price enters the entry note WITHOUT the
  `/request` note shape: the models table prices first-party traffic, whose
  requests pay no per-request fee, and the priced note shape is exactly
  what the loaders fold in once per request. Discount notes as provider
  rows.
- **A weekly schedule is not tracked** (notice): a models row carries no
  schedule (the loaders admit one on provider rows only).
- **The loaders run on the would-be file** before anything is written; a
  failure writes nothing.

## The reprice pass recomputes stored prices in place (SV-REPRICE)

`backend/ingest_reprice.py` recomputes rate-derived STORED state for
records whose `pricing_version` differs from `constants.PRICING_VERSION`
(NULL is stale), from stored columns only — the same
`pricing.compute_cost` the parser runs, over each row's own tokens and
`ts` — so a rate change never refetches R2. Rows update in batched
transactions. Each batch recomputes its rows, writes rows whose cost or
flag moved in one set-based UPDATE, and re-stamps the rest with the
current version in one set-based UPDATE — a restamp advances the
staleness marker only, so the pass's count, and every gate that reads
it (full-rebuild promotion, response-cache invalidation, ingest_done),
tracks rows whose rate-derived data actually changed. A stored version
that parses as an int NEWER than the binary's is skipped, never
overwritten. It runs in `_rebuild_derived_state` between suppression
and the canonical pass; a reparse stamps the current version, so fresh
rows never reprice. If it changes any row (a cost or a flag moved — a
restamp changes none), the run takes a full derived rebuild, since
repricing can move rollups outside the dirty files. No endpoint, panel
or rollup reads `pricing_version`. The recomputed state is `cost_usd`
(the per-request fee folded in, SV-RATE-DATA), the long-context flag
(`records.long_context` — membership-keyed since issue #765: a MEMBER
row re-derives whatever its stored flag, so a refresh fold move
converting the pre-fold NULL rows lands exactly what a reparse stores;
a NON-MEMBER row keeps its stored flag — except a stored TRUE, which
only the Codex path's threshold test can have written, re-derived so a
lapsed member's TRUE unbills (issue #833)), and the fee column
(`records.request_fee_usd`), re-derived from the same columns under the
same switch. The completion marker follows SV-SCHEMA-AUTOAPPLY.

Pair-qualified staleness: before the keyset loop, the pass classifies
the stale `(model, provider)` pairs with one DISTINCT scan and
SQL-restamps, in one set-based UPDATE, every stale row whose stored
`rate_fingerprint` equals its pair's CURRENT fingerprint — the fp
covers every rate input `pricing.resolve()` consults (both resolution
branches, windows, schedules, start, tier, default, free shape) plus
the pricing modules' source (`backend/rate_fingerprint.py`), so an
edited entry, a correction, a schedule change or a logic change all
move it while an untouched pair's stands still; the recomputation for
matching rows is the identity by construction and reads zero rows into
Python. The SQL restamp set is exactly `{1-9-digit plain-digit
versions <= V}`, a subset of the keyset path's (Python's `int()`
parses spellings the SQL cast refuses), and the guard still protects
newer-version rows on both paths. NULL fp is the conservative stale
shape (pre-feature rows, older binaries): one recompute, then clean.
An out-of-band mutation of cost-relevant columns sets
`rate_fingerprint` NULL alongside `DELETE FROM ingest_derived_state`.
A PRICING_VERSION bump that moved no pair's data restamps only.

## Brand values escape per context (SV-BRAND-ESCAPE)

`APP_NAME` / `APP_TITLE` / `APP_DESCRIPTION` / `APP_PRIVACY_NOTICE_URL`
are config, and config is hostile input. Each injection context has its
own function (html and script payload and the URL-attribute allow-list
in `backend/branding.py`, the export-filename slug in
`backend/api_export.py`); never hand-concatenate a brand value into a
page:

- HTML text/attributes (title, meta, logo, sign-in page): html-escape.
  `<title>` and `<meta>` are replaced before the injected script block
  in `public/index.html`, so the `count=1` substitutions hit the real
  elements.
- The `window.BRAND` script payload: script-context escaping, which
  also neutralises `</script>`, `<!--` and U+2028/U+2029 (raw ones are
  a JS string syntax error; `<!--` opens a legacy HTML-like comment).
- A URL attribute (the sign-in page's privacy-notice `href`):
  `branding.url_attr` — an ALLOW-LIST, never a `javascript:`
  deny-list: after stripping the leading/trailing C0-controls-and-space
  the URL parser strips, only `http://`/`https://` or a site-relative
  path (leading `/`, not `//`) is accepted, returned already
  html-escaped for the double-quoted attribute. A refused value drops
  the link and logs a warning (a display-only setting must not fail
  startup or drop silently). Escaping alone cannot make a URL safe: the
  browser entity-decodes and strips whitespace before it reads the
  scheme.
- The export-PNG `Content-Disposition` filename: slugified to
  `[A-Za-z0-9._-]`.

A new surface echoing a brand value MUST route through one of these —
not a copy, not a new escape, never `f"{value}"`.

## The first boot of this build over an existing DB re-keys every file (SV-REKEY-CUTOVER)

The first boot of THIS BUILD over ANY database from an earlier build
re-keys stored identity from the bare object key to
`<bucket>/<object-key>`, whatever `R2_BUCKET` says, deleting and
re-inserting each row. Adding a bucket on an already-migrated deploy
re-keys nothing. It is one-time and converging, with windows that close
when the first clean run finishes — run it off-peak and watch `/health`:

- (a) during the run, new rows (default canonical) and unswept old rows
  are both canonical, so reads double-count; kimi-code and legacy tool
  ids are `file_key`-scoped and cannot dedup at all.
- (b) a fatal mid-run keeps the double-count until the next clean run.
- (c) a transcript or sidecar request for an unswept old row errors
  (its key names an unconfigured bucket).
- (d) a per-object fetch failure drops that file's rows until the next
  hourly run.

A fresh DB has none of these.

## Rates may be keyed by serving host (SV-PROVIDER-RATES)

An OpenRouter record names its serving host in `message.provider`,
stored as `records.provider`. `pricing.resolve(model, ts, provider)`
uses `PROVIDER_RATES[(normalised model, provider)]` when that row
exists (a dated permaslug such as `-20260731` folds to its `-0731`
slug), else the model alone. A record with NO provider (every other
lane) always prices by the model alone: z.ai's GLM must never take an
OpenRouter host's rate. Free ids (`:free`, `stealth/`) stay zero ahead
of both.

Provider rows follow SV-DATED-RATES: windows in `PROVIDER_DATED_RATES`,
boundaries joining `RATE_EPOCHS`, and every re-deriving fold groups by
provider as well as epoch. Both sides read them from `src/pricing.json`
(`window.providerRates` in the browser).

## Model resolution flags estimates (SV-RATE-ESTIMATES)

`pricing.resolve()` returns `kind`: `exact` | `tier` | `default`.

- Ids are normalised first: provider/region prefixes stripped
  (`us.anthropic.`, `anthropic/`) and `.` → `-`, so `claude-opus-4.8`
  resolves like `claude-opus-4-8`.
- EXACT allows only a dated-snapshot (`-20250514`) or bracket (`[1m]`)
  suffix after a key. A short version suffix must NOT match a shorter
  key (billing `claude-opus-4-9` at a shorter key's retired rate is a
  silent 3x overcount).
- Unmatched Claude models fall back to their family's
  current-generation LIST rates as `tier`; anything else is `default`.
  Non-exact results surface as `estimated_rate` in the API, so a guess
  is never presented as fact.

Never invent a rate for an unpriced variant (e.g. `-fast`): let it fall
back and be flagged.

## CI thresholds are self-raising ratchets (SV-CI-RATCHETS)

Coverage and module size are governed by
`.github/ci-thresholds.json`, validated by `scripts/ci/thresholds.py`
and enforced in `tests.yml` — never hand-set numbers in a workflow.

- Coverage: per language (`python`, `javascript`), `measured` and
  `floor = measured − 1.5`. On a `master` push,
  `scripts/ci/ratchet.py --language <language>` raises a calibration
  only when the run beats `measured` by more than the 1.5 hysteresis.
  Never lower a floor by hand, for any reason.
- Python measures the full pytest run (`--cov=backend`). JavaScript
  runs the node-executing tests under `NODE_V8_COVERAGE`, folded with
  c8 over `src/**/*.js` — the files node executes. `.jsx` panels are
  outside it: node parses no JSX, and parity tests' eval'd fragments
  are attributed to the eval. Each language gates against its own
  floor in `tests.yml`.
- **The gap sits ABOVE the measured value for every cost family, and
  BELOW it for every quality family.** This is not a typo: coverage is
  a QUALITY (higher is better, `floor = measured - 1.5`); a cost
  family's `floor` is the CEILING the recorded value may be exceeded
  by, `floor = measured + 1.5`, and only a run that got CHEAPER moves
  it. Writing a cost family the coverage way round puts the ceiling
  below the measurement that recorded it, and every later run at that
  measurement fails a gate no change can satisfy.
- Reparse (`reparse`): one record per phase of one reparse pass
  (`scripts/ci/reparse_bench.py`, called from `tests.yml` through
  `.github/actions/reparse-bench`), each phase carrying TWO instruments
  because each sees something the other cannot — and only ONE of them
  gates (issue #513):
  - `bytecodes` — hundreds of bytecode instructions per file, counted
    with `sys.monitoring`'s INSTRUCTION event
    (`scripts/ci/reparse_phases.py`). Exact: the same tree retires the
    same number on a loaded machine and an idle one, under any
    `PYTHONHASHSEED`. It catches the uniform per-file slowdown, and
    needs no amplification to be stable; its gap is sized for
    interpreter drift, not for noise. This is the GATED instrument: the
    enforced set is `thresholds.REPARSE_GATED_METRICS`, which the gate
    reads, and it fails closed — a count that was not taken fails the
    gate, because a gate whose only instrument is missing must not read
    as a pass.
  - `share` — the phase's percent of that run's own CPU,
    `time.process_time()` (never wall). Scale-free inside one run; it
    shows where the pass spends its work, and it is still measured,
    printed and recorded — but it is TELEMETRY: nothing compares it
    against the recorded share budgets and the ratchet never tightens
    them. A proportion of a timed run is not a count of work: its
    runner-to-runner spread measures 1.9-2.7 points against the 1.5-point
    gap it would be judged against (#500, #506), and it moves when the
    corpus MIX shifts between formats of different parse cost even with
    no code path slower and every count under its own ceiling (PR #512).
    The measurement doctrine's own principle decides it: counters gate,
    derived proportions do not. The share members STAY in
    `.github/ci-thresholds.json`, carrying the reading they recorded, and
    the loader and the direction guard keep validating and defending them
    like any recorded number; nothing fails on them.
  Both are costs: `floor = measured + 1.5`, and
  `scripts/ci/reparse_ratchet.py` mirrors `ratchet.py` with every
  comparison reversed, one gated phase at a time. The pass is
  decomposed by wrapping the callables the real path calls, and the
  residual — everything they do not account for — is a phase of its
  own, named and ratcheted, so an unmeasured phase cannot hide inside a
  measured one. `perf stat`'s machine-instruction count rides along as
  a CROSS-CHECK and nothing more: it prices a whole process rather
  than a phase, and needs a counter facility a hosted runner may
  refuse. When an instrument is unavailable the bench prints NOT
  MEASURED with the reason — an absent measurement must never read as
  a passing one. Never raise a floor by hand, for any reason. A
  parse-semantics change that legitimately spends the headroom takes
  the reparse family's own sanctioned re-seed — the delete under
  `[reparse-re-seed]`, then a runner-measured seed of the final tree,
  exactly the suite-cost path — and while the family is absent the
  reparse gate prints `no reparse budget — re-seed in flight` and exits
  0 (issue #698). Every run that wrote its measurement uploads it as the
  `reparse-measurement` workflow artifact — the runner-measurement
  source a reparse seed is fed through `reparse_ratchet.py --seed`, the
  way `suite-measurement` feeds the suite-cost seed.
- Module size (`module_size_baseline`): every tracked `*.py` under
  `backend/`, `scripts/`, `tests/`, every tracked `src/**/*.js(x)`, and
  the shipped SQL, CSS and workflow-YAML families (`backend/*.sql`,
  `public/*.css`, `.github/workflows/*.yml|yaml`) is capped per file
  (production 500 / test 700; everything but `tests/` is production)
  by `scripts/ci/size_baseline.py`. Entries are never added or
  raised by hand — an outgrown file moves code into a new module. CI
  tightens an entry as its file shrinks and drops it once back under
  the ceiling. A new file family may be seeded exactly once, at current
  line counts, through the loader's own writer; every later run follows
  the rule unchanged.
- Pylint suppressions (`pylint_suppression_baseline`): the per-file
  count of inline `pylint: disable`/`disable-next` comments naming a
  complexity check (`too-many-*`) over tracked `*.py` under `backend/`
  and `scripts/` (`tests/` stays outside), checked and tightened by
  `scripts/ci/suppression_baseline.py` under the same never-added,
  never-raised, only-shrinks rules as module size.
- Suite cost (`suite_cost`): per-phase instruction counts (millions of
  bytecode instructions, one decimal; phases `collection`, `run`,
  `residual`) of ONE pytest pass over the pinned fixture
  `scripts/ci/suite_bench_files.txt`, measured by
  `scripts/ci/suite_bench.py` with `sys.monitoring`'s INSTRUCTION
  events (the `process_time` fallback is telemetry that fails closed
  at `--check`). This is the suite-cost gate's instrument — runner wall is void, and a
  count is exact within one environment state (the bench records its
  across-state bound) — and the gap sits ABOVE the measured value because the number is a
  cost ceiling, not a quality floor. Seeded via the loader's writer
  (`suite_ratchet.py --seed`, only when the member is absent) FROM A
  RUNNER MEASUREMENT — the environment the gate and the master-push
  record path share; `residual` is environment-sensitive, so a
  non-runner seed would gate runners red; tightened by the master-push
  bot (`suite_ratchet.py --tighten`, past the 1.5 hysteresis, both
  fields, never raised). The interpreter micro is pinned in the
  workflows that run the bench. The budgets are BOUND TO THE WORKLOAD
  THEY MEASURED: the fixture's tree-scanning tests (the db-marker
  derivation, the version-literal guard, the scratch-DB scan) spend
  per-line over ``tests/``, so a change to that tree's content moves
  the counts with no fixture, interpreter or machine change. Every
  measurement records the workload identity `tests_tree_lines` (the
  tests/*.py line total) whenever its tree has one, and a seed writes
  it beside the budgets when its reading carries it; the gate names a
  drifted identity when a phase breaches instead of reading it as
  slowness, and the guard refuses a hand-moved identity on an
  established family — the sanctioned re-seed, measured on the tree
  the seed describes, is the only writer. Sanctioned RE-SEED: when the recorded
  workload legitimately changes — the fixture list, the interpreter
  pin, the code the pinned tests execute, or the scanned tree's
  content (issue #524: the day's 345.9→348.6 step was #518's +643 test
  lines moving the scans, not a runner change) — it lands as TWO reviewed
  gate-definers, never one. The first deletes the stale member under
  the commit-message marker `[suite-cost-re-seed]`
  (`scripts/ci/reseed.py`) — the only way `thresholds.load` accepts a
  family-absent document; the suite-cost gate then prints `no suite_cost
  budget — re-seed in flight` and exits 0, and the guard reads the
  removal as legal. Each cost family's re-seed runs under its OWN
  marker — suite_cost under `[suite-cost-re-seed]`, reparse under
  `[reparse-re-seed]` — the same bounded walk, fail closed, one probe
  per family: a marker buys that family's ABSENCE only, never a raised
  budget and never another family's absence (issue #698). The second
  seeds the new counts through the family's own writer —
  `suite_ratchet.py --seed` for suite_cost, `reparse_ratchet.py
  --seed` for reparse — FROM A RUNNER MEASUREMENT, cited by run id in
  the PR body, never hand-derived.
  Against a base that predates the family, those entries are a
  brand-new member's seed. One change cannot do both (the guard refuses
  an upward move the base carries): the delete is its own commit and
  its message carries the marker. The marker's scope is that delete —
  the seed commit does not carry it, and it is the commit that restores
  the family; a restoring document is valid either way. Every helper
  that re-validates a loader-produced document asks the same verdict
  the loader asked (`thresholds.verdict()`); `normalise` takes it as a
  required argument, so no helper can read strict by omission.

  **The measuring run measured the tree master will hold once the seed
  lands, the seed change's own edits included.** A budget binds to the
  tree it was measured on, so a measured tree master does not hold
  records numbers no later run reproduces — and the recorded identity
  catches the resulting drift only when it lies in `tests/` — while the
  guard admits no hand correction of either, so the repair is another
  delete+seed pair.
  Two properties of the run decide it, both read off the canary: the
  run RAN the tests leg (a docs-only or bot-data push skips that leg and
  carries no artifact), and the bench RAN (its step is gated on the
  suite and the JS coverage having succeeded, so a run whose suite
  failed carries no measurement either). Which run carries both follows
  from whether the window's changes have merged, not from choice:

  - They have MERGED: the `suite-measurement` artifact of the newest
    commit the window takes whose ci-gate run RAN the tests leg. A
    pricing-bot or docs-only push skips it, so the LAST commit is not
    always a usable source. The measured tree must equal master's tree
    at the seed, apart from `.github/ci-thresholds.json` and
    documentation-only paths; when a commit outside those landed after
    the newest measuring run, take a fresh measurement of the final
    tree by dispatching ci-gate (`gh workflow run ci-gate.yml --ref
    master`) — a `workflow_dispatch` run reads no changed paths, so it
    runs the full gate set, the tests leg and its `suite-measurement`
    artifact included.
  - They are still OPEN — the canary went red on a pull request, which
    includes the seed PR itself: that same artifact from the seed PR's
    OWN ci-gate run on the commit carrying its FINAL CODE AND TESTS,
    the last push before the data-only one. So the seed change lands
    its code and tests, waits for that run, seeds from its artifact,
    and regenerates the family in a later push touching nothing but
    `.github/ci-thresholds.json`. That later run is the CHECK, not the
    source: it must be green and name no workload-identity drift, and
    it is the first run to read the seeded identity back. Its own
    values cannot come from its own run — they must exist before the
    seed commit is written — so the source is always an earlier run.

  Both sources are measured BEFORE the merge: the artifact is retained
  for a day, and for a seed change touching ONLY
  `.github/ci-thresholds.json` a master push is path-ignored by
  ci-gate, so no master run exists afterwards to re-measure against.

  The measured tree MUST NOT CHANGE between the measuring run and the
  merge; the seed's own write of `.github/ci-thresholds.json`, and
  documentation-only paths, are the only permitted differences. `tests/`
  is the part of that tree the recorded identity detects
  (`tests_tree_lines`), but the budgets bind to every
  line the pinned tests execute — a pricing bot's `src/pricing.json` and
  `backend/constants.py` reach them through `tests/conftest.py` — so a
  code line landing after the measuring run invalidates it the same way.
  When the tree did change, the source is a fresh run of the FINAL tree;
  recorded numbers are never adjusted to fit.

  **The window is bounded.** `reseed._MAX_VISITS` caps the marker walk.
  On master each commit is one visit, and this repository rebase-merges,
  so master carries no merge commits and a pull request lands as one
  commit per commit it carries. A pull request's merge-ref run also
  visits GitHub's merge commit and every commit of the pull request,
  interleaved breadth-first with master's, so a multi-commit in-window
  pull request reaches the cap sooner: in-window pull requests land as
  one commit each, and the window closes while HEAD is fewer than ten
  visits from the delete. Past that the walk fails CLOSED: it reads as
  no marker, and the family-absent document is gated as any other is.

  **Closing a window is not re-opening one.** Two separate facts, and
  the next re-seed needs both: the walk reached a family-present commit
  — the window this marker opened is closed, so the delete's marker can
  never exempt a later family-absent document — and a NEW marked delete
  precedes the new seed. A seed already on master supplies the first;
  the second is a fresh pair, never the standing one re-used.
- The direction guard (`scripts/ci/thresholds_guard.py`, a step in
  `tests.yml`) makes the never-rules mechanical: on every PR and master
  push it compares the data against the base document and fails on a
  lowered coverage value, a RAISED reparse budget, a raised entry, or
  an added entry under the frozen core families (the Python and `src/`
  JavaScript scope the size ratchet had when the guard landed) or for
  an unmeasured path. What stays legal is exactly the bots' move set
  — a raise, a tighten, a brand-new member's entries — plus a new
  measured family's one-time seed and a cost family's REMOVAL, the
  latter admitted only on a commit carrying that family's own re-seed
  marker (`[suite-cost-re-seed]` for suite_cost, `[reparse-re-seed]`
  for reparse). A marker buys the absence, never a raised budget
  (the upward-move refusal stands). The truth of every seed is pinned
  by the committed-document-matches-tree tests, which run on the same
  merge ref.
- Coverage numbers carry exactly one decimal (`92.0`, never `92` or
  `92.00`) — what the ratchet writes, `coverage --precision=1`
  measures and the JS gate's `toFixed(1)` reads. The document is
  always the loader's canonical bytes; never hand-edit it.
- The bot's raise/tighten commit on master touches only the data file
  and must not re-trigger workflows: every gate workflow's push
  trigger (`version-guard.yml` included) ignores
  `.github/ci-thresholds.json`. A new gate workflow carries the
  exemption over.

## Tests never pin repository-managed data (SV-TEST-DATA)

A test NEVER depends on data the repository changes by design — the
rate rows in `src/pricing.json`, the committed `PARSER_VERSION` /
`PRICING_VERSION` / `MARKER_READER_VERSION`, or the host/model set the
refresh maintains (SV-RATE-REFRESH). Assert against synthetic data, or
values derived at run time (`str(int(constants.PRICING_VERSION) + 1)`).
A rounded expectation is exact only when computed by the same algorithm
over the same values in the same order as the code under test;
otherwise tolerate one unit in the rounded place (plus the code's
accumulated rounding).

One more legitimate shape: a literal rate vector pinned at a FIXED
PAST instant inside an already-closed dated window. Closed windows are
immutable (SV-RATE-DATA), so the pin is refresh- and
perturbation-invariant; it is the only way to test end-exclusivity and
promotion boundaries against known vectors. Stamp the reference
instant before every appended cutover (a fixed past `from`, strictly
before the seeded data); `tests/test_provider_pricing.py`'s SEEDED
comment is the exemplar.

Enforcement has two halves. The perturbed-data CI leg
(`scripts/ci/perturb_test_data.py`) runs the suite on a tree where each
rate row gains five appended entries per run — ×2, ×0.37, a per-row
irregular factor, five independent per-field factors, and a
single-field move, in seeded-shuffled row order — and the version
constants are bumped, so a hidden dependency fails as a test, not as a
broken refresh. The guard, `tests/test_no_pinned_version_literals.py`,
scans `tests/` for a literal assigned to any of the three constants
and for live-row shapes: a read of the committed document
(`pricing.PRICING_JSON`), a module-level bind of the `pricing.json`
path, and a `rate_for` / `resolve` / `compute_cost` call naming a live
model or host. An inline `# sv-test-data: allow` comment with a reason
excuses a site; a marker whose site is gone fails the guard as rot.
