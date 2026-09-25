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
  computed at parse time (instrumentation/bash-IO and interrupt-marker
  user messages don't anchor a window).
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
  long-context meter when a pay-as-you-go request's prompt exceeds the
  272k threshold (2× input side, 1.5× output — persisted per record so
  every cost breakdown reconciles). Kimi's kimi-code and legacy wire
  formats land in the same tables with their own rate rows. The
  browser's Inspector parses every format too (`src/parser-lanes.js`).
- **Configurable branding** — `APP_NAME` / `APP_TITLE` /
  `APP_DESCRIPTION` set the browser title, meta description, logo,
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
  • files        (file_key PK = <bucket>/<object-key>, ctx_turns JSONB,
                  rate_limit_hits JSONB)
  • records      (file_key, line_num PK, per-request tokens + cost
                  + text_chars for visible-response size
                  + reply_latency_s for the user→assistant gap
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

`backend/parse.py` implements the parse spec (SV-PARSER-SPEC in
`.claude/rules/claudit-doctrine.md`, pinned by `fixtures/parser/`),
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

- `R2_BUCKET` names one or more buckets joined by `+` (e.g. `R2_BUCKET=claude`
  or `R2_BUCKET=codex+kimi`). Every stored file key is qualified with its
  bucket as `<bucket>/<object-key>`; a bucket not named here is not served
  or listed, and an invalid name aborts startup. Configuring several buckets
  is how one deploy serves Codex and Kimi transcripts beside Claude Code
  ones (a codexmeter / kimimeter deploy).
- `APP_NAME`, `APP_TITLE`, `APP_DESCRIPTION` brand every user-visible
  surface — browser title, meta description, logo text, sign-in page,
  export-PNG filename. Unset, they reproduce the claudit strings exactly;
  a codexmeter deploy sets `APP_NAME=codexmeter` and nothing else changes.
- `EXPORT_PYTHON` names the interpreter the export-PNG subprocess runs
  under (default `/usr/bin/python3`). The plot script imports matplotlib
  and psycopg, and matplotlib ships in `requirements-dev.txt` — not
  `backend/requirements.txt` — so a stock quickstart venv cannot render
  exports. Point it at a Python with both installed; if the interpreter
  is missing one, `/api/export` answers `503` naming `EXPORT_PYTHON`
  instead of an opaque 500.
- The rest of the environment (`DATABASE_URL_VIZ`, `R2_ENDPOINT`, auth and
  admin keys) is documented inline in [`backend/.env.example`](backend/.env.example).

### Client-side internet requirement

The frontend loads its script dependencies from unpkg.com via pinned,
SRI-hashed tags in `public/index.html` (React, ReactDOM, and Babel
standalone for in-browser JSX transpilation). If the browser cannot
reach unpkg.com, the server keeps answering but the page renders
blank — there is no fallback UI. Google Fonts (fonts.googleapis.com /
fonts.gstatic.com) is cosmetic only; if unreachable, the UI falls back
to the system font stacks already declared in `public/app.css`. The
authoritative pin list is `public/index.html` itself.

### First boot of this build over an existing database

The first ingest this build runs over a database created by an earlier
build **re-keys every stored file**: this build's `r2.list_keys` yields
bucket-qualified keys whatever `R2_BUCKET` says, so stored identity
moves from the bare object key to `<bucket>/<object-key>` and each old
row is deleted and re-inserted under its new key in the same run.
Adding a bucket to `R2_BUCKET` on an already-migrated deploy re-keys
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
cp backend/.env.example .env  # edit values
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

The first request blocks while ingest runs (~30s on a warm DB,
several minutes for a cold cache against the full `claude` bucket).
Subsequent ingests are incremental (etag + parser_version check per
file). `POST /admin/ingest` with the `X-Admin-Token` header and a
same-origin `Origin` header forces an out-of-band run. The request is
served on a worker thread (off the event loop), so the service keeps
answering while the run proceeds.

The server needs outbound reachability to the configured R2 endpoint
and its Postgres instance.

For local dev without R2 credentials, point `R2_ENDPOINT` at a
filesystem mirror (e.g. `R2_ENDPOINT=file:///tmp/r2/`) — the R2
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

The database is derived state: everything in it is parsed out of the
bucket on every ingest, so a fresh database re-ingests to byte-identical
totals. If a database is lost or damaged beyond repair, recovery is
`createdb` plus a service restart — the schema auto-applies at startup
and the ingest run repopulates every table. No backup of the database
itself is needed beyond the bucket it is built from.

## Auth

Login expects a numeric user ID whose row in the auth DB's `users`
table has a PBKDF2 web-password hash stored under
`config.web_password_hash` (with a paired `web_password_salt`). The
hash is either a bare hex digest — the legacy shape, verified at
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
per IP+user pair per 5-minute window.
Sessions are HMAC-signed cookies with a 7-day TTL. A **Continue as
guest** button mints a read-only guest session (no project filter, no
per-session transcript access; cookie invalidates on every server
restart).

## Layout

- `backend/` — FastAPI app, auth, ingest, parser (Claude + Codex +
  Kimi), R2 client, pricing, caches, schema, SSE broadcaster.
- `public/` — `index.html`, `app.css`. Served at `/`.
- `src/` — React JSX modules. Served at `/src/*` with mtime-based
  cache-bust.
- `scripts/` — `plots/ccusage_plot_db.py`, our DB-backed usage-plotting
  script (queries the claudit Postgres `records` table), and `ci/`, the
  CI entry points that are too big to live inside a workflow's `run:`
  block (`smoke.py`, `compare_durations.py`).
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
matrix over Linux/macOS/Windows × Python 3.13/3.14 for the no-database
subset), `lint`, `types`, `eslint`, `smoke` (boots the
real server against the fixture mirror), `codeql`, `audit` (`pip-audit`,
daily), `actionlint` (lints the workflows themselves), `speed`
(benchmarks this commit against the last release *on the same runner*),
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
