# claudit

Human-facing overview: [`README.md`](README.md). Process, setup detail
and the CI workflow catalogue: [`CONTRIBUTING.md`](CONTRIBUTING.md). The
invariants live as numbered SV-* rules in
[`.claude/rules/claudit-doctrine.md`](.claude/rules/claudit-doctrine.md);
this brief points at them instead of restating them.

## Project overview

**claudit** (Claude Code Usage Dashboard) is a self-hosted web app that visualises AI coding-session JSONL transcripts (Claude Code, Codex, Kimi). It ingests transcripts from Cloudflare R2 (or a local `file://` mirror), parses them into Postgres, and serves dashboards and raw transcripts to a React frontend rendered via in-browser Babel (no npm/build step). It exposes statistics only — no session-quality analysis, coaching or "auditor"; that scope rule is deliberate (see [Scope](README.md#scope)).

The panel set is described panel-by-panel in README.md. Lines Added/Deleted is per-call churn from tool-call arguments, stored on `tool_uses`: Edit/Write plus the Bash shapes whose line counts are readable off the command text (SV-BASH-CHURN).

## Technology stack

- **Backend**: Python 3.13+, FastAPI, Uvicorn, psycopg3 (pooled)
- **Frontend**: React 18 from CDN, in-browser Babel, vanilla JS/JSX — no webpack, vite or npm install. The root `package.json` holds the **CI toolchain only** (eslint, eslint-plugin-react, c8, globals — devDependencies, nothing served), so the eslint gate can be pinned somewhere Dependabot sees
- **Database**: PostgreSQL — `claudit` for app data, an external auth DB for user credentials
- **Object storage**: Cloudflare R2 via the S3 API, or a local `file://` mirror
- **Scheduling**: APScheduler (BackgroundScheduler), hourly ingest
- **Serialization**: orjson
- **Testing**: pytest, pytest-asyncio, httpx2 (TestClient)
- **Deployment**: systemd (`examples/claudit.service`)

React, ReactDOM and Babel load from unpkg.com via pinned, SRI-hashed tags in `public/index.html`; if unreachable the page renders blank (README: Client-side internet requirement).

## Project structure

```
backend/          — FastAPI application
  app.py          — Startup/shutdown, route mounting, static serving,
                    index.html rewriting (cache-bust, auth injection)
  api.py          — Router assembly + the smaller panel endpoints
                    (/api/me, /api/projects, /api/tool-usage,
                    /api/tool-error-rate, /api/activity-heatmap,
                    /api/cost-by-context, /api/cost-by-agent,
                    /api/reply-latency, /api/events SSE, /api/models,
                    /api/context-growth/{agg,session}); includes the
                    sub-routers below
  api_common.py   — Shared endpoint helpers: dated-rate fold,
                    _parse_range/_bucket_seconds/_iso, HEATMAP_TZ
  rate_boundaries.py — Instants a (model, provider) pair's rate can
                    change, in pricing.resolve's order (SV-DATED-RATES)
  timing.py       — CLAUDIT_TIMING flag, logger setup, Phases collector
  api_dashboard.py — /api/dashboard (sources/queries/build split)
  api_cache.py    — /api/cache (prompt-cache totals, TTL-split cost,
                    top requests)
  api_sessions.py — /api/sessions*, transcript, sidecar
  api_export.py   — /api/export PNG render subprocess
  api_web_metrics.py — POST /api/metrics beacon sink and GET
                    /api/web-metrics p50/p75 readout
  constants.py    — Dependency-free shared constants; keeps the
                    api/ingest import graph acyclic
  turn_flags.py   — Events between two requests, folded onto the NEXT
                    request as records.turn_flags
  parse.py        — JSONL → records, ctx_turns, rate_limit_hits,
                    tool_uses (incl. per-call churn). Implements
                    SV-PARSER-SPEC for the Claude path; parse_file()
                    sniffs the format and dispatches lane formats
  agent_sidecar.py — Agent meta.json sidecar classification: role,
                    teammate marking, teammate_name. Parses no transcript
  prompt_gate.py  — XML-open deny-by-default prompt gate: a user text
                    opening with a tag is harness-injected unless on the
                    pasted_content keep-list; denied texts count no
                    prompt, anchor no latency, split no ctx turn
  parse_codex.py  — Codex rollouts: cumulative-token differencing,
                    cross-file request identity, long-context meter,
                    apply_patch churn
  parse_kimi.py   — Both Kimi wire formats (kimi-code, legacy kimi-cli)
  kimi_content.py — kimi-code content and tool-call helpers
  parse_common.py — Machinery shared by all parsers: per-file state, turn
                    bookkeeping, billing-row builder, tool-result
                    settling and error_kind, _dispatch_prompt_shape,
                    ctx-turn builders
  json_shape.py   — Decoded-JSON object/array guards
  parse_lanes.py  — sniff_format and the lane→claudit row adapter
                    (to_claudit); legacy Kimi tool ids namespaced by file
                    key; lane default roles → DEFAULT_AGENT_TYPE
  key_layout.py   — Object key → (project, session, is_main) for both
                    layouts (Claude's <project>/<session>/<stem>.jsonl,
                    lanes' sessions/<project>/<session>/wire.jsonl);
                    pairs subagent transcripts with their meta.json
  lane_markers.py — Stored project.json markers (etag + reader version);
                    refetched only when missing or stale
  lane_projects.py — Lane-project identity: stored hash→project_id
                    fallback, marker→stored→hash resolution, stall rekey
  project_aliases.py — Per-deploy alias fold (SV-PROJECT-ALIASES)
  branding.py     — Brand values → title, meta, logo, sign-in page,
                    export filename (SV-BRAND-ESCAPE)
  tool_errors.py  — Tool-result text and the harness-generic failure
                    classification (SV-WHY-COLUMNS)
  target_paths.py — Lexical target paths in the transcript's namespace
                    (Windows drive/UNC/device vs POSIX incl. Git-Bash
                    /c); no filesystem lookup
  bash_argv.py    — Churn for ARGV-array commands (Codex `monitor`),
                    handed to bash_churn's scanners
  bash_reads.py   — Read/write targets and whole-vs-slice from Bash text
                    (SV-CONTEXT-INTAKE)
  bash_literals.py — Bounded shell words and literal output, provenance
                    kept; no expansion or execution
  bash_loops.py   — Heredoc repeat count from enclosing literal loops
  bash_dash_c.py  — Python stdin/`-c` matching and inline tokenization
  bash_churn.py   — Churn from Bash text: heredoc writes, git-apply/patch
                    hunks, literal python read/replace/write bodies, and
                    the paths they write. Needs-to-run → 0, never guessed
  bash_churn_errors.py — Whether a heredoc write survives a later
                    stage's failure
  pricing.py      — Resolves model and (model, provider) rates.
                    Logic only (SV-RATE-DATA, SV-PROVIDER-RATES)
  pricing_load.py — Loads src/pricing.json's rate tables, checked, at
                    import; pricing.py re-exports them (SV-RATE-DATA)
  long_context.py — Codex long-context meter constants (threshold,
                    multipliers, model set); dependency-free; pricing
                    re-exports as pricing.LONG_CONTEXT_*
  rate_fingerprint.py — Per-(model, provider) digest of the rate data
                    resolve() consults, plus the pricing modules'
                    source; the reprice pass's clean restamp proves a
                    row clean from it (SV-REPRICE)
  ingest.py       — R2 walk, reparse decision, two-phase persistence,
                    ingest_done SSE
  ingest_walk.py  — Project identity and stored-version helpers
  ingest_persist.py — `_persist`: one file per transaction, INSERT
                    columns and placeholders one per line, same order
  ingest_progress.py — /health progress readout
  ingest_timing.py — Per-run timing state
  ingest_scope.py — Per-run dirty scope and derived-state fingerprint
                    (SV-ROLLUP, SV-SCHEMA-AUTOAPPLY)
  ingest_fetch.py — Fetch/parse worker pools, cooperative abort, thread
                    and process pipelines (INGEST_PARSE_PROCESSES /
                    INGEST_PERSIST_THREADS); re-exported via ingest so
                    tests keep their patch seams
  ingest_rollup_state.py — Suppression, canonical flags, teammate
                    resolution
  ingest_rollup_hourly.py — The seven hour-keyed rollup rebuilds
  ingest_rollup_latency.py — latency_rollup rebuilds
  ingest_rollup_web_metrics.py — web_metrics_rollup rebuilds,
                    over the beacon retention window
  ingest_rollups.py — Derived rebuilds in load-bearing order:
                    suppression, repricing, aliases, canonical flags,
                    teammate resolution, rollups. Re-exports only
  ingest_reprice.py — The reprice pass: recomputes cost_usd and
                    records.long_context in place from stored token
                    columns, no R2 fetch (SV-REPRICE)
  ingest_orphans.py — Orphan sweeps: file rows whose R2 key is gone,
                    unreferenced projects
  ingest_scan.py  — The ingest's listing scan: transcripts paired with
                    meta.json sidecars (_Wire), lane markers
  ingest_runs.py  — ingest_runs row lifecycle: open at run start, close
                    at run end; failure summary for /health
  ingest_lockwatch.py — Liveness guard for the db-wide ingest advisory
                    lock; a lost lock aborts the run (issue #374)
  ingest_warm.py  — Response-cache warming after an ingest or restart
                    (warm_common, WARM_RANGES)
  r2.py           — S3 client with file:// mirror fallback
  auth.py         — PBKDF2-SHA256 hashing/verification
  login.py        — /login, /logout, /login/guest, rate limiting
  login_page.py   — The sign-in page's HTML template and response
                    builder; every slot escaped at its own site, CSP
                    nonce on the inline style (SV-BRAND-ESCAPE)
  login_workers.py — Runs login CPU work without releasing its
                    admission slots early (WorkerReservation)
  session.py      — HMAC session cookies, auth middleware, guest sentinel
  events.py       — Thread-safe SSE broadcaster
  db.py           — viz_pool (claudit) and auth_pool (auth DB,
                    READ-ONLY); pools never join across DBs
  cache.py        — LRU for raw transcript bytes (256 MB, 20-min idle)
  web_metrics.py  — The beacon sink's closed vocabulary and its
                    write; the rollup grain's only writer
  schema*.sql     — Applied at every startup as one script under
                    one stamp, in db.SCHEMA_PATHS order
                    (SV-SCHEMA-AUTOAPPLY)

public/
  index.html      — Bootstraps React/Babel, loads /src/*; rewritten per
                    request to inject window.BACKEND_URL, window.IS_GUEST
                    and mtime ?v= cache-busts
  app.css         — Dark-theme styles

src/              — Served at /src/* (in-browser Babel)
  app.jsx         — Shell, routing, dashboard fetcher, SSE, synthetic preview
  parser.js       — Browser transcript parser (SV-PARSER-SPEC)
  pricing.json    — Every rate table (SV-RATE-DATA)
  parser-lanes.js — Browser lane parser + window.LONG_CONTEXT_*
                    (SV-PARSER-SPEC lockstep)
  dashboard-binning.js — Bin width, never finer than the server bucket
  dashboard-fetch.js — The Overview's /api/dashboard request state:
                    loading / empty / error outcomes (plain JS, so node
                    can execute it)
  dashboard-summary.jsx — The Overview's status block: renders the
                    loading / empty / error outcomes
  dashboard-charts.jsx      — Core SVG panels
  dashboard-charts-extra.jsx — Context growth, cache TTL panels
  context-growth-view.jsx    — Context growth components
  detail-pane.jsx            — Session detail / inspector
  sessions-list.jsx — Sessions page: sortable table of reconstructed
                    sessions
  timeline-selection.js — Shared Inspector timeline-selection: the one
                    clamped selection index every consumer derives
                    from
  event-helpers.jsx          — Event formatting helpers
  synthetic-data.js          — Synthetic dashboard data
  views/cache-view.jsx, views/context-growth-view-v2.jsx

scripts/plots/    — DB-backed usage plot (ccusage_plot_db.py,
                    _render.py, _timeline.py) over `records`; visual
                    parity with upstream nhz-io/ccusage-plot

tests/            — pytest suite (conftest.py forces file-mode R2 and
                    test-safe env defaults)

fixtures/         — parser/, codex/, r2_mini/ (SV-FIXTURE-SIZE)

examples/         — claudit.service

.claude/rules/    — Doctrine (SV-* rules)
```

## Build and test commands

Setup detail lives in CONTRIBUTING.md; the short form:

```bash
createdb claudit
psql claudit -f backend/schema.sql        # idempotent; also auto-applies at startup

# External auth DB — this repo owns no schema for it. Startup aborts on
# db.schema_check() unless users carries the two columns the login reads.
createdb claudit_auth
psql claudit_auth -c "CREATE TABLE users (user_id BIGINT PRIMARY KEY, \
config JSONB NOT NULL DEFAULT '{}'::jsonb)"

cp backend/.env.example .env              # edit: DATABASE_URL_VIZ, DATABASE_URL_AUTH, R2_*, ADMIN_TOKEN
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt

python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

The first request may block on the startup ingest (~30 s warm, minutes
cold against a large bucket); `/health` reflects ingest state via
`ingest_runs`. Without R2 credentials, point `R2_ENDPOINT` at
`fixtures/r2_mini/`.

```bash
# Out-of-band ingest; admin POSTs need a same-origin Origin header.
curl -X POST http://127.0.0.1:8000/admin/ingest \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Origin: http://127.0.0.1:8000"
```

A service restart also kicks an ingest (`systemctl restart|status
claudit`, `journalctl -u claudit -f`).

## Code style

Authoritative in CONTRIBUTING.md: `from __future__ import annotations`
and type hints; raw parameterised (`%s`) psycopg3 SQL, never
interpolated, no ORM; ES2020-ish functional React with `window.`
globals; `snake_case` / `camelCase` / singular SQL table names; lint
configs `.pylintrc`, `setup.cfg`, `pyrightconfig.json`, `eslint.config.mjs`,
style opinions off. Parsers skip malformed JSON lines silently
(`orjson.JSONDecodeError` → `continue`); ingest catches broad
exceptions, logs to `ingest_runs.error`, never crashes the scheduler.

## Testing

Parser tests (`tests/test_parse.py`) are fixture-driven: add a fixture
to `fixtures/parser/` BEFORE changing parser behaviour, test name 1:1
with the feature (SV-FIXTURE-SIZE). API tests (`tests/test_api.py`) use
a fresh temporary DB + mini R2 mirror per fixture and bypass auth by
mounting only `api.router`. Ingest tests cover etag triggers, orphan
deletion, `turn_count` consistency and cross-file uuid retention; auth
tests cover PBKDF2 round-trips and constant-time comparison. Tests never
pin repository-managed data (SV-TEST-DATA).

## Security considerations

- **Auth**: PBKDF2-SHA256, per-user hex salts. A stored hash is a bare
  hex digest (legacy, verified at 200,000 iterations) or
  `pbkdf2_sha256$<iterations>$<salt>$<hash>`; new writes use 600,000.
  `web_password_salt` must stay populated (the verifier gates on it), so
  external user management can issue credentials. Cookies are
  HMAC-signed, `HttpOnly`, `Secure` (`COOKIE_SECURE`), `SameSite=strict`,
  7-day TTL.
- **Login**: every credential failure (unknown id, no password, wrong
  password) returns the same generic 401 body and costs about one PBKDF2
  run at the write count, so ids cannot be enumerated by response or
  timing; residual: a hash versioned above that count costs longer. Rate
  limits: 5 failures per IP+user and 20 per IP, per 5-minute window.
  Both limiters prune on access; history tables cap at 4,096 keys
  (expired swept first, then unseen keys refused), in-flight reservation
  maps likewise; in-flight keys reserve history capacity and live
  windows are never evicted.
- **Guest mode**: `user_id=0` sessions use a per-process secret, so
  cookies die on restart. Guests are blocked from `/api/projects`,
  `/api/sessions*` and `?project=`.
- **Server-side logout, credential-bound sessions**: each user's session
  secret, `generation` and credential fingerprint live in claudit's
  `user_session` table. A token needs a matching generation, and the
  auth-DB user must still exist with the hash and salt of the last login
  (cached ≤ 60 s, so deletion or a password change revokes within that).
  Rows without a fingerprint are invalid until the next login.
  `GET /logout` bumps the generation.
- **No auth-DB writes**: the app only READS `users.config`.
- **Admin**: `POST /admin/ingest` checks `X-Admin-Token` with
  `hmac.compare_digest`.
- **Same-origin**: every mutating route, `/login` and `/login/guest`
  included, requires Origin or Referer host to match Host (no Host →
  refused). `GET /logout` is exempt and relies on `SameSite=strict`.
- **R2 file-mode traversal**: `_safe_join` in `backend/r2.py` refuses
  keys escaping the root via `os.path.realpath`.
- **No local upload path** (SV-NO-LOCAL-UPLOAD).

## Deployment process

Runs under systemd behind a reverse proxy
([`examples/claudit.service`](examples/claudit.service)):
`--timeout-graceful-shutdown 5`, `TimeoutStopSec=10`, `Restart=always`,
`RestartSec=5`, `After=network.target postgresql.service`. A restart
mid-ingest aborts the run cooperatively (`ingest_runs` says "aborted");
the next successful run rebuilds derived state. Schema applies at every
startup (SV-SCHEMA-AUTOAPPLY), so a restart suffices after editing
`schema.sql`. Version bumps ship with their cause (SV-PARSER-VERSION,
SV-REPRICE). `backend/constants.VERSION` reads the tree `VERSION`;
`/health` reports it.

## CI — batch your pushes

**Push a batch of commits once, not one at a time**: intermediate runs
tell you nothing; only the tip matters.

**A branch without a PR is checked by dispatch:**
`gh workflow run ci-gate.yml --ref <branch>`.

**Nineteen workflows; `ci-gate.yml` owns the push/PR surface** and folds
ten gate legs into **`ci gate / aggregate`**. Documentation-only changes
(`*.md`, `PRESENTATION.txt`, `examples/`, `.claude/`, licences) skip the
expensive legs by classification and still pass. The per-workflow table
is in CONTRIBUTING.md; two behaviours documented only here: `speed.yml`
gates one measured pytest pass against committed instruction budgets
(scripts/ci/suite_bench.py; no baseline release involved), and
`refresh-pricing.yml` deliberately keeps its no-cache setup despite the
pip-cache rule. The master-push deploy key lives in the `master-push`
GitHub environment, reachable only from master; the pricing bot's push
runs in a key-only job that receives the tested data as an artifact,
runs no test/dependency/upstream code, and stops its ssh-agent after
the push. Deploy-key pushes start workflows, so the hourly pricing
commit gets a real aggregate verdict on master's tip. The ratchet
push (tests.yml's `ratchet-push` job) instead pushes with the job's
own `GITHUB_TOKEN` — an environment's secret resolves empty inside a
`workflow_call` callee (issue #479) — and a `GITHUB_TOKEN` push starts
no runs, so the ratchet commit is silent by construction.

Run these locally before pushing — CI is the backstop:

```bash
python3 -m pytest tests/ -q --cov=backend          # tests.yml (+ coverage)
git ls-files -co --exclude-standard '*.py' | xargs pylint       # lint.yml, gate 1
git ls-files -co --exclude-standard '*.py' | xargs pycodestyle  # lint.yml, gate 2
pyright                                            # types.yml
npx --no-install eslint 'src/**/*.js' 'src/**/*.jsx'  # eslint.yml
python3 scripts/ci/smoke.py                        # smoke.yml
pip-audit -r backend/requirements.txt -r requirements-dev.txt \
          -r requirements-test.txt                 # audit.yml
actionlint .github/workflows/*.yml && \
  zizmor .github/workflows/                        # actionlint.yml
```

Install the PINNED deps first (`pip install -r backend/requirements.txt
-r requirements-dev.txt -r requirements-test.txt`) — `pyright` resolves
third-party types from installed packages. Use `-co
--exclude-standard`: a new module is untracked until staged, and a bare
`git ls-files` would lint everything except it.

**Releases are cut by editing the root `VERSION`** (one semver line, no
`v`). Between releases it carries the next version with `-dev`; a
release drops the suffix (`release.yml` waits for every other check,
then tags), and `VERSION` is bumped to the next `-dev` right after, by
hand (patch vs minor is a judgement). `version-guard.yml` refuses a
`VERSION` naming a published release.

**Actions are hash-pinned** with the version in a trailing comment —
never "tidy" back to `@v4`. Every workflow sets `permissions:`
explicitly; `zizmor` enforces that and `persist-credentials: false`. A
zizmor suppression goes at the offending line with a justification,
never as a raised `--min-severity`.

**`.gitignore` is deny-by-default**: `*` first, then each shipped path
named back. A new file of an unlisted type is invisible to `git status`;
`git check-ignore -v <path>` names the hiding rule, and the fix is a
name-back rule in the file's directory block — never loosening `*`.

**Coverage and file size are self-raising ratchets** (SV-CI-RATCHETS).

## Development conventions

- **Base a new panel on the closest existing one** — existing panels
  encode non-obvious fixes. Cost by Context is bars plus a cumulative
  line, as `TimeSeriesPanel`'s **Cost (USD)**
  (`src/dashboard-charts.jsx:404-423`) already is:
  - bars are a dim FIELD (`fillOpacity` 0.3, 0.85 on hover), leaving
    room for a line over them;
  - the cumulative line is the SAME hue, made legible by a white halo
    (`stroke="#fff" strokeOpacity="0.15" strokeWidth="4"`) drawn under
    it;
  - hover lives on the container (the tooltip's `offsetParent`), guarded
    to the plot area;
  - axes are named by rotated captions, not a legend.

  `tests/test_panel_wiring.py` pins these at source level on purpose:
  node cannot parse JSX and nothing renders React, so a panel can pass
  the suite and still draw a black rectangle.
- **`records` carries `stop_reason`, `effort` and `thinking_tokens`**
  beside the token columns. `stop_reason` comes only from a reply's
  closing line, so NULL marks a reply whose closing usage never arrived
  and whose `output_tokens` is the 1-3 opening placeholder; use
  `text_chars / 4` for those. `cli_version`, `turn_flags` and
  `turn_tool_results` let a prompt-cache miss be attributed with a
  GROUP BY instead of re-reading the raw file. `turn_flags` are events
  in the window before the request: `stop_hook_block`, `interrupt`,
  `compact`, `api_error`, `model_switch`, `effort_switch`,
  `advisor_switch`, `version_switch`, `tools_delta`, `image_result`,
  `slash_command`, `user_prompt`, `date_change`, `user_rejected`,
  `cwd_switch`, `away_summary`, `resume`, `cwd_rebuild`, and the
  `prompt_snapshot` diffs `tools_change`, `system_change`,
  `prompt_rerender`, backfilled onto the request BEFORE the snapshot
  line (`backend/turn_flags.py`).
- **Rollup grains** (SV-ROLLUP): `usage_rollup` (session × hour × model
  × provider × is_main × long_context), `tool_rollup` (hour × project ×
  model × tool), `tool_error_rollup` (+ error_kind), `dispatch_rollup`
  (hour × project × agent_type × agent_model), `dispatch_brief_rollup`
  (hour × project × agent_type × brief_ref, with summed prompt length),
  `ctx_cost_rollup` and `agent_rollup` (both carry `total_tokens` beside
  `cost_usd`), and `latency_rollup`.
- **`latency_rollup` is stored once per display bucket width**
  (`constants.LATENCY_BUCKETS`) because percentiles do not compose —
  feasible only because the widths are epoch-aligned and few. It also
  stores an all-projects row (`project_id = ''`): a project filter
  changes each group's population, and an all-projects `p50` is not
  derivable from per-project ones.
- **A Codex record above the 272k threshold** bills the whole record on
  the long-context meter, persisted on `records.long_context`
  (SV-DATED-RATES).
- **The rest is doctrine** — read the rule before touching its area:
  cost split (SV-COST-SPLIT), canonical dedup (SV-CANONICAL-FLAG),
  suppression (SV-SUPPRESSED-MODELS), incremental rollups (SV-ROLLUP),
  context intake stays psql-only (SV-CONTEXT-INTAKE), subset tokens
  (SV-SUBSET-TOKENS), version bumps (SV-PARSER-VERSION, SV-REPRICE),
  multi-bucket identity (SV-FILES-RECORDS), parser lockstep and
  self-containment (SV-PARSER-SPEC, SV-READ-ONLY-CANONICAL).
