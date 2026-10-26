# claudit

**Claude Code Usage Dashboard** — a self-hosted web app for visualising AI
coding-session usage transcripts.

A FastAPI backend ingests transcripts from Cloudflare R2 (or a local `file://`
mirror), parses them into Postgres, and serves dashboards and raw transcripts
to a React + in-browser-Babel frontend (no build step). One deploy can ingest
several buckets and several transcript formats at once: Claude Code session
JSONLs, Codex rollouts, and the two Kimi CLI wire formats (kimi-code and
legacy).

## Scope

claudit **exposes statistics** about Claude Code usage: how much you spent,
how many tokens went where, cache-tier breakdowns, reply latency, tool-error
rate, and when you were active. It answers *how much*, *how many*, and *when*.

It is deliberately **not** a session-analysis, coaching, or auditing tool. It
does not review your transcripts for "what went wrong", score session quality,
or synthesize an AI narrative about your work — Claude Code's own `/insights`
already does that. claudit puts the numbers in front of you and stays out of
the way; interpreting them is your job, not the dashboard's. PRs that add
qualitative analysis, an "auditor" agent, or a chatbot are out of scope.

## Why ingest from object storage, not the local session directory?

Claude Code prunes old session transcripts from the local tree over time. If
claudit read those files directly, a reparse after a prune would silently
**drop** history — your long-term stats would shrink. Ingesting from durable
storage (Cloudflare R2, or any `file://` mirror you keep) preserves the full
timeline, so the numbers only ever grow. This is the reason the ingest path
goes through a bucket rather than reading the live session directory.

## Panels

The dashboard panel set — Session Burn Rate, Cost by Model, Token Breakdown,
Prompt-Cache TTL Split, Per-Session Context Growth, Response Sizes, Tool Usage,
Reply Latency, Tool Error Rate — is based on
[`nhz-io/ccusage-plot`](https://github.com/nhz-io/ccusage-plot). This repo
ports those matplotlib-only offline visualisations into a hosted SVG/React app
with multi-user auth, R2 ingest, and live updates.

Panels with no upstream counterpart — Activity Heatmap, Lines Added/Deleted,
Cost/Tokens by Context Size and Cost/Tokens by Agent Type — originate here.

## Features

- **Cost-by-model** breakdown with the canonical 5-minute / 1-hour
  cache-create TTL split (`ephemeral_5m` × 1.25× base, `ephemeral_1h` × 2× base),
  beside a **Tokens by Model** bar measuring the same sessions before
  the rate. Every cost surface — this one, Cost by Agent Type, Cost by
  Context Size, the cost half of Token Breakdown, the total-cost card
  and the heatmap's cost metric — is HIDDEN when nothing in the range
  cost anything, rather than drawn as a row of zeros. Their tokens
  counterparts stay: a locally-served lane priced at zero still
  processes tokens.
- **Token Breakdown** as paired sort-by-tokens / sort-by-cost bars
  over Input, Output, Cache Create (5m / 1h / unsplit), Cache Read.
- **Thinking Output** — extended-thinking tokens over time, on its own
  panel and hidden when the range has none. It is a SUBSET of Output
  Tokens (the API reports it under `usage.output_tokens_details`), so it
  is plotted but never summed into a total or priced separately — the
  output rate already covers it.
- **Prompt-Cache TTL Split** showing adaptively-bucketed `ephemeral_5m`
  vs `ephemeral_1h` cache_create volumes with a 5m-share-% trend strip.
- **Response Sizes by Model** — adaptively-bucketed median + p90 of
  *visible response characters* (text content blocks; thinking excluded)
  on a log y-axis, per-model checkboxes.
- **Per-Session Context Growth** — per-model sub-panels with a
  p25–p75 IQR ribbon under a median line plus faint per-session
  traces, a multi-model checkbox-driven comparison row, and
  per-FILE traces so sub-agent invocations surface under their own
  model even when no main session JSONL exists. Each trace is
  anchored at an implicit (turn 0, ctx 0) origin.
- **Session burn rate** scatter with dot **area** scaling by
  end-of-session context size, model-coloured, plus EMA lines for
  output/input/cache-create/cache-read tokens-per-hour.
- **Tool Usage Ratio over Time** — adaptively-bucketed
  stacked-area-to-100% per tool (bucket size = largest in [60s, 1d]
  yielding ≥100 bins across the range) with top-N-at-any-bucket band
  promotion (so emerging tools don't get hidden in `Other`),
  per-panel model select, a per-bucket `Other` breakdown on hover,
  and `server_tool_use` blocks (e.g. WebSearch) counted alongside
  client tool calls.
- **Tool Error Rate over Time** — per-model EMA progression with
  per-tool toggleable lines (top-3 default ON), plus an Aggregate line.
- **Reply Latency over Time** — per-(bucket, model) p10–p90 ribbon
  with a median line and top/bottom 1% outlier dots (only when the
  bucket has ≥100 replies); log y-axis from 0.1s to max p90. Latency
  is the gap from each anchored user message to its assistant reply,
  computed at parse time. Non-empty user text, including harness-injected
  instrumentation/bash-IO, anchors a window without changing prompt counts
  or context turns; interrupt markers clear it. A reply after a failed
  attempt stores no latency unless a fresh user message re-anchors it.
- **Activity Heatmap** — weekday × hour grid of request activity in
  Czech local time (`Europe/Prague`, DST-aware via Postgres
  `AT TIME ZONE`), with requests / output-tokens / cost metric toggle
  and a per-panel model filter, plus Σ margin totals per weekday and per hour.
- **Cost by Context Size** — dollars spent at each 50k-wide
  context bucket, with a cumulative-share line answering "what
  fraction of my spend happens above N tokens of context". Context
  per call is fresh + cache-create + cache-read; the top bucket is
  open-ended so the `[1m]` variants are not dropped. Precomputed at
  ingest into `ctx_cost_rollup`, which `usage_rollup` cannot serve —
  its grain sums tokens across an hour and destroys the per-call
  window size the x-axis is made of. **Tokens by Context Size** is
  the same panel measured before the rate, and is the one a free
  lane still has; the cost half is hidden when nothing in the range
  cost anything.
- **Lines Added / Deleted** — per-call line churn read off the
  tool arguments. Edit and Write are the obvious sources; under
  bypass permissions most editing goes through Bash instead, so
  heredoc bodies written to a file, inline `git apply`/`patch`
  hunks, and python read/replace/write one-liners are counted
  too, alongside literal printf/echo output and supported sed edits.
  These are estimates: an Edit or python replacement counts one
  occurrence, its old and new text diffed line by line the way git
  would show the change (context repeated on both sides is not churn),
  without looking up match counts, and success does not prove that
  lines changed. A recognized
  Bash file write with unknown addition size receives one added line per call
  when no additions were already counted. Known empty writes and whole-line deletions,
  read-only calls and null sinks stay at zero additions; unknown overwrite
  deletions remain zero. A heredoc inside a `for` loop over a literal
  word list counts once per iteration; any loop whose count needs the
  command to run counts once. Commands are never executed to obtain counts.
- **Cross-file uuid dedup** resolved at ingest into
  `records.is_canonical`, so sub-agent JSONLs roll into their parent
  session without double-counting; read endpoints filter that flag.
- **Rate-limit hit** detection (Claude Code's `out of extra usage`
  marker on `type:"assistant"` records).
- **Codex and Kimi transcripts parse into the same tables.** Codex
  rollouts are differenced out of their cumulative token counters, dedup
  by request identity across resumed/forked rollouts, and billed on the
  long-context meter when a request's prompt exceeds the model's own
  threshold — a per-model datum (issue #765), defaulting to the Codex
  meter's 272k (2× input side, 1.5× output — persisted per record so
  every cost breakdown reconciles). Kimi's kimi-code and legacy wire
  formats land in the same tables with their own rate rows. The
  browser's Inspector parses every format too (`src/parser-lanes.js`).
- **Configurable branding** — `CLAUDIT_APP_NAME` / `CLAUDIT_APP_TITLE` /
  `CLAUDIT_APP_DESCRIPTION` set the browser title, meta description, logo,
  sign-in page and export-PNG filename, so this codebase can serve as
  codexmeter or kimimeter by config alone.
- **Time-range picker** (24h / 7d / 30d / 90d / 1y / all).
- **Live updates**: server-sent `ingest_done` events trigger a
  data refetch — no page reload.
- **Auth**: user-id + password against an external auth DB's
  `users.config` PBKDF2 hashes, OR a guest mode (read-only, no project
  filtering, no per-session detail).

## Architecture

```
R2 (one or more buckets: claude, codex, kimi, …)
  ↓  hourly ingest  (APScheduler @ :15 UTC, or POST /admin/ingest)
Postgres `claudit`
  • projects     (project_id PK)
  • files        (file_key PK = <bucket>/<object-key>, ctx_ts TIMESTAMPTZ[],
                  ctx_line/ctx_input/ctx_output INTEGER[], prompt_ts TIMESTAMPTZ[],
                  rate_limit_hits JSONB)
  • records      (file_key, line_num PK, per-request tokens + cost
                  + text_chars for visible-response size
                  + reply_latency_s for the user→assistant gap
                  + generation_s / output_tokens_per_s (stored only)
                  + stop_reason / effort / thinking_tokens
                  + long_context for the Codex meter
                  + cli_version / turn_flags / turn_tool_results)
  • tool_uses    (file_key, line_num, idx PK, ts, tool_name, model,
                  tool_use_id, is_canonical, is_error, result_chars,
                  read_targets, read_kind, is_reread)
  • ingest_runs  (audit log)
  ↓  on-demand
FastAPI  →  /api/dashboard, /api/cache,
            /api/sessions*, /api/context-growth/*,
            /api/me, /api/projects, /api/models,
            /api/tool-usage, /api/tool-error-rate,
            /api/activity-heatmap, /api/cost-by-context,
            /api/cost-by-agent, /api/reply-latency,
            /api/events SSE, /api/export
  ↓
React + in-browser Babel  →  /  (served by FastAPI)
```

An operator can fold worktree/scratch project ids into their repository's
project for the project picker with data, not code: insert a row into
`project_aliases`, and the next ingest re-keys stored files and rebuilds
the rollups under the target id.

```sql
INSERT INTO project_aliases(pattern, project_id, note)
VALUES ('-tmp-daedalus-%', '-root-daedalus-public', 'worktrees');
```

Deleting a row stops folding new files; already-folded rows stay at the target
only until a reparse restores their raw object-key id or the next marker-backed
lane ingest moves them to the marker slug.

`backend/parse.py` implements the parse spec (SV-PARSER-SPEC in
`.claude/rules/claudit-doctrine-parser.md`, pinned by `fixtures/parser/`),
including Phase 1 within-file `requestId` max-merge, sniffs each blob's
format (`backend/parse_lanes.py`) and dispatches Codex/Kimi blobs to the
lane parsers; cross-file uuid dedup (Phase 2) is resolved at ingest into
`records.is_canonical` (`ingest.recompute_canonical`), and the read
endpoints filter that flag. Costs are pre-computed at ingest using
the rates in `src/pricing.json` (single source of truth — bump
`PRICING_VERSION` in `backend/constants.py` when a rate change reprices
stored records; the reprice recomputes each record's `cost_usd` from its
own stored columns, with no full reparse).

## Configuration

All app configuration names start with `CLAUDIT_`. Names read by v0.4.0
remain deprecated fallbacks for one release after this rename (listed in
`backend/env_config.py`). The prefixed name wins when both are set, even if
empty. Startup warns once for each active old name, without logging values;
old names shadowed by a prefixed name are not reported. Rename your `.env`
keys to remove the warnings. `GUEST_LOGIN` and the blob-cache settings were
unreleased and have no fallback: use `CLAUDIT_GUEST_LOGIN`,
`CLAUDIT_R2_BLOB_CACHE` and `CLAUDIT_R2_BLOB_CACHE_MAX_BYTES`.

- `CLAUDIT_R2_BUCKET` names one or more buckets joined by `+` (e.g. `CLAUDIT_R2_BUCKET=claude`
  or `CLAUDIT_R2_BUCKET=codex+kimi`). Every stored file key is qualified with its
  bucket as `<bucket>/<object-key>`; a bucket not named here is not served
  or listed, and an invalid name aborts startup. Configuring several buckets
  is how one deploy serves Codex and Kimi transcripts beside Claude Code
  ones (a codexmeter / kimimeter deploy).
- `CLAUDIT_APP_NAME`, `CLAUDIT_APP_TITLE`, `CLAUDIT_APP_DESCRIPTION` brand every user-visible
  surface — browser title, meta description, logo text, sign-in page,
  export-PNG filename. Unset, they reproduce the claudit strings exactly;
  a codexmeter deploy sets `CLAUDIT_APP_NAME=codexmeter` and nothing else changes.
- `CLAUDIT_APP_PRIVACY_NOTICE_URL` optionally renders a "Privacy notice" link on
  the sign-in page; unset, the page is unchanged. An operator's notice
  can disclose the third-party hosts the app contacts: the sign-in page
  itself loads nothing off-site, while the dashboard behind it fetches
  fonts.googleapis.com and fonts.gstatic.com.
- `CLAUDIT_EXPORT_PYTHON` names the interpreter the export-PNG subprocess runs
  under (default `/usr/bin/python3`). The plot script imports matplotlib
  and psycopg, and matplotlib ships in `requirements-dev.txt` — not
  `backend/requirements.txt` — so a stock quickstart venv cannot render
  exports. Point it at a Python with both installed; if the interpreter
  is missing one, `/api/export` answers `503` naming `CLAUDIT_EXPORT_PYTHON`
  instead of an opaque 500.
- The rest of the environment (`CLAUDIT_DATABASE_URL_VIZ`, `CLAUDIT_R2_ENDPOINT`, auth and
  admin keys) is documented inline in [`backend/.env.example`](backend/.env.example).

### Startup collection and feature switches

Collection and feature switches default ON when absent. Set a switch to
`0` to disable it. They are read once at process startup, so an environment
change requires a service restart. A collection change takes effect on the
next successful ingest; it sets the registered parsed fields to SQL `NULL`
and clears and stops rebuilding any registered optional rollups. A feature
change affects the corresponding display or API surface after restart. The
two sets are independent: hiding a feature does not stop collecting its data,
and collection switches only disable a feature when that feature declares an
exclusive collection dependency. No current feature depends exclusively on the
existing collection groups. Generation speed is stored only, with no display
feature or rollup.

The explicit-value grammar is shared with `CLAUDIT_TIMING`: values are
lowercased without trimming. `""`, `"0"`, `"false"`, and `"no"` are false;
every other value is true, including `"off"`, spaces, and values with leading
or trailing whitespace. A bare process with an explicitly empty switch gets
false. Compose uses `${VAR:-default}`, so an empty Compose value resolves to
the default instead; use `0` to disable portably. The complete variable list
is in [`backend/.env.example`](backend/.env.example).

`CLAUDIT_HIDE_RATE_LIMITS` remains the inverse compatibility alias and defaults
OFF. Rate-limit display is enabled only when `CLAUDIT_FEATURE_RATE_LIMITS` is
enabled and the legacy alias is false; a truthy legacy alias wins even when
`CLAUDIT_FEATURE_RATE_LIMITS=1`. This switch affects display only. Rate-limit
events continue to be collected and stored.

#### Collection switches

| Environment variable | Stored data group |
|---|---|
| `CLAUDIT_COLLECT_TURN_CONTEXT` | Turn flags, turn tool-result summaries, and CLI version |
| `CLAUDIT_COLLECT_REQUEST_METADATA` | Reply stop reason and effort |
| `CLAUDIT_COLLECT_CACHE_WRITE_TOKENS` | Cache-write token count |
| `CLAUDIT_COLLECT_CONTEXT_INTAKE` | Tool-result size, read/write targets and read-kind metadata |
| `CLAUDIT_COLLECT_ERROR_TEXT` | Failed tool-result text |
| `CLAUDIT_COLLECT_FILE_EXTRAS` | File model list and prompt/turn counts |
| `CLAUDIT_COLLECT_TOOL_ERROR_KINDS` | Tool error classification and its optional rollup |
| `CLAUDIT_COLLECT_DISPATCH_ANALYTICS` | Dispatch model/prompt metadata and its optional rollups |
| `CLAUDIT_COLLECT_GENERATION_SPEED` | Generation duration and output tokens per second for all formats; backend parser only, stored only |

The exact stored columns and rollup tables for each group are listed in the
[collection switch inventory](PRIVACY.md#collection-switch-inventory). Raw
transcript objects and their disk-cache copies are unaffected.

#### Feature switches

| Environment variable | Display or API group |
|---|---|
| `CLAUDIT_FEATURE_USAGE_TIMESERIES` | Input, Output, Cache Create, Cache Read, Total Tokens, and Cost time series |
| `CLAUDIT_FEATURE_THINKING_OUTPUT` | Thinking Output panel |
| `CLAUDIT_FEATURE_LINE_CHURN` | Lines Added/Deleted panel and summary cell |
| `CLAUDIT_FEATURE_MODEL_COMPARISON` | Cost/Tokens by Model |
| `CLAUDIT_FEATURE_TOKEN_BREAKDOWN` | Token Breakdown card and embedded bars |
| `CLAUDIT_FEATURE_PROJECT_COMPARISON` | Cost/Tokens by Project |
| `CLAUDIT_FEATURE_CACHE_TTL` | Prompt-Cache TTL Split card and share strip |
| `CLAUDIT_FEATURE_RESPONSE_SIZES` | Response Sizes card |
| `CLAUDIT_FEATURE_CONTEXT_GROWTH` | Per-Session Context Growth and comparison plots |
| `CLAUDIT_FEATURE_BURN_RATE` | Session Burn Rate card |
| `CLAUDIT_FEATURE_CONTEXT_COMPARISON` | Cost/Tokens by Context Size |
| `CLAUDIT_FEATURE_TOOL_USAGE` | Tool Usage Ratio card |
| `CLAUDIT_FEATURE_REPLY_LATENCY` | Reply Latency card |
| `CLAUDIT_FEATURE_PAGE_PERFORMANCE` | Operator Page Performance card and readout |
| `CLAUDIT_FEATURE_TOOL_ERROR_RATE` | Tool Error Rate card |
| `CLAUDIT_FEATURE_AGENT_COMPARISON` | Cost/Tokens by Agent Type |
| `CLAUDIT_FEATURE_ACTIVITY_HEATMAP` | Activity Heatmap, margin totals, and legend |
| `CLAUDIT_FEATURE_SUMMARY` | Overview summary strip and PNG header statistics |
| `CLAUDIT_FEATURE_RATE_LIMITS` | Rate-limit counts, marks, legend, tooltip, and accessible description |
| `CLAUDIT_FEATURE_SESSIONS` | Sessions navigation and list |
| `CLAUDIT_FEATURE_CACHE` | Cache navigation, CacheView, and ContextGrowthAgg companion |
| `CLAUDIT_FEATURE_INSPECTOR` | Inspector navigation, transcript/sidecar reads, and session details |
| `CLAUDIT_FEATURE_PROJECT_PICKER` | Project picker, project-list endpoint, and project-filter affordance |
| `CLAUDIT_FEATURE_EXPORT_PNG` | PNG button and export endpoint |
| `CLAUDIT_FEATURE_LIVE_UPDATES` | EventSource, live-update indicator, and SSE endpoint |

### Client-side internet requirement

The frontend loads React, ReactDOM and Babel standalone for in-browser JSX
transpilation from this server's `/vendor/` paths. The pinned builds ship in
`public/vendor/` with comments and source-map references removed; their
upstream licence notices are in `public/vendor/LICENSES.txt`. The SRI hashes
in `public/index.html` pin the served bytes. Script dependencies require no
client-side internet access. Google Fonts (fonts.googleapis.com /
fonts.gstatic.com) is cosmetic only; if unreachable, the UI falls back
to the system font stacks already declared in `public/app.css`.

### First boot of this build over an existing database

The first ingest this build runs over a database created by an earlier
build **re-keys every stored file**: this build's `r2.list_keys` yields
bucket-qualified keys whatever `CLAUDIT_R2_BUCKET` says, so stored identity
moves from the bare object key to `<bucket>/<object-key>` and each old
row is deleted and re-inserted under its new key in the same run.
Adding a bucket to `CLAUDIT_R2_BUCKET` on an already-migrated deploy re-keys
nothing. That one-time run has known, self-healing behaviour:

- (a) until the run finishes, the new rows (which default
  `is_canonical=TRUE`) and the not-yet-swept old rows are both
  canonical, so live reads double-count for the duration; kimi-code and
  legacy tool ids are file-key-scoped and cannot dedup at all in that
  window;
- (b) a fatal mid-run leaves that double-count in place until the next
  clean run rebuilds derived state;
- (c) a transcript or sidecar request for a still-unswept old row
  returns an error, because its `-root-x`-style key names a bucket that
  is no longer configured;
- (d) a per-object fetch failure during the run drops that file's rows
  until the next hourly run (on the previous build, a failed fetch kept
  the old row).

Run the cutover off-peak and watch `/health` until the run finishes;
every window closes on its own once one clean run completes.

## Quick start

```bash
createdb claudit
psql claudit -f backend/schema.sql
createdb claudit_auth
psql claudit_auth -c "CREATE TABLE users (user_id BIGINT PRIMARY KEY, \
config JSONB NOT NULL DEFAULT '{}'::jsonb)"
cp backend/.env.example .env  # edit: CLAUDIT_DATABASE_URL_VIZ, CLAUDIT_DATABASE_URL_AUTH, CLAUDIT_R2_*, CLAUDIT_ADMIN_TOKEN
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

The second database is the **auth database**, holding the `users` table
the login page reads. claudit owns no schema for it — in production it is
provisioned and populated by your own user-management process, and the
application only ever READS it. Set `CLAUDIT_DATABASE_URL_AUTH` to point at the
database just created (`CLAUDIT_DATABASE_URL_AUTH=postgresql:///claudit_auth`);
the shipped `backend/.env.example` defaults to
`postgresql:///users`, a database this quick start does not create.

`db.schema_check()` aborts startup unless that table exists and carries
the two columns the login lookup reads, and it is deliberately strict
about both:

- `user_id` must be an integer type and `config` must be `JSONB`. Those
  two are the whole of the login lookup's SQL
  (`SELECT config FROM users WHERE user_id = %s`), and the same minimal
  shape `scripts/ci/smoke.py` builds for its own fixture. A wrong type
  aborts with `auth DB users.user_id must be an integer type (smallint,
  integer or bigint), got 'text'`.
- A table keyed by `id` rather than `user_id` used to pass the check and
  500 every login with `UndefinedColumn` — issue #368 tightened it to the
  two columns above.
- The connecting role needs `SELECT` on the table. It is the database,
  not the application, that is the real boundary: claudit issues no write
  against the auth DB, but a `GRANT`-less role cannot read it either, and
  startup then aborts with `auth DB: role '<role>' lacks SELECT on
  'users'`. So when `CLAUDIT_DATABASE_URL_AUTH` connects as a role other than the
  table's owner, run `GRANT SELECT ON users TO <role>` too.

A `users` table outside the `public` schema aborts with the same
`no 'users' table visible` message a wrong database produces, so check
the schema as well as the DSN. A database that cannot be reached at all
never gets that far — the connection times out instead, with no mention
of which variable was wrong (issue #463).

The table starts empty, so there is no credentialed account yet —
**Continue as guest** on the sign-in page gets you in without one, and
[Auth](#auth) has the credential shape a real sign-in needs. Guest login
is enabled by default; `CLAUDIT_GUEST_LOGIN=0` turns it off.

The first request blocks while ingest runs (~30s on a warm DB,
several minutes for a cold cache against the full `claude` bucket).
Subsequent ingests are incremental (etag + parser_version check per
file). `POST /admin/ingest` with the `X-Admin-Token` header and a
same-origin `Origin` header forces an out-of-band run. The request is
served on a worker thread (off the event loop), so the service keeps
answering while the run proceeds.

The server needs outbound reachability to the configured R2 endpoint
and its Postgres instance.

For local dev without R2 credentials, point `CLAUDIT_R2_ENDPOINT` at a
filesystem mirror (e.g. `CLAUDIT_R2_ENDPOINT=file:///tmp/r2/`) — the R2
client falls back to walking the directory tree.

## Operations

### Benchmark per-file parsing

`scripts/bench_parser.py` compares `parse_file()` with an unchanged checkout.
It reads archived JSONLs into memory before timing, clears parser caches for
each file, and alternates baseline/candidate runs sequentially. Every complete
output must match before a timing result is accepted; it does not run ingest
or add parser threads/processes.

```bash
.venv/bin/python scripts/bench_parser.py --baseline /path/to/baseline-checkout \
  --repeat 7 --json /tmp/parser-times.json /path/to/archived-session.jsonl
```

Directories select all descendant JSONLs. Use archived data, identify the
sample being measured, and keep the host idle or reserve a CPU externally
when comparing small timing differences. `--timeout` bounds each sequential
worker (120 seconds by default); imports, file I/O and output hashing are
outside the parser timer.

The deploy is intended to run under systemd. See
[`examples/claudit.service`](examples/claudit.service) for a sample
unit file. Key settings:

- `--timeout-graceful-shutdown 5` so SSE connections drain quickly.
- `TimeoutStopSec=10` for fast restarts.

A restart during an ingest aborts the run cooperatively within the stop
timeout: the run stops at its next bounded step, its `ingest_runs` row
is closed with an "aborted" error instead of lingering unfinished, and
the derived-state rebuild, the `ingest_done` broadcast and the cache
warm are skipped — the next successful run rebuilds all derived state
and converges.

```bash
systemctl restart claudit
systemctl status claudit
journalctl -u claudit -f
```

Schema migrations are applied automatically at every startup, so a
restart is enough after editing `backend/schema.sql`. Applying it by
hand is still safe, and is how you create a fresh database:

```bash
psql claudit -f backend/schema.sql
```

### Rebuilding from the bucket

The database is derived from the bucket except for deployment-specific
state, so a fresh database reproduces byte-identical totals only when the
operator-owned `suppressed_models` and `project_aliases` tables are
restored. Both are populated per deployment and ship empty; neither is
stored in the bucket. Without `suppressed_models`, previously purged
records return during ingest, are counted and priced, and change the
totals. Back up both tables with:

```bash
pg_dump --table=suppressed_models --table=project_aliases --data-only "$CLAUDIT_DATABASE_URL_VIZ" > deployment_state.sql
```

If a database is lost or damaged beyond repair, recreate it with
`createdb` and start the service once so the schema auto-applies. Then
stop the service, restore the saved tables after the schema exists, and
restart the service to kick an ingest with both per-deployment tables in
place:

```bash
psql "$CLAUDIT_DATABASE_URL_VIZ" --file=deployment_state.sql
```

The `user_session` table is also not derived from the bucket; it holds
per-user session secrets and logout generation counters. If it is lost,
every user must sign in again.

## Auth

Login expects a numeric user ID whose row in the auth DB's `users`
table has a PBKDF2 web-password hash stored under
`config.web_password_hash` (with a paired `web_password_salt`). That
table is external — see [Quick start](#quick-start) for the `CREATE
TABLE` an operator runs once, and the two columns (`user_id`, `config`)
startup insists on. The hash is either a bare hex digest — the legacy
shape, verified at
200,000 iterations with the `web_password_salt` value — or a
versioned string `pbkdf2_sha256$<iterations>$<salt>$<hash>` that
carries its own count and salt; `backend/auth.py` verifies both, and
new writes use the versioned shape at 600,000 iterations. An external
user-management process can write either shape to issue credentials —
the `web_password_salt` key must stay populated either way: a
versioned string carries its own salt, but the verifier gates on the
key's presence. Every login credential failure answers one generic
401, and every
failure costs about one PBKDF2 run at the write count — the real
verification where it can run, a dummy remainder run on top where it
cannot or would run cheaper — so account ids cannot be enumerated
from the login endpoint (a stored hash versioned above the target
count still costs longer). The login rate limiter counts 5 failures
per IP+user pair and 20 per IP, per 5-minute window.
Sessions are HMAC-signed cookies with a 7-day TTL. A **Continue as
guest** button mints a read-only guest session (no project filter, no
per-session transcript access; cookie invalidates on every server
restart). Guest login is enabled by default; `CLAUDIT_GUEST_LOGIN=0` turns it
off: the button and "or" divider disappear, `/login/guest` returns 404,
and existing guest cookies are refused. Exporting or erasing one person's
data is an operator procedure, documented in [`PRIVACY.md`](PRIVACY.md).

## Layout

- `backend/` — FastAPI app, auth, ingest, parser (Claude + Codex +
  Kimi), R2 client, pricing, caches, schema, SSE broadcaster.
- `public/` — `index.html`, `app.css`. Served at `/`.
- `src/` — React JSX modules. Served at `/src/*` with mtime-based
  cache-bust.
- `scripts/` — `plots/ccusage_plot_db.py`, our DB-backed usage-plotting
  script (queries the claudit Postgres `records` table), and `ci/`, the
  CI entry points that are too big to live inside a workflow's `run:`
  block (`smoke.py`, `suite_bench.py`).
- `tests/` — pytest suite (parser fixtures, ingest, API).
- `fixtures/` — small JSONL + zip samples for parser tests.
- `examples/` — sample systemd service file.

## Contributing

Issues and PRs welcome — including agent-authored ones. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) for setup, the test suite, and the
invariants most likely to trip a patch.

CI runs on pushes to `master` and on pull requests against it — a pull
request's commits are checked once, never once per event: `tests` (the
full suite with a coverage ratchet on Linux + Postgres, plus a portable
matrix over Linux/macOS/Windows × Python 3.14/3.15 for the no-database
subset; the canary job also measures one pytest pass against the
committed suite-cost budgets), `lint`, `types`, `eslint`, `smoke` (boots the
real server against the fixture mirror), `codeql`, `audit` (`pip-audit`,
daily), `actionlint` (lints the workflows themselves),
`version-guard` (refuses a tree `VERSION` naming an already-published
release), and `release` (master pushes touching `VERSION` only). A branch without a
PR is checked by dispatching a workflow on it: `gh workflow run
tests.yml --ref <branch>`. Most of them run locally too —
`CONTRIBUTING.md` lists the commands.

Releases are cut by editing the root `VERSION` file. Between releases the
tree carries the next version with a `-dev` suffix (`0.4.0-dev`); a
release drops the suffix — `release.yml` waits for every other check on
that commit, then tags `v<VERSION>` — and `VERSION` is bumped to the next
`-dev` immediately after. `version-guard` refuses any master push or PR
whose tree version names a tag that already exists. The running build
reports its version at `/health`.

## License

MIT (see [`LICENSE`](LICENSE), © 2026 Nitjsefnie).
Third-party notices are in [`NOTICE`](NOTICE).

Original visual design and dot-scaling formulas adapted from
[`nhz-io/ccusage-plot`](https://github.com/nhz-io/ccusage-plot),
licensed MIT © 2026 Kumarajiva — see
[upstream LICENSE](https://github.com/nhz-io/ccusage-plot/blob/main/LICENSE).
