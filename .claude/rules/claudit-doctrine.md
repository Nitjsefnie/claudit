# claudit Doctrine

Local rules for the claudit repo. Global rules under `~/.claude/rules/**` still apply.

## Parser-spec ownership (SV-PARSER-SPEC)

The CANONICAL parser is `~/.claude/scripts/parse_session.py` (owned by
analyst). Both the in-browser `src/parser.js` AND the backend
`backend/parse.py` MIRROR that semantics. Keep all three in lockstep on:

- Per-file requestId `_merge_usage_max` during ingest (Phase 1,
  persisted into `records`)
- Cross-file UUID dedup, resolved at INGEST into `records.is_canonical`
  (see SV-CANONICAL-FLAG). The winner is still exactly what
  `DISTINCT ON (uuid) ORDER BY uuid, file_key` picked at read time.
  There is still no persisted Phase 2 rollup; the per-session SUM
  aggregation that used to live in `compute_cache` was dropped in R1.
- `<task-notification>` ref detection for sub-agent jsonls
- Sidecar `data/subagents/agent-*.jsonl` resolution
- `MODEL_RATES` table (single source of truth: `backend/pricing.py`,
  initially copied from parse_session.py:1148-1166)

When you find a discrepancy: the Python canonical is right by default.
If the canonical itself has a real bug, file it for analyst via mailbox;
don't quietly fork the semantics here.

## Cost accounting is split TTL, always (SV-COST-SPLIT)

Every place this repo computes cost from `usage` records MUST split
`cache_creation` into `ephemeral_5m` and `ephemeral_1h` and apply the
correct multiplier:

- 5m write: 1.25× base input rate
- 1h write: 2× base input rate
- Tokens with no `ephemeral_*` split (legacy SDK records) charged at the
  5m rate (conservative undercount, not overcount).

Single-rate `cache_create` cost is BANNED. If you bump `MODEL_RATES`,
also bump `PARSER_VERSION` in `backend/constants.py` so the next ingest reparses every
session.

## Backend is the ONLY load path (SV-NO-LOCAL-UPLOAD)

Supersedes SV-IN-BROWSER-FALLBACK, which required a drag-drop offline
fallback. That fallback was removed on 2026-07-21: there is now no way to
feed a transcript in from the browser — no drag-drop target, no
`FileReader`, no zip expansion, and no JSZip dependency. The only jsonls
the app reads come from R2, owned by the same operator.

There is still no upload endpoint and no server-side parsing of
operator-supplied jsonls. Do not reintroduce either, and do not add a
file picker as a "convenience" — an ingress path is exactly what was cut.

`src/parser.js` is NOT dead code and must stay. It still serves two live
consumers: `loadFromBackend()` parses the bytes from
`/api/sessions/{id}/transcript` client-side via `parseTranscript` +
`computeSessionStats`, and the Token Breakdown panel prices rows through
`window.rateForModel`. Its rate table remains bound to `backend/pricing.py`
by SV-PARSER-SPEC and the node parity test.

## Bundle distribution NOT applicable (SV-NO-BUNDLE)

claudit does not ship via `claude-setup.zip`. Distribution path is
git (this repo) + `pip install -r backend/requirements.txt`.

## Test fixtures stay small (SV-FIXTURE-SIZE)

`fixtures/parser/*.jsonl` are hand-crafted single-record samples,
each under 1 KB. `fixtures/r2_mini/` is the end-to-end mini mirror
(2 projects, 4 sessions, 1 sidecar, 1 cross-session shared uuid).
Don't grow either by accident — larger samples go under
`/tmp/analyst.BCYKic3p/r2/` (the local R2 mirror, not committed).

## Develop against the full corpus, never the local tree (SV-FULL-CORPUS)

Any measurement, probe, or panel prototype reads the R2 corpus (or a
full local mirror of it), NOT `~/.claude/projects/`. The live session
tree is pruned by Claude Code, so it is a small and BIASED sample:
recent sessions survive, long-finished ones are gone, and whichever
projects were touched lately are over-represented.

Measured 2026-09-07: 475 local jsonls against 11,204 objects in the
bucket — 4%. A behavioural rate derived from the local tree is a rate
over that 4%, and nothing in the number says so.

This is the same fact the README gives as the reason ingest goes
through object storage at all, applied one step earlier: it governs the
throwaway script you write to decide whether a panel is worth building,
not just the shipped ingest path. A prototype that samples the local
tree can report a clean zero for behaviour that is abundant in the
corpus, which reads as "no signal here" and kills the panel.

Use `backend/r2.py` (`list_keys` / `get_stream`) so a probe honours
`R2_ENDPOINT` and works against either the bucket or a `file://`
mirror. Stratify and say the sample size in the output; never present
a corpus-wide claim from an unstated subset.

## Read-only on canonical paths (SV-READ-ONLY-CANONICAL)

claudit NEVER edits `~/.claude/scripts/parse_session.py` or
`~/.claude/scripts/discord_mb.py`. Those are owned by analyst, and per
global doctrine they are NOT copied, symlinked, or hardlinked into this
repo — invoke them by absolute path under `~/.claude/scripts/`.

## Schema is applied at startup, not by a human (SV-SCHEMA-AUTOAPPLY)

`db.apply_schema()` runs `backend/schema.sql` on every boot, before
`schema_check()`, under a Postgres advisory lock so concurrent boots do
not race the same DDL. The file is idempotent by construction, which is
what makes this safe to repeat.

The manual `psql ... -f backend/schema.sql` step is no longer load-bearing:
a deploy that pulls code writing a new column converges the database
instead of aborting every ingest with `UndefinedColumn` while the
dashboard serves stale aggregates (issue #43, hit twice in one day across
the claudit and glmmeter deploys).

The cost is that ROLLBACK IS ONE-DIRECTIONAL — restarting an older binary
leaves it against a newer schema. That is acceptable ONLY while every
migration is additive and nullable, so an older binary ignores what it
does not know. A migration that DROPS or retypes a column breaks this
property and needs a different mechanism, not a quiet exception.

## Schema fail-fast (SV-SCHEMA-FAIL-FAST)

`backend/db.schema_check()` runs at every server startup. It verifies
(a) `claudit.files` exists and (b) the auth DB's `users.config` is a
JSONB column. Either failure aborts startup with a clear error rather
than silently degrading to a broken auth flow at first login.

## Per-file files+records contract (SV-FILES-RECORDS)

The schema is per-file, not per-session. Two tables hold the parse
output (see `backend/schema.sql`):

- `files(file_key PK, project_id, session_id, is_main, r2_etag,
  r2_size_bytes, r2_last_modified, parsed_at, parser_version,
  ctx_turns JSONB, turn_count)` — one row per ingested JSONL, with
  the context-growth trace inlined as `ctx_turns`.
- `records(file_key, line_num, uuid, request_id, ts, model,
  fresh_tokens, cache_creation_tokens, cache_read_tokens,
  output_tokens, eph5_tokens, eph1h_tokens, cost_usd, text_chars,
  reply_latency_s, stop_reason, effort, thinking_tokens, cli_version,
  turn_flags, turn_tool_results)`
  PK `(file_key, line_num)` — one row per usage-bearing line AFTER
  per-file Phase 1 max-merge for matching `request_id`.

Cross-file uuid dedup is resolved at INGEST into `records.is_canonical`
(SV-CANONICAL-FLAG below). There is still NO persisted `record_uuids`
or `session_requests` table — both were dropped in R1 along with the
per-session rollup, the materialized hourly view, and the `sessions`
table. Reintroducing a persisted rollup or any cross-session table
requires a new migration, not a quiet code change.

## Dedup is a flag, not a read-time sort (SV-CANONICAL-FLAG)

`records.is_canonical` marks the row that
`DISTINCT ON (r.uuid) ORDER BY r.uuid, r.file_key` used to select at
read time. `line_num` breaks ties within a `file_key`, which the old
read-time ORDER BY left arbitrary. Rows with a NULL `uuid` are legacy
records kept verbatim (they were the `UNION ALL` leg) and are always
canonical.

Read endpoints MUST filter `WHERE is_canonical` and MUST NOT
reintroduce `DISTINCT ON (uuid)`. It was moved because `records` is
immutable between hourly ingests, yet every read re-sorted the whole
table to drop ~3.5% duplicates — `/api/cache` prefixed that dedup as a
CTE onto four queries and paid it four times per request (19.1s at
range=all; 0.52s after).

`ingest.recompute_canonical()` runs after EVERY successful ingest, not
only when files changed: adding or removing a FILE can change which row
wins for a uuid, and a freshly-migrated DB has the column defaulted to
TRUE across the board. The UPDATE only touches rows whose flag actually
flips, so a steady-state pass writes nothing. The column defaults to
TRUE so a migrated-but-not-yet-recomputed DB over-counts (behaves like
no dedup) rather than silently dropping rows.

Changing the winner rule means changing BOTH `recompute_canonical()`
and `src/parser.js`/`parse_session.py` semantics in lockstep
(SV-PARSER-SPEC).

`tool_uses.is_canonical` is the same rule keyed on `tool_use_id` (the
block's globally unique id), set in the same pass. It exists because a
compaction sidecar (`agent-acompact-*`) replays the main file's
assistant lines, tool_use blocks included: records were deduped by uuid
while the 6k replayed calls were counted twice in every tool rollup.
Rows with a NULL `tool_use_id` are always canonical. Every rollup and
live read over `tool_uses` MUST filter `tu.is_canonical`.

## Foreign models are purged, not filtered (SV-SUPPRESSED-MODELS)

`suppressed_models(pattern, note, added_at)` lists models whose records
must not count. `ingest.purge_suppressed()` runs FIRST in
`_rebuild_derived_state()` — before the canonical pass — and DELETEs
matching `records` plus the `tool_uses` on the same lines.

Suppression is a delete, not a read-time predicate, because every read
path and every rollup already treats `records` as the truth; one deletion
keeps them consistent without ~15 extra filters a new endpoint could
forget. The `tool_uses` half is not optional: `tool_rollup` LEFT JOINs
`records` for the model, so a call whose record is gone reappears under a
NULL model.

Patterns are matched `model ILIKE pattern`, so `glm-%` covers a family
and a bare model id still matches exactly. The table ships EMPTY and must
stay that way in `schema.sql`: the same codebase is deployed over the
`zai` bucket as glmmeter, where `glm-%` would suppress everything.
Populate per deploy.

Why it exists: Claude Code writes every session under one tree whichever
endpoint served it, so resuming a session on the other lane interleaves
that provider's assistant entries into a transcript this bucket already
owns — real usage, but not ours, and priced against our table it invents
a cost. Removing a pattern brings the rows back only on a reparse (bump
`PARSER_VERSION`).

`files.models` lists every model that answered in the file, recorded at
parse time and so surviving the purge: it is the only trace that a
session switched lanes, and an analysis over `records` that must skip
mixed-lane sessions joins it against `suppressed_models`.

## A token type may be a SUBSET (SV-SUBSET-TOKENS)

`records.thinking_tokens` is part of `output_tokens`, not a sixth slice
of the billed partition: the API reports it under
`usage.output_tokens_details`, and `pricing` never sees it — the output
rate already covers those tokens.

It is declared in `api_dashboard.TOKEN_TYPE_FIELDS` anyway, because that
tuple drives zero-suppression and panel order, and a subset needs both.
What it must NEVER do is enter an arithmetic total: not `t.total` in
`src/app.jsx`, not the Token Breakdown rows (which partition the billed
tokens and price each one), not a cost computation. `usage_rollup`
carries the column so the panel reads a pre-aggregate like every other
series; it is summed, never added to the others.

codexmeter reached the same shape independently for Codex's
`reasoning_output_tokens` (`backend/parse_common.py`: "a SUBSET of
`output` … never added to the cost"), which is the same quantity under
the other provider's name — measured there at 47.7% of all output
tokens.

## Aggregates are precomputed at ingest (SV-ROLLUP)

`usage_rollup` holds pre-summed usage at grain
`(session_id, hour, model, is_main)`, rebuilt by
`ingest.rebuild_rollup()` after every successful ingest (AFTER
`recompute_canonical()` — it reads `is_canonical`). ~6.1k rows stand in
for ~286k records.

The grain is load-bearing, do not "simplify" it:

- **Keyed by HOUR, not by session.** A range filter sums only the
  in-range hours; a session-grained table would count a session
  straddling the boundary whole.
- **Carries MODEL.** A session's dominant model is
  `argmax(SUM(requests))` over the in-range rows — exactly what
  `MODE() WITHIN GROUP` computed from raw records, not an
  approximation.
- **Carries `first_ts`/`last_ts`.** Burn-rate span is
  `MAX(last_ts) - MIN(first_ts)`, which composes; a stored duration
  would not.

Only pure sums/counts/min/max may be served from it. **`PERCENTILE_CONT`
does not compose** — p50/p90 of a union of hours is not derivable from
per-hour p50/p90 — so `response_sizes` stays a live pass over `records`.
Do not "optimise" it onto the rollup.

The rollup is only valid for display buckets ≥ 1 hour. `/api/dashboard`
gates on `bucket_s >= 3600` and otherwise reads a live subquery shaped
with the same column names, so both paths share one set of queries. The
24h view buckets at 5 minutes and takes the live path.

It is derived state: anything that mutates `records` outside ingest must
rebuild or clear it, or reads serve a stale pre-aggregate. Totals are
verified equal to the equivalent `records` aggregate (requests, tokens,
cost, distinct sessions) — keep that true.

`ctx_cost_rollup` is the third of the composable kind, grain
`(hour, project_id, model, ctx_bucket)` holding `requests`,
`total_tokens` and `cost_usd`.
It exists because `usage_rollup` CANNOT serve a cost-by-context panel:
that grain sums fresh/create/read across a whole (session, hour, model),
which destroys the PER-CALL window size the x-axis is made of. Bucketing
by that window at ingest keeps it, and the stored columns are pure sums,
so they compose across hours, projects and models alike. `total_tokens`
is what the tokens variant of the panel charts: the same bars measured
before the rate, which is the only measure a free lane has.

A cost panel is HIDDEN when the range cost nothing, never drawn as a row
of zeros — Cost by Model, Cost by Agent Type, Cost by Context Size, the
cost half of Token Breakdown, the total-cost card and the heatmap's cost
metric all gate on there being cost in view. Their tokens counterparts
do not: a free lane (bonsai-2-27b, priced at zero) still processes
tokens, and that is what it has to show.

Its bucket edges (`constants.CTX_BUCKET_WIDTH` / `CTX_BUCKET_MAX`) are
baked into stored rows exactly like `LATENCY_BUCKETS`: a read cannot
re-bucket to a different width, so changing either constant requires a
rollup rebuild. It does NOT require a `PARSER_VERSION` bump — the
context size is derived from stored `records` columns
(`fresh + cache_creation + cache_read`), not from a reparse.

`records` cascades from `files`; `files` cascades from `projects`.
Reparse is idempotent: deleting a file's `records` rows and
re-inserting on the next ingest leaves the table byte-identical.

## Failure causes and dispatches are stored, not re-derived (SV-WHY-COLUMNS)

`tool_uses.is_error` says THAT a call failed. Four columns say why, and
what a dispatch asked for:

- `error_kind` — `rejected` | `tool_error` | `failed`, NULL unless the
  call errored. HARNESS-GENERIC by rule: a PreToolUse hook denial carries
  the DEPLOY's wording, so it lands in `failed`. Do NOT add a kind for a
  particular operator's hooks — that couples a general tool to one setup.
  `tool_error` means the call RAN: a `<tool_use_error>` wrapper, or a
  Bash result opening `Exit code N` (the harness's own wording for a
  nonzero exit — 58% of all errored results in a recent sample, and in
  `failed` indistinguishable from a denial). `rejected`/`failed` mean it
  never ran, so those rows carry no `write_targets` and no churn.
- `error_text` — leading `parse.ERROR_TEXT_MAX` chars of the failed
  result. Grouping on it is how hook denials get separated from real
  failures, which is why the parser needs no hook vocabulary.
- `agent_type` / `agent_model` — read off an `Agent`/`Task` call's
  arguments. `files.agent_type` records what RAN and exists only when the
  subagent wrote a JSONL; these record what was ASKED for, so a dispatch
  that produced no file is still attributable.

`tool_error_rollup` and `dispatch_rollup` carry the composable halves
(pure counts). `error_text` is deliberately NOT in either grain —
unbounded cardinality does not belong in a rollup.

## Bash line churn is an estimate from command text (SV-BASH-CHURN)

Keep available payload counts for heredocs, patches, Python edits and literal
printf/echo/sed output. Python and sed replacements count the declared old/new
payload once, without match multiplication; a successful exit does not prove
that a line changed. For a recognized Bash file write with unknown addition
size, use one added line per CALL only if no additions were already counted.
Known-empty and deletion-only operations add zero. Do not infer old contents
for copies/overwrites, invent paths for unresolved targets, execute recorded
commands or infer writes from opaque script names or arbitrary object methods.
Read-only calls, null sinks and rejected/unlaunched/failed writes do not gain
fallback churn. Preserve the existing heredoc-write-before-later-error rule.

## Context intake is stored per call (SV-CONTEXT-INTAKE)

Five columns on `tool_uses` record what a call put INTO the context
window and whether it had already been put there:

- `result_chars` — size of the tool_result, IMAGES INCLUDED. An image
  block's base64 payload is the largest thing a result can carry and is
  92% of all duplicated read bytes over the live corpus, so a text-only
  measure reports the cheapest half and calls it a total. Recorded for
  errored calls too: a failure occupies the window like a success does.
- `read_targets` / `write_targets` — TEXT[] because one call can name
  several files (`cat a b`, `diff a b`).
- `read_kind` — `whole` | `slice`. LOAD-BEARING, not decorative:
  `grep -n x big.py` and `cat big.py` both name one file and only the
  second put it in context. Score them alike and grep-then-narrow ranks
  as MORE wasteful than one indiscriminate `cat`, which is backwards.
- `is_reread` — resolved at parse time by `parse._resolve_rereads`,
  NULL for anything that is not a settled whole-file read.

Targets come from tool arguments for `Read`/`Edit`/`Write` and from
COMMAND TEXT for `Bash` (`backend/bash_reads.py`). The Bash half is not
an extra: measured over the corpus, Bash is ~79% of the read surface
under bypass permissions, so an argument-only reader sees almost none
of the intake. Write targets from Bash text cover `>`/`>>`/`tee`,
`sed -i` (a WRITE, never a slice read — its script operand is never a
file either), and the paths a `python3 -`/`-c` body opens for writing
(`bash_churn.python_write_paths`). `$VAR` is expanded only when the same
command assigns it (`S=/tmp/s && cat > $S/f`); any `$` that survives
means a runtime path, and the token yields nothing. Heredoc bodies are
stripped before the scan — a docstring naming `INDEX.md` read nothing.

`is_reread` is deliberately CONSERVATIVE — a floor, not an estimate. It
excludes slices (different halves of a file are not redundant), reads
after a write to the same path (the bytes changed), errored reads (they
returned a failure, not the file, so they neither waste nor count as
having seen it), and partial overlap (`cat a b` after only `a` still
brought `b` in). Easier to argue up from a floor than to defend a
number that counted useful reads.

Scope is ONE jsonl, because that is where a session's context restarts.

These are psql-only, exactly like `error_text`: no endpoint, no panel,
no rollup. `read_targets` is unbounded cardinality, the same reason
`error_text` stays out of `tool_error_rollup`. **Do not add a panel for
this** — measured over 11,205 objects, duplicate NON-IMAGE whole reads
are 0.47% of all result bytes and 90% of the raw total sits in a
handful of image-heavy sessions, so a chart would swing with the range
picker and show an outlier as a trend. The data is for querying:

    SELECT sum(result_chars) FROM tool_uses WHERE is_reread;

## The parser version is code, never the environment (SV-PARSER-VERSION)

`constants.PARSER_VERSION` is the ONLY switch that forces a reparse, and
it lives in code so the bump travels in the same commit as the change
that needs it. It used to be `os.environ.get("PARSER_VERSION", "1")`,
which let a parser change ship while every stored row stayed on the old
semantics with nothing to detect the drift. Do not reintroduce an env
override.

## Rates are a function of (model, timestamp) (SV-DATED-RATES)

`pricing.rate_for(model, ts)` — a model may carry dated overrides in
`DATED_RATES`, a list of `(end_exclusive_utc, rates)` windows per exact
model key. Cost must be computed against the timestamp of the request
being priced, never the time of rendering. `parse.py` passes each
record's own `ts`; omitting `ts` yields LIST price (conservative — never
silently applies a discount).

A window is NEVER dropped once it has expired. Every `PARSER_VERSION`
bump reparses the whole bucket, and a record from inside the window must
come out at the price in force then; removing the window reprices that
history at list on the next reparse, silently. The machinery is also
tested through the `synthetic_dated_rate` fixture in `tests/conftest.py`
rather than only the live entries, so the path cannot rot whenever the
table happens to be empty.

Any read path that RE-DERIVES rates from summed tokens must group by
`pricing.RATE_EPOCHS` (`api.rate_epoch_sql` / `api.fold_per_model`), or
its per-component breakdown drifts from the `SUM(cost_usd)` total it
claims to decompose. Totals themselves always come from the stored
per-record `cost_usd` — do not recompute them at read time.

## Model resolution flags estimates (SV-RATE-ESTIMATES)

`pricing.resolve()` returns `kind`: `exact` | `tier` | `default`.

- Model ids are normalised first: provider/region prefixes stripped
  (`us.anthropic.`, `anthropic/`) and `.` → `-`, so `claude-opus-4.8`
  resolves the same as `claude-opus-4-8`.
- An EXACT match allows only a dated-snapshot (`-20250514`) or bracket
  (`[1m]`) suffix after a table key. A short version suffix must NOT
  match a shorter key — billing `claude-opus-4-9` at `claude-opus-4`'s
  retired 15/75 is a silent 3x overcount.
- Unmatched Claude models fall back to their family's current-generation
  LIST rates and are flagged `tier`; anything else is `default`.
  Non-exact resolutions surface as `estimated_rate` in the API so a
  guessed figure is never presented as fact.

Never invent a rate for a variant we have no published price for
(e.g. `-fast`): let it fall back and be flagged.
