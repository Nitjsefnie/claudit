---
paths:
  - "**/backend/ingest*.py"
  - "**/backend/schema*.sql"
  - "**/backend/schema_files.py"
  - "**/backend/schema_indexes.py"
  - "**/backend/db.py"
  - "**/backend/key_layout.py"
  - "**/backend/lane_*.py"
  - "**/backend/project_aliases.py"
  - "**/backend/api_dashboard.py"
  - "**/backend/api_common.py"
---

# claudit Doctrine — ingest and identity

## Schema is applied at startup, not by a human (SV-SCHEMA-AUTOAPPLY)

`db.apply_schema()` runs the idempotent static files in
`backend/schema_files.py::SCHEMA_PATHS`, followed by the fixed analytical
index policy in `backend/schema_indexes.py`, before `schema_check()`.
`schema_stamp` hashes that complete static-plus-policy SQL string; DDL and
stamp commit in ONE transaction under a Postgres advisory lock, with an
under-lock stamp recheck. The static reader stays independent of policy.
Resolve CONFIG lazily at startup, after dotenv, never through a db/schema
module's import-time snapshot.

A current effective stamp skips all DDL and migration locks. A changed
index-affecting collection choice changes the SQL/hash and reapplies the
whole script without manual stamp deletion. Display switches and unrelated
collection choices do not change this stamp. `records_turn_flags_idx`
exists iff `turn_context` is collected; `tool_uses_read_targets_idx` and
`tool_uses_reread_idx` exist iff `context_intake` is collected. Static SQL
never creates those indexes, so fresh OFF startup never builds them.
`records_request_idx` and `tool_uses_tool_idx` are unconditionally dropped:
no app query needs them. Keep identity, timestamp, canonical and latency
indexes intact.

Ordinary index CREATEs run in the same transaction; CONCURRENTLY cannot.
Initial or reenable builds may block startup and writes. Transaction-local
`lock_timeout` bounds waits, not build duration; production duration is
unmeasured. Any failure rolls back DDL and stamp before advisory unlock;
systemd retries, and the next successful boot converges atomically.
An out-of-band catalog mutation still needs `DELETE FROM schema_stamp`
to force reapplication: a matching stamp proves the script ran, not that
later catalog state is intact. The schema stamp is separate from code
parser versions and `files.collection_flags` collection metadata.

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
schema. Typed file timeline columns are not readable by a binary that
predates them. Additive nullable columns and nullable relaxations preserve old
named-column writers; relaxations retain types, defaults, primary keys and
identities. `files.collection_flags TEXT[]` is nullable; NULL and an empty
array both denote all collections ON. Nonconstraint index removal and
collection-conditioned analytical indexes are compatible changes to access
paths, not stored data. Guarded, idempotent widening exceptions are:

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
older binaries is not repaired). An older binary applying its own schema
may recreate removed indexes and write its own differing stamp; the next
forward boot reapplies its effective script and restores policy. Any other
migration that DROPS or retypes a column needs a different mechanism, not a
quiet exception.

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
`<bucket>/<object-key>`, the bucket one of `CLAUDIT_R2_BUCKET`'s names (several
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

The schema is per-file, not per-session; ordered schema files are applied
through `backend/schema_files.py`, starting with `backend/schema.sql`:

- `files(file_key PK, project_id, session_id, is_main, r2_etag,
  r2_size_bytes, r2_last_modified, parsed_at, parser_version,
  ctx_ts TIMESTAMPTZ[], ctx_line INTEGER[], ctx_input INTEGER[],
  ctx_output INTEGER[], prompt_ts TIMESTAMPTZ[], turn_count)` — one row per
  ingested JSONL; the four context arrays have equal cardinality, with
  turn index and input delta derived by readers.
- `records(file_key, line_num, uuid, request_id, ts, model,
  fresh_tokens, cache_creation_tokens, cache_read_tokens,
  output_tokens, eph5_tokens, eph1h_tokens, cost_usd, request_fee_usd,
  web_search_requests,
  text_chars, reply_latency_s, stop_reason, effort, thinking_tokens,
  cli_version, turn_flags, turn_tool_results, long_context,
  long_context_input_mult, long_context_output_mult, provider,
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

## The first boot of this build over an existing DB re-keys every file (SV-REKEY-CUTOVER)

The first boot of THIS BUILD over ANY database from an earlier build
re-keys stored identity from the bare object key to
`<bucket>/<object-key>`, whatever `CLAUDIT_R2_BUCKET` says, deleting and
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
