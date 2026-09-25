# claudit

@README.md

## Project overview

**claudit** (Claude Code Usage Dashboard) is a self-hosted web application that visualises Claude Code session JSONL transcripts. It ingests transcripts from Cloudflare R2 (or a local `file://` mirror), parses them into Postgres, and serves dashboards and raw transcripts to a React frontend rendered via in-browser Babel (no npm/build step).

The dashboard panels include: Session Burn Rate, Cost by Model, Tokens by Model, Token Breakdown, Thinking Output, Prompt-Cache TTL Split, Per-Session Context Growth, Response Sizes, Tool Usage Ratio, Reply Latency, Tool Error Rate, Activity Heatmap, Cost by Context Size, Tokens by Context Size, Cost/Tokens by Agent Type, and Lines Added/Deleted (per-call churn from tool call arguments, stored on `tool_uses`) — Edit/Write plus the Bash shapes whose line counts are readable straight off the command text.

## Technology stack

- **Backend**: Python 3.13+, FastAPI, Uvicorn, psycopg3 (with connection pooling)
- **Frontend**: React 18 (loaded from CDN), in-browser Babel transpilation, vanilla JS/JSX — no webpack, vite, or npm install
- **Database**: PostgreSQL (two separate DBs: `claudit` for app data, external auth DB for user credentials)
- **Object storage**: Cloudflare R2 via S3-compatible API, or local filesystem mirror (`file://`)
- **Scheduling**: APScheduler (BackgroundScheduler) for hourly ingest
- **Serialization**: orjson for fast JSON parsing
- **Testing**: pytest, pytest-asyncio, httpx (for TestClient)
- **Deployment**: systemd service (see `examples/claudit.service`)

## Project structure

```
backend/          — FastAPI application
  app.py          — Startup/shutdown, route mounting, static asset serving,
                    index.html rewriting with cache-bust and auth injection
  api.py          — REST router assembly + the smaller panel endpoints
                    (/api/me, /api/projects, /api/tool-usage,
                    /api/tool-error-rate, /api/activity-heatmap,
                    /api/cost-by-context, /api/cost-by-agent,
                    /api/reply-latency, /api/events SSE, /api/models,
                    /api/context-growth/{agg,session}); includes the four
                    sub-routers below
  api_common.py   — Shared endpoint helpers: Phases timing, dated-rate
                    fold, _parse_range/_bucket_seconds/_iso, HEATMAP_TZ
  api_dashboard.py — /api/dashboard (sources/queries/build split)
  api_cache.py    — /api/cache (per-model and per-session prompt-cache
                    totals, TTL-split cost, top requests)
  api_sessions.py — /api/sessions*, transcript, sidecar
  api_export.py   — /api/export PNG render subprocess
  constants.py    — Dependency-free shared constants (LATENCY_BUCKETS);
                    exists to keep the api/ingest import graph acyclic
  turn_flags.py   — Events between two API requests (blocking Stop
                    hook, interrupt, compaction, /model, /effort, CLI
                    version change, tool-list delta, image result),
                    folded onto the NEXT request as records.turn_flags
                    so prompt-cache misses can be attributed in SQL.
  parse.py        — JSONL → records + ctx_turns + rate_limit_hits +
                    tool_uses (incl. per-call lines_added/lines_deleted
                    for Edit/Write/Bash, derived from the call
                    arguments; errored calls are zeroed).
                    Implements SV-PARSER-SPEC, incl. Phase 1
                    within-file requestId max-merge. Owns
                    the Claude path; parse_file() sniffs the format and
                    dispatches lane formats to the parsers below.
  agent_sidecar.py — Agent-sidecar classification (split out of
                    parse.py): the role a transcript's meta.json sidecar
                    names (sidecar_agent_role), whether it marks a named
                    teammate, and the teammate_name recorded for the
                    ingest-time join. Nothing here parses a transcript.
  parse_codex.py  — Codex rollout parser (ported from codexmeter):
                    cumulative-token differencing, cross-file request
                    identity for uuid dedup, per-record long-context
                    meter, apply_patch churn attribution.
  parse_kimi.py   — The two Kimi wire formats (kimi-code and legacy
                    kimi-cli), ported from codexmeter's parsers.
  parse_common.py — Format-independent machinery both lane parsers and
                    the Claude path share: per-file parse state, turn
                    bookkeeping, the billing-row builder (records the
                    long_context flag), tool-result settling incl. the
                    harness-generic error_kind classification, and
                    _dispatch_prompt_shape (a dispatch prompt's length
                    and brief reference, shared by the Claude path and
                    Kimi's Agent calls).
  parse_lanes.py  — Format sniffing (sniff_format) and the lane→claudit
                    row adapter (to_claudit): lane row shapes project
                    onto claudit's records/tool_uses columns, legacy
                    Kimi tool ids are namespaced by file key.
                    lane_agent_type / lane_sidecar_agent_type normalise
                    a lane's default role (Codex "default", kimi-code
                    "agent") and a missing one to DEFAULT_AGENT_TYPE.
  key_layout.py   — Object key → (project, session, is_main). The one
                    place that knows both layouts: Claude Code's
                    <project>/<session>/<stem>.jsonl and the lane
                    sessions/<project>/<session>/wire.jsonl tree.
                    Also pairs a subagent transcript with the
                    meta.json sidecar beside it (transcript_stem /
                    sidecar_stem), whose role fills agent_type when
                    the transcript names none in-band.
  lane_markers.py — Stored lane project markers: what each
                    sessions/<project>/project.json said at its last GET
                    (etag + reader version), so a marker is fetched only
                    when it has no row or its row holds another etag or
                    reader version.
  lane_projects.py — Lane-project identity: the stored hash→project_id
                    mapping a marker-failed run falls back to, the
                    marker→stored→hash resolution the walk uses, and the
                    migration-stall rekey.
  branding.py     — APP_NAME/APP_TITLE/APP_DESCRIPTION → browser title,
                    meta, logo, sign-in page, export filename. Escapes
                    per context (HTML vs script payload) — see
                    SV-BRAND-ESCAPE.
  tool_errors.py  — Tool-result text handling and the HARNESS-GENERIC
                    failure classification (rejected / tool_error /
                    failed) shared by every format.
  target_paths.py — Lexical target paths in the transcript's namespace,
                    never the host's: Windows drive/UNC/device vs POSIX
                    (incl. Git-Bash /c) spellings. No mount, symlink,
                    filesystem case or short-name lookup occurs.
  bash_argv.py    — Line churn for a command given as an ARGV ARRAY
                    (Codex's `monitor` calls): finds the inline shell/
                    python payload positionally and hands it to
                    bash_churn's scanners, so the estimates stay
                    claudit's.
  bash_reads.py   — read/write TARGETS recovered from Bash command
                    TEXT, plus whether the command emitted the file
                    whole or a slice. Bash is ~79% of the read surface
                    under bypass permissions, so `Read` arguments alone
                    see almost none of the intake.
  bash_literals.py — Bounded shell words and literal output for the
                    Bash scanners: quote/expansion provenance kept until
                    a consumer decides a word is literal. No expansion
                    or command execution.
  bash_loops.py   — How many times a heredoc body runs, read off the
                    loops around it: a literal `for` word list
                    multiplies the count; a runtime list counts one, the
                    floor. Split out of bash_churn so that module stays
                    one concern.
  bash_churn.py   — lines_added/lines_deleted recovered from Bash
                    command TEXT: heredoc bodies redirected into a file,
                    inline git-apply/patch hunks, and python
                    read/replace/write bodies (heredoc or -c) whose
                    replacement strings are literals — inline, via a
                    name bound to a literal at that statement, or via
                    a same-script helper called with literals. Also
                    the paths those bodies open for writing, and
                    whether a heredoc write survives a later stage's
                    nonzero exit. Anything needing the command to RUN
                    counts 0, never an estimate.
  pricing.py      — Loads src/pricing.json and resolves a record to its
                    per-model token rates (USD/M), or to PROVIDER_RATES:
                    per-(model, serving host) rates for OpenRouter
                    records, which carry message.provider (stored as
                    records.provider). A record with no provider prices by
                    model alone (SV-PROVIDER-RATES). Logic only; the rates
                    are data (SV-RATE-DATA).
  ingest.py       — R2 walk, etag/parser-version reparse decision, persistence
                    in two-phase transactions, broadcasts ingest_done SSE.
  ingest_persist.py — ingest's `_persist` alone: one file per
                    transaction, with INSERT columns and VALUES
                    placeholders laid out one per line in the same order
                    (issue #86). Re-exported from ingest.
  ingest_rollups.py — The derived-table rebuilds ingest runs after each
                    walk, in a load-bearing order: suppression, the
                    canonical flags, every rollup, the teammate
                    agent_type resolution. Nothing here fetches, parses
                    or persists. Re-exported from ingest.
  r2.py           — S3 client with file:// filesystem-mirror fallback for dev.
  auth.py         — PBKDF2-SHA256 password hashing/verification helpers
                    (versioned hash format; a legacy bare-hex hash still
                    verifies at 200,000 iterations, unchanged).
  login.py        — /login GET/POST, /logout, /login/guest, rate-limiting.
  session.py      — HMAC-signed session cookie mint/verify, auth middleware,
                    guest-mode sentinel (user_id=0, per-process secret).
  events.py       — Thread-safe SSE broadcaster (asyncio.Queue per client).
  db.py           — Two psycopg pools: viz_pool (claudit) and auth_pool
                    (the shared auth DB, which is now genuinely
                    READ-ONLY from this application — user session
                    secrets live in claudit's own database, the
                    user_session table). Pools never join across DBs.
  cache.py        — In-process LRU with idle-time eviction for raw transcript
                    bytes (256 MB, 20-min idle).
  schema.sql      — Applied at every startup by db.apply_schema().
                    Idempotent CREATE TABLE IF NOT EXISTS + safe
                    ALTER TABLE ... ADD COLUMN IF NOT EXISTS migrations,
                    plus guarded, idempotent constraint widenings on
                    derived state (the usage_rollup PK swaps — see
                    SV-SCHEMA-AUTOAPPLY).

public/           — Static assets served at /
  index.html      — Bootstraps React, Babel from CDN; loads /src/*.
                    Backend rewrites this on every request to inject
                    window.BACKEND_URL, window.IS_GUEST, and mtime-based ?v=
                    cache-bust query strings on every static asset reference.
  app.css         — Dark-theme dashboard styles.

src/              — React JSX modules served at /src/* (in-browser Babel)
  app.jsx         — Top-level shell, routing, dashboard fetcher, SSE listener,
                    synthetic data preview.
  parser.js       — In-browser transcript parser for backend-fetched
                    transcripts; prices from src/pricing.json with the
                    same resolution logic as backend/pricing.py.
  pricing.json    — Every rate table, as append-only per-row histories.
                    The single source both sides read (SV-RATE-DATA).
  parser-lanes.js — The lane formats' browser parser (Codex + both Kimi
                    wires), mirrored from backend parse_codex/parse_kimi
                    (SV-PARSER-SPEC lockstep) so the Inspector's
                    in-browser parse yields the stored numbers, plus the
                    window.LONG_CONTEXT_* constants.
  dashboard-binning.js — Shared dashboard bin-width selection: frontend
                    bins never finer than the server-provided bucket.
  dashboard-charts.jsx      — Core SVG panels (time series, HBar, burn rate).
  dashboard-charts-extra.jsx — Additional panels (context growth, cache TTL).
  context-growth-view.jsx    — Context growth visualisation components.
  detail-pane.jsx            — Session detail / inspector panes.
  event-helpers.jsx          — Shared event formatting helpers.
  synthetic-data.js          — Synthetic dashboard data generator.
  views/
    cache-view.jsx           — Cache analysis view.
    context-growth-view-v2.jsx — Updated context growth view.

scripts/          — scripts/plots/: our DB-backed usage-plotting script,
                    split into ccusage_plot_db.py (DSN resolution,
                    load_events, find_limit_hits, CLI main),
                    ccusage_plot_render.py (theme, burn-rate panel,
                    session/EMA math) and ccusage_plot_timeline.py
                    (metric panels, plot_timeline). Queries the claudit
                    Postgres `records` table (visual-design parity with
                    upstream nhz-io/ccusage-plot).

tests/            — pytest suite
  conftest.py     — Injects repo root into sys.path; forces file-mode R2 and
                    test-safe env defaults (COOKIE_SECURE=0, etc.).
  test_parse.py   — Fixture-driven parser tests (see fixtures/parser/).
  test_api.py     — End-to-end API tests with fresh DB + mini R2 mirror.
  test_ingest.py  — Ingest pipeline tests (etag triggers, orphan deletion).
  test_auth.py    — PBKDF2 round-trip and known-vector tests.
  test_pricing.py — Rate lookup and cost computation tests.
  test_r2.py      — R2 client (S3 + file:// mode) tests.
  test_login.py   — Login flow tests.
  test_session.py — Session token mint/verify tests.

fixtures/         — Small JSONL + zip samples for parser and API tests.
  parser/         — Hand-crafted single-record samples, each under 1 KB.
  codex/          — Ported verbatim from the public codexmeter repo;
                    exempt from the 1 KB cap (see SV-FIXTURE-SIZE).
  r2_mini/        — Mini filesystem mirror (2 projects, 4 sessions, 1 peer,
                    1 cross-session shared uuid) for ingest/API tests.

examples/         — Sample systemd service file (claudit.service).

.claude/rules/    — Local doctrine (SV-PARSER-SPEC, SV-COST-SPLIT, etc.).
```

## Build and test commands

### Setup

```bash
# 1. Create the app database and apply schema
createdb claudit
psql claudit -f backend/schema.sql

# 2. Configure environment
cp backend/.env.example .env
# Edit .env to set real DATABASE_URL_VIZ, DATABASE_URL_AUTH, R2_*, ADMIN_TOKEN

# 3. Create virtualenv and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

### Run the server

```bash
python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

The first request may block while the startup ingest runs (~30 s on a warm DB, several minutes for a cold cache against a large bucket). `/health` reflects ingest state via the `ingest_runs` table.

For local dev without R2 credentials, point `R2_ENDPOINT` at a filesystem mirror (e.g. `R2_ENDPOINT=file:///path/to/mirror/`) — the R2 client falls back to walking the directory tree.

### Run tests

```bash
# Full suite (requires local PostgreSQL for test DB creation)
python3 -m pytest tests/ -q

# Individual modules
python3 -m pytest tests/test_parse.py -v
python3 -m pytest tests/test_api.py -v
python3 -m pytest tests/test_ingest.py -v
```

Tests use fixture-driven data, not real R2. `conftest.py` forces `R2_ENDPOINT=file:///tmp/sv-test-r2/` and sets `COOKIE_SECURE=0` so TestClient cookies work over plain HTTP.

### Manual operations

```bash
# Force an out-of-band ingest run. Admin POSTs are origin-checked, so
# the request must carry a same-origin Origin header. The run itself is
# served on a worker thread (off the event loop), so the service keeps
# answering while the ingest proceeds.
curl -X POST http://127.0.0.1:8000/admin/ingest \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Origin: http://127.0.0.1:8000"

# Restarting the service also kicks a fresh ingest (a startup ingest
# runs on every boot of backend/app.py — equivalent to the curl above
# for any case where the admin token isn't handy or the service was
# already going to be restarted for another reason).
systemctl restart claudit

# Re-apply schema migrations (idempotent)
psql claudit -f backend/schema.sql
```

## Code style guidelines

- **Python**: `from __future__ import annotations` at the top of every `.py` file; type hints used throughout; no ORM — raw SQL via psycopg3.
- **JavaScript/JSX**: ES2020-ish, React functional components with hooks; globals attached to `window.` for cross-module sharing (e.g. `window.parseTranscript`, `window.rateForModel`).
- **SQL**: Parameterised queries only (`%s` placeholders); never interpolate user input into query strings. Cross-file uuid dedup is resolved at INGEST into `records.is_canonical` (SV-CANONICAL-FLAG); read paths filter that boolean and must not reintroduce `DISTINCT ON (uuid)`.
- **Naming**: `snake_case` for Python; `camelCase` for JS/JSX; SQL tables are singular nouns.
- **Error handling**: Parser silently skips malformed JSON lines (`orjson.JSONDecodeError` → `continue`). Ingest catches broad exceptions, logs to `ingest_runs.error`, and never crashes the scheduler.

## Testing instructions

- **Parser tests** (`test_parse.py`) are fixture-driven. Add a JSONL fixture to `fixtures/parser/` before changing parser behaviour, and map the test name 1:1 to the feature.
- **API tests** (`test_api.py`) spin up a fresh temporary DB + mini R2 mirror per fixture. They bypass auth by mounting only the `api.router` into a clean FastAPI app.
- **Ingest tests** (`test_ingest.py`) validate etag-based reparse triggers, orphan deletion, `turn_count` consistency, and cross-file uuid write-time retention (every row is kept; the winner is flagged `is_canonical`).
- **Auth tests** (`test_auth.py`) verify PBKDF2 round-trips and constant-time comparison against garbage inputs.
- Keep fixture files small: `fixtures/parser/*.jsonl` under 1 KB each; `fixtures/r2_mini/` under a few KB. Larger samples stay out of the repo, in a local mirror you point `R2_ENDPOINT` at (not committed).

## Security considerations

- **Auth**: PBKDF2-SHA256 password hashes with per-user hex salts. A stored hash is either a bare hex digest — the legacy shape, always verified at 200,000 iterations — or a versioned string `pbkdf2_sha256$<iterations>$<salt>$<hash>` that carries its own count; new writes use the versioned format at 600,000 iterations. A legacy bare-hex hash verifies unchanged, so hashes written by an external user-management process keep working. Session cookies are HMAC-signed, `HttpOnly`, `Secure` (configurable via `COOKIE_SECURE`), `SameSite=strict`, 7-day TTL.
- **Login**: every credential failure — unknown id, no configured password, wrong password — answers the same generic 401 with an identical body, and every failure costs about one PBKDF2 run at the write count (600,000): the real verification where it can run, a dummy remainder run on top where it cannot or would run cheaper (a legacy 200,000-iteration hash, a malformed one), so an account id cannot be enumerated by response or timing — with one documented residual: a stored hash versioned above the target count still costs longer. The rate limiter keys 5 failures per IP+user pair per 5-minute window (one user's failures never lock a different user behind the same egress IP), prunes expired entries per key on access, and sweeps fully expired keys once the table grows past a cap, so it never grows without bound.
- **Guest mode**: `user_id=0` sessions are signed with a per-process secret regenerated at startup; cookies invalidate on restart. Guests are blocked from `/api/projects`, `/api/sessions*`, and `?project=` filter params.
- **Server-side logout (issue #108)**: each real user's session secret and a per-user `generation` counter live in the app's own `user_session` table. A session token carries the generation it was minted at, and verification requires it to still be current — so `GET /logout`, before clearing the cookie, bumps that user's generation and every token that user holds (in any browser) stops verifying, not just the cookie the response clears. Guests have no row and no generation: they keep dying on restart via the process-local secret.
- **No auth-DB writes (issue #94)**: user session secrets live in claudit's own `user_session` table; the application only ever READS the shared auth DB (`users.config`, for the password hash). Leftover `web_session_secret` values in the shared table are inert, and cleaning them up is the auth-DB owner's business.
- **Admin**: `POST /admin/ingest` requires `X-Admin-Token` header, checked via constant-time `hmac.compare_digest`.
- **Same-origin**: every mutating route (anything not GET/HEAD/OPTIONS) — including `/login` and `/login/guest` — enforces an origin/referer check: the `Origin` (or `Referer`) header's host must match the request's `Host` header, and a request with no `Host` header is refused. Browsers always send `Origin` on POST, so this only affects command-line/scripted clients, which must send a matching header. `GET /logout` is exempt as a safe method and leans on the cookie's `SameSite=strict` instead, which keeps a cross-site logout request from carrying the cookie at all.
- **R2 file-mode path traversal**: `_safe_join` in `backend/r2.py` uses `os.path.realpath` to refuse keys that escape the bucket root (defence for sidecar `?path=../../../etc/passwd` attacks).
- **SQL injection**: All DB access uses parameterised psycopg3 queries.
- **No local upload path at all**: there is no drag-drop target, no `FileReader`, and no upload endpoint. The backend only reads JSONLs from R2 (or its local mirror). Session transcripts are fetched from `/api/sessions/{id}/transcript` and parsed in the browser.

## Deployment process

Intended to run under systemd behind a reverse proxy. Key settings from `examples/claudit.service`:

- `--timeout-graceful-shutdown 5` so SSE connections drain quickly.
- `TimeoutStopSec=10` for fast restarts.
- `Restart=always` with `RestartSec=5`.
- `After=network.target postgresql.service`.

A restart during an ingest aborts the run cooperatively within the stop timeout: the aborted run's `ingest_runs` row says it was aborted, and the next successful run rebuilds all derived state and converges.

```bash
# Typical systemd workflow
systemctl restart claudit
systemctl status claudit
journalctl -u claudit -f
```

Schema migrations are **applied automatically at startup**: `db.apply_schema()` runs `backend/schema.sql` before `schema_check()` on every boot, so a deploy cannot outrun its database (issue #43). Re-applying by hand stays harmless and is still how you create a fresh DB. Bump `PARSER_VERSION` in `backend/constants.py` whenever parser semantics change or a rate change reprices stored records; every file reparses on the next ingest. It is a code constant so the bump travels in the same commit as the change that needs it.

## CI — batch your pushes

Every gate workflow runs on **pushes to `master` that touch code** (a
`paths-ignore` deny-list skips `*.md`, `PRESENTATION.txt`,
`examples/`, `.claude/`, licences — a deny-list on purpose, so a new code
directory can't silently stop being tested) **and on pull requests
against `master`** — a pull request's commits are checked ONCE, on the
merge ref, never once per event. Postgres 16 service container; fixtures
`createdb`/`dropdb` per module, so `PGHOST`/`PGUSER`/`PGPASSWORD` drive
both libpq and the shelled-out `psql`.

**The tests workflow runs the suite twice over a matrix and a job.** The
`pytest` job keeps the FULL suite with its coverage ratchet on Linux +
Postgres 16, exactly where it was. A second job, `pytest-portable`, runs
the same suite minus every database test — `-m "not db"` — matrixed over
ubuntu/macos/windows × Python 3.13/3.14, the platforms and Pythons a
Postgres service container cannot reach; it has no services, no `psql`,
and no coverage steps. The db/portable split is MECHANICAL, not a
hand-kept list: `tests/conftest.py` marks every test whose fixture
closure reaches a fixture registered in `tests/db_marker.py`'s
`DB_FIXTURES`, the tests that reach a server without any DB fixture
carry `@pytest.mark.db` explicitly, and `tests/test_db_marker.py`
re-derives both from source and fails on drift — so an unmarked database
test fails the marker's own meta-test before it can fail the portable
cells on a missing server.

**Push a batch of commits once, not one at a time.** Pushing N related
commits individually starts N CI runs; the intermediate ones tell you
nothing, burn runner minutes, and the only result that matters is the
tip. Commit as granularly as you like locally — then push once when the
group is done. (`cancel-in-progress` on pull_request runs limits the
damage by cancelling superseded runs; master pushes queue instead of
cancelling each other, but the right fix is not generating them.)

**A branch without a PR is checked by dispatch.** A branch push no longer
fires CI — only a `master` one does — so to check a working branch,
dispatch the workflow on it: `gh workflow run tests.yml --ref <branch>`
(every gate carries `workflow_dispatch` for exactly this).

**There are THIRTEEN workflows, not one.** `tests.yml` is the one people
remember, and a green pytest says nothing about the other twelve. Six run
locally — run them before pushing, because CI is the backstop, not the
first check:

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

**Run them in an environment with the PINNED deps installed**, not
whatever your interpreter happens to have. `pyright` resolves third-party
types from the installed packages, so a stale local psycopg makes it
disagree with CI — which is exactly how a psycopg 3.3 typing change got
through a locally-green `pyright` and turned `types` red on the push:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt -r requirements-dev.txt -r requirements-test.txt
# or point pyright at an existing one:
pyright --pythonpath /path/to/venv/bin/python
```

The seven that only make sense on GitHub:

| Workflow | Question it answers | Trigger |
| --- | --- | --- |
| `codeql.yml` | Is there a security defect in the Python or JS? Results go to the Security tab, never the build. | push + PR + weekly cron. The cron is NOT redundant: a query published today would otherwise only ever run against files touched after it shipped. Under `pull_request` the checkout takes the merge ref — the same commit `analyze` files SARIF against — so alerts land on the tree actually analysed. |
| `audit.yml` | Are the frozen pins still free of advisories? Resolves the full transitive tree, which is the point — nothing here pins `starlette`. | push + PR + **daily** cron. The cron is the important half: this answer changes with no commit to hang it on. |
| `speed.yml` | Did the tests that exist in both this commit and its baseline get >30% slower? The baseline is the last release on a master push, the branch's merge base on a pull request. | push + PR. Runs BOTH builds on the same runner, interleaved in pairs after a discarded warm-up round; the verdict is the median of the paired ratios. Skips green while no release exists (master pushes only — a PR always has a merge base). A fork PR waits for a reviewer's approval in the `fork-speed-benchmark` environment before its code runs. |
| `release.yml` | — | push to `master` touching `VERSION`. Waits for every other check on that SHA, then tags `v<VERSION>`. A dev version (`X.Y.Z-dev`) skips every step — nothing is tagged. |
| `version-guard.yml` | Does the tree `VERSION` name a version that has already shipped? Fails a master push or PR whose `VERSION` matches an existing `v<VERSION>` tag — under the dev-suffix discipline this only ever fires on a missed bump. The hourly pricing bot's commits (author AND file shape: only `src/pricing.json` + `backend/constants.py`) are exempt; PRs get no carve-out. | push to `master` + PR, deliberately no path filter: the tag set changes when a release lands, independently of any push. |
| `refresh-pricing.yml` | — (a data job, not a gate) Re-fetches OpenRouter's per-provider prices, appends every moved or new rate effective from the detection time, bumps `PARSER_VERSION`, and commits to `master` as `github-actions[bot]` after the suite passes on the new data (SV-RATE-REFRESH). A refused host blocks only itself: every other move is still committed, then the run goes red, naming the host to read by hand. | hourly cron + `workflow_dispatch`, `master` only. Its push starts no other workflow. |
| `claim.yml` | Can a contributor without write access take an issue? `/claim` on an open, unassigned issue assigns the commenter; `/unclaim` and `/release` remove only the commenter's own assignment. Runs no repository code — talks to the API only. | `issue_comment` (created), prefiltered to a command-bearing comment on an open non-PR issue from a non-Bot; the action re-checks all of it exactly. |

**Coverage and file size are self-raising ratchets**, each checked by a
step of its own in `tests.yml` so "tests failed" / "coverage dropped" /
"file grew" stay distinguishable. Both live as committed data in
`.github/ci-thresholds.json`: the coverage floor sits a fixed 1.5 points
under the recorded measured value, and on a `master` push CI raises the
floor automatically once a run measures more than the 1.5-point
hysteresis above the recorded measured. The floor is never lowered by
hand. File size replaces pylint's flat 1000-line limit with a per-file
ceiling (`module_size_baseline`: production 500 / test 700 lines, with
entries seeded for files already over); CI tightens an entry as its file
shrinks and drops it once the file is back under the ceiling, but
entries are never added or raised by hand — growth is fixed by moving
code into a new module. The data file is ignored by every gate
workflow's push trigger, so the bot's ratchet commit never re-triggers
CI.

**Release = edit `VERSION`.** One semver line at the repo root, no
leading `v`. Between releases the tree carries the next version with a
`-dev` suffix (`0.4.0-dev`); a release drops the suffix, and `VERSION`
is bumped to the next `-dev` immediately after a release lands — the
tree version never names an already-published release, which
`version-guard.yml` enforces on every master push and PR (the hourly
pricing bot's own commits are exempt). `release.yml` reacts to a dropped
suffix and skips a dev version entirely; nothing bumps it automatically,
because deciding patch-vs-minor is a judgement about what changed.
`backend/constants.VERSION` reads it and `/health` reports it.

**`-co --exclude-standard`, not a bare `git ls-files`.** CI lints the
committed tree, so the workflow's own `git ls-files '*.py'` is right
*there*. Locally it is a trap: a brand-new module is untracked until
you stage it, `git ls-files` never lists it, and pylint reports a
clean run over every file except the one you just wrote. `-c`
(cached) plus `-o` (other) covers both, and `--exclude-standard`
keeps `.gitignore`d files out.

Toolchain and pinned versions: `requirements-dev.txt` (lint/type) and
`requirements-test.txt` (coverage). Configs are `.pylintrc`, `setup.cfg`,
`pyrightconfig.json`, `.eslintrc.json` — style opinions (line length) are
deliberately off, so what pylint does flag is a real finding, not
formatting taste.

**Actions are hash-pinned**, with the version in a trailing comment. Do
not "tidy" one back to `@v4`: a tag is a moving pointer, and these jobs
hold a repository token. Dependabot keeps the hashes current. Every
workflow also sets `permissions:` explicitly and passes
`persist-credentials: false` to checkout — `zizmor` enforces all three,
and a suppression belongs at the offending line with a justification (see
`eslint.yml`), never as a raised `--min-severity`.

**`.gitignore` is deny-by-default**: `*` first, then each shipped path
named back. A new file of an unlisted type is invisible to git and will
NOT appear in `git status` — `git check-ignore -v <path>` names the rule
hiding it, and the fix is a name-back rule in the file's own directory
block. Never "fix" it by loosening the leading `*`.

## Development conventions

- **Read what already exists before adding a panel, and base your style
  on it.** Find the closest existing panel and copy its treatment rather
  than inventing one. This is not a tidiness rule — the existing panels
  encode solutions to problems that are not obvious until you have
  already shipped the bug. Cost by Context is bars plus a cumulative
  line, which `TimeSeriesPanel`'s **Cost (USD)**
  (`src/dashboard-charts.jsx:404-423`) already is; skipping that read
  cost four rounds of fixes for problems it had solved:
  - bars are a dim FIELD (`fillOpacity` 0.3, 0.85 on hover), not a
    bright one — a bright field leaves no room for a line over it, and
    no line colour can win: bright enough to read on the bars is
    invisible on the dark surface, and the reverse. Measured at 1.07:1
    contrast, i.e. the same luminance;
  - the cumulative line is the SAME hue as the bars, made legible by a
    white halo (`stroke="#fff" strokeOpacity="0.15" strokeWidth="4"`)
    drawn under it — not by a second colour, which also has to survive
    a CVD check it will probably fail;
  - hover lives on the container (the tooltip's `offsetParent`) and is
    guarded to the plot area, so the tip never shows over the header;
  - the two axes are named by rotated captions, not a legend.

  `tests/test_panel_wiring.py` pins these against the reference, so a
  copy that drifts fails rather than merely looking different. Its
  guards are source-level on purpose: node cannot parse JSX and nothing
  here renders React, so a panel can pass the whole suite and still draw
  a black rectangle — which is exactly what shipped.
- **Context intake is stored per call, and stays psql-only.**
  `tool_uses` carries `result_chars` (result size, images included),
  `read_targets` / `write_targets` (TEXT[]), `read_kind`
  (`whole`/`slice`) and `is_reread`. Bash targets are recovered from
  command TEXT (`backend/bash_reads.py`) because Bash is ~79% of the
  read surface. No endpoint, no panel, no rollup — SV-CONTEXT-INTAKE.
  Measured corpus-wide, duplicate non-image whole reads are 0.47% of
  all result bytes, which is why a panel would chart an outlier rather
  than a habit. Query it: `SELECT sum(result_chars) FROM tool_uses
  WHERE is_reread;`
- **A token type may be a SUBSET.** `thinking_tokens` is part of `output_tokens` (the API reports it under `usage.output_tokens_details`), so it gets its own panel and its own `usage_rollup` column but is never summed into a total or priced — see SV-SUBSET-TOKENS. Everything else in `TOKEN_TYPE_FIELDS` partitions the billed tokens exactly once.
- **Cost is always TTL-split**. `cache_creation` decomposes into `ephemeral_5m` (× 1.25 base) + `ephemeral_1h` (× 2 base). Tokens with no `ephemeral_*` split are charged at the 1h rate — 1h is the main-session norm (98.7% of their writes), and 5m is the subagent exception (SV-COST-SPLIT). Single-rate `cache_create` cost is banned.
- **Cross-file uuid dedup is resolved at INGEST** into `records.is_canonical` (SV-CANONICAL-FLAG); read paths filter that boolean and must not reintroduce `DISTINCT ON (uuid)`. Per-file `requestId` max-merge also happens at ingest. The same pass sets `tool_uses.is_canonical` on `tool_use_id`, because a compaction sidecar (`agent-acompact-*`) replays the main file's tool calls; every rollup and live read over `tool_uses` filters it.
- **`records` carries `stop_reason`, `effort` and `thinking_tokens`** alongside the token columns. `stop_reason` comes only from a reply's closing line, so NULL marks a reply whose closing usage never arrived and whose `output_tokens` is the 1-3 opening placeholder; `text_chars / 4` is the honest estimate for those. `cli_version`, `turn_flags` (events in the window before the request: `stop_hook_block`, `interrupt`, `compact`, `api_error`, `model_switch`, `effort_switch`, `advisor_switch`, `version_switch`, `tools_delta`, `image_result`, `slash_command`, `user_prompt`, `date_change`, `user_rejected`, `cwd_switch`, `away_summary`, `resume`, `cwd_rebuild`, and the `prompt_snapshot` diffs `tools_change`, `system_change`, `prompt_rerender`, which are backfilled onto the request BEFORE the snapshot line — see `backend/turn_flags.py`) and `turn_tool_results` exist so a prompt-cache miss can be attributed with a GROUP BY instead of a re-read of the raw file.
- **Foreign-model records are purged at ingest**, not filtered at read time — `suppressed_models` holds `ILIKE` patterns and `ingest.purge_suppressed()` deletes matching `records` and their `tool_uses` before the canonical pass (SV-SUPPRESSED-MODELS). The table ships empty; populate it per deploy. `files.models` keeps every model the file contained, recorded before the purge, so a session that switched lanes can still be identified and excluded from an analysis.
- **Aggregates are precomputed at ingest** into `usage_rollup` (grain: session × hour × model × provider × is_main × long_context), `tool_rollup` (hour × project × model × tool), `tool_error_rollup` (hour × project × model × tool × error_kind), `dispatch_rollup` (hour × project × agent_type × agent_model), `dispatch_brief_rollup` (hour × project × agent_type × brief_ref, carrying a summed prompt length alongside the count), `ctx_cost_rollup` and `agent_rollup` (both carrying `total_tokens` beside `cost_usd`, so the tokens variant of each panel needs no second pass) and `latency_rollup` — see SV-ROLLUP. The first two hold pure sums/counts/min/max and are summed up to the display bucket; they are valid only for buckets ≥ 1h, so the 24h view takes a live path.
- **`latency_rollup` is different**: percentiles do NOT compose across buckets, so it is stored once *per display bucket width* (`constants.LATENCY_BUCKETS`) — possible only because the widths are epoch-aligned and there are just a handful. It also stores a separate all-projects row (`project_id = ''`), because a project filter changes the population inside each group and `p50` over all projects is not derivable from per-project `p50`s. Response-size percentiles are still live.
- **Parsing is self-contained.** Never invoke or vendor a parser from outside the repo (SV-READ-ONLY-CANONICAL). `backend/parse.py` and `src/parser.js` implement SV-PARSER-SPEC; when they drift, fix it here against the spec and the parser fixtures.
- **Tests use fixtures, not real R2.** The R2 client supports `R2_ENDPOINT=file:///path/to/mirror/` for offline dev.
- **Parser version invalidation:** Bump `PARSER_VERSION` in `backend/constants.py` whenever parser semantics change or a `src/pricing.json` change reprices stored records — every file reparses on next ingest. Never an env var: a parser change and its reparse must ship together.
- **Several buckets, several formats, one deploy.** `R2_BUCKET` may name several buckets joined by `+`; every stored file key is `<bucket>/<object-key>` and the bucket comes from the stored key, never the request. `parse_file()` sniffs the format (Claude, Codex rollout, kimi-code, legacy Kimi) and dispatches; the lane parsers and `src/parser-lanes.js` are in lockstep (SV-PARSER-SPEC). A pay-as-you-go Codex record above the 272k threshold bills the whole record on the long-context meter, persisted on `records.long_context` and applied by every per-component cost re-derivation (SV-DATED-RATES).
- **Backend is the only load path:** the drag-drop fallback was removed (SV-NO-LOCAL-UPLOAD). `src/parser.js` stays — it parses backend-fetched transcripts and prices them from `src/pricing.json`.
