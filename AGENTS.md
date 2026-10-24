# claudit

Overview: [`README.md`](README.md). Process, setup detail, the CI workflow
catalogue and the module map: [`CONTRIBUTING.md`](CONTRIBUTING.md).
Invariants are the numbered SV-* rules in [`.claude/rules/`](.claude/rules/)
(core file plus path-scoped domain files); this brief points at them instead
of restating them.

## Project overview

**claudit** (Claude Code Usage Dashboard) is a self-hosted web app that
ingests AI coding-session JSONL transcripts (Claude Code, Codex, Kimi) from
Cloudflare R2 (or a local `file://` mirror), parses them into Postgres, and
serves dashboards and raw transcripts to a React frontend rendered via
in-browser Babel (no npm/build step). It exposes statistics only: no
session-quality analysis, coaching or "auditor", deliberately
([Scope](README.md#scope)).

Panels: README.md. Lines Added/Deleted is per-call churn from tool-call
arguments, stored on `tool_uses`: Edit/Write plus the Bash shapes whose line
counts are readable off the command text (SV-BASH-CHURN).

## Technology stack

- Backend: Python 3.13+, FastAPI, Uvicorn, psycopg3 (pooled), APScheduler
  (BackgroundScheduler, hourly ingest), orjson.
- Frontend: vendored React 18, in-browser Babel, vanilla JS/JSX; no webpack,
  vite or npm install. The root `package.json` is the **CI toolchain only**
  (eslint, eslint-plugin-react, AST coverage, globals; devDependencies,
  nothing served), so Dependabot sees the eslint gate's pin.
- Database: PostgreSQL, `claudit` for app data plus an external auth DB for
  user credentials.
- Object storage: Cloudflare R2 via the S3 API, or a local `file://` mirror.
- Testing: pytest, pytest-asyncio, httpx2 (TestClient). Deployment: systemd
  (`examples/claudit.service`).

React, ReactDOM and Babel are pinned builds in `public/vendor/`, served from
`/vendor/` via SRI-hashed tags in `public/index.html`, comments and
source-map references stripped; upstream notices in
`public/vendor/LICENSES.txt`. Google Fonts still loads off-site (README:
Client-side internet requirement).

## Project structure

Per-file detail: [Module map](CONTRIBUTING.md#module-map).

- `backend/` (FastAPI): `app.py` startup, routes, static serving and
  index.html rewriting; `api*.py` endpoints; `parse*.py`, `codex_fork.py`,
  `kimi_content.py`, `prompt_gate.py`, `reply_timing.py`, `turn_flags.py`,
  `bash_*.py` the parsers (SV-PARSER-SPEC, SV-BASH-CHURN,
  SV-CONTEXT-INTAKE); `ingest*.py` R2 walk, persistence, rollups, reprice; `pricing.py` (logic only), `pricing_load.py`,
  `long_context.py`, `rate_*.py` rates (SV-RATE-DATA, SV-PROVIDER-RATES);
  `auth.py`, `login*.py`, `session.py` auth; `db.py` `viz_pool` and
  `auth_pool` (auth DB, READ-ONLY; pools never join across DBs);
  `schema*.sql` applied at every startup (SV-SCHEMA-AUTOAPPLY); `branding.py`, `login_page.py` (SV-BRAND-ESCAPE);
  `tool_errors.py` (SV-WHY-COLUMNS); `project_aliases.py` (SV-PROJECT-ALIASES).
- `public/`: `index.html` (rewritten per request to inject
  `window.BACKEND_URL`, `window.IS_GUEST` and mtime `?v=` cache-busts),
  `app.css`, `vendor/`.
- `src/` (served at `/src/*`): `app.jsx` shell; `parser.js`,
  `parser-lanes.js`, `parser-codex.js`, `ctx-input.js`, `record-dedup.js`,
  `rates.js` the browser mirrors of the backend parsers and rate resolution
  (SV-PARSER-SPEC lockstep, SV-CANONICAL-FLAG); `pricing.json` every rate
  table (SV-RATE-DATA); `dashboard-*`, `views/` and the other `.jsx` files
  the panels.
- `scripts/plots/` DB-backed usage plot; `scripts/ci/` CI entry points;
  `tests/` pytest (conftest.py forces file-mode R2 and test-safe env
  defaults); `fixtures/` parser/, codex/, r2_mini/ (SV-FIXTURE-SIZE);
  `examples/claudit.service`; `.claude/rules/` the SV-* doctrine.

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

cp backend/.env.example .env              # edit: CLAUDIT_DATABASE_URL_VIZ, CLAUDIT_DATABASE_URL_AUTH, CLAUDIT_R2_*, CLAUDIT_ADMIN_TOKEN
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt

python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Runtime collection and feature switches: [Configuration](README.md#configuration); stored-field
inventory: [Privacy: collection switch inventory](PRIVACY.md#collection-switch-inventory).

The first request may block on the startup ingest (~30 s warm, minutes cold
against a large bucket); `/health` reflects ingest state via `ingest_runs`.
Without R2 credentials, point `CLAUDIT_R2_ENDPOINT` at `fixtures/r2_mini/`.

```bash
# Out-of-band ingest; admin POSTs need a same-origin Origin header.
curl -X POST http://127.0.0.1:8000/admin/ingest \
  -H "X-Admin-Token: $CLAUDIT_ADMIN_TOKEN" \
  -H "Origin: http://127.0.0.1:8000"
```

A service restart also kicks an ingest (`systemctl restart|status claudit`,
`journalctl -u claudit -f`).

## Code style

Authoritative in CONTRIBUTING.md (House style): `from __future__ import
annotations` and type hints; raw parameterised (`%s`) psycopg3 SQL, never
interpolated, no ORM; ES2020-ish functional React with `window.` globals;
`snake_case` / `camelCase` / singular SQL table names; style opinions off.
Parsers skip malformed JSON lines silently (`orjson.JSONDecodeError` →
`continue`); ingest catches broad exceptions, logs to `ingest_runs.error`,
never crashes the scheduler.

## Testing

Parser tests (`tests/test_parse.py`) are fixture-driven: add a fixture to
`fixtures/parser/` BEFORE changing parser behaviour, test name 1:1 with the
feature (SV-FIXTURE-SIZE). API tests mount only `api.router` (auth bypassed)
against a fresh temporary DB + mini R2 mirror. Portable subset
(`-m "not db"`) and db-marker mechanics: CONTRIBUTING.md. Tests never pin
repository-managed data (SV-TEST-DATA).

## Security considerations

- **Auth**: PBKDF2-SHA256, per-user hex salts. A stored hash is a bare hex
  digest (legacy, verified at 200,000 iterations) or
  `pbkdf2_sha256$<iterations>$<salt>$<hash>`; new writes use 600,000.
  `web_password_salt` must stay populated (the verifier gates on it), so
  external user management can issue credentials. Cookies: HMAC-signed,
  `HttpOnly`, `Secure` (`CLAUDIT_COOKIE_SECURE`), `SameSite=strict`, 7-day
  TTL.
- **Login**: every credential failure (unknown id, no password, wrong
  password) returns the same generic 401 body and costs about one PBKDF2 run
  at the write count, so ids cannot be enumerated by response or timing
  (residual: a hash versioned above that count costs longer). Rate limits: 5
  failures per IP+user and 20 per IP, per 5-minute window. Both limiters
  prune on access; history tables and in-flight reservation maps cap at
  4,096 keys (expired swept first, then unseen keys refused); in-flight keys
  reserve history capacity and live windows are never evicted.
- **Guest mode**: `user_id=0` sessions use a per-process secret, so cookies
  die on restart. Guests are blocked from `/api/projects`, `/api/sessions*`
  and `?project=`.
- **Server-side logout, credential-bound sessions**: each user's session
  secret, `generation` and credential fingerprint live in claudit's
  `user_session` table. A token needs a matching generation, and the auth-DB
  user must still exist with the hash and salt of the last login (cached
  ≤ 60 s, so deletion or a password change revokes within that). Rows
  without a fingerprint are invalid until the next login. `GET /logout`
  bumps the generation.
- **No auth-DB writes**: the app only READS `users.config`.
- **Admin**: `POST /admin/ingest` checks `X-Admin-Token` with
  `hmac.compare_digest`.
- **Same-origin**: every mutating route, `/login` and `/login/guest`
  included, requires the Origin or Referer host to match Host (no Host →
  refused). `GET /logout` is exempt and relies on `SameSite=strict`.
- **R2 file-mode traversal**: `_safe_join` in `backend/r2.py` refuses keys
  escaping the root via `os.path.realpath`.
- **No local upload path** (SV-NO-LOCAL-UPLOAD).

## Deployment process

Runs under systemd behind a reverse proxy
([`examples/claudit.service`](examples/claudit.service) holds the unit's
shutdown and restart settings). A restart mid-ingest aborts the run
cooperatively (`ingest_runs` says "aborted"); the next successful run
rebuilds derived state. Schema applies at every startup
(SV-SCHEMA-AUTOAPPLY), so a restart suffices after editing `schema.sql`.
Version bumps ship with their cause (SV-PARSER-VERSION, SV-REPRICE).
`backend/constants.VERSION` reads the tree `VERSION`; `/health` reports it.

## CI — batch your pushes

- **Push a batch of commits once, not one at a time**: only the tip's run
  tells you anything.
- `ci-gate.yml` owns the push/PR surface and folds its gate legs into
  **`ci gate / aggregate`**; per-workflow catalogue, the registry-image
  cache and the Postgres composite: CONTRIBUTING.md. Documentation-only
  changes (`*.md`, `PRESENTATION.txt`, `examples/`, `.claude/`, licences)
  skip the expensive legs by classification and still pass.
- Deliberate shapes, not drift: the tests canary runs the suite-cost bench
  and gates it against committed instruction budgets
  (`scripts/ci/suite_bench.py`), and `refresh-pricing.yml` keeps its
  dependency installs uncached despite the pip-cache rule.
- **Releases are cut by editing the root `VERSION`** (one semver line, no
  `v`). Between releases it carries the next version with `-dev`; a release
  drops the suffix (`release.yml` waits for every other check, then tags),
  and right after, `VERSION` is bumped by hand to the next `-dev` (patch vs
  minor is a judgement). `version-guard.yml` refuses a `VERSION` naming a
  published release.
- **Actions are hash-pinned** with the version in a trailing comment; never
  "tidy" back to `@v4`. Every workflow sets `permissions:` explicitly;
  `zizmor` enforces that and `persist-credentials: false`. A zizmor
  suppression goes at the offending line with a justification, never as a
  raised `--min-severity`.
- **`.gitignore` is deny-by-default**: `*` first, then each shipped path
  named back. A new file of an unlisted type is invisible to `git status`;
  `git check-ignore -v <path>` names the hiding rule, and the fix is a
  name-back rule in the file's directory block, never loosening `*`.
- **Coverage and file size are self-raising ratchets** (SV-CI-RATCHETS).

## Development conventions

- **Base a new panel on the closest existing one**: existing panels encode
  non-obvious fixes. Cost by Context is bars plus a cumulative line, as
  `TimeSeriesPanel`'s **Cost (USD)** (`src/dashboard-charts.jsx`) already is:
  - bars are a dim FIELD (`fillOpacity` 0.3, 0.85 on hover), leaving room
    for a line over them;
  - the cumulative line is the SAME hue, made legible by a white halo
    (`stroke="#fff" strokeOpacity="0.15" strokeWidth="4"`) drawn under it;
  - hover lives on the container (the tooltip's `offsetParent`), guarded to
    the plot area;
  - axes are named by rotated captions, not a legend.

  `tests/test_panel_wiring.py` pins these at source level on purpose: node
  cannot parse JSX and nothing renders React, so a panel can pass the suite
  and still draw a black rectangle.
- **`records` carries `stop_reason`, `effort`, `thinking_tokens`,
  `cli_version`, `turn_flags` and `turn_tool_results`** beside the token
  columns. `stop_reason` comes only from a reply's closing line, so NULL
  marks a reply whose closing usage never arrived; use `text_chars / 4` for
  its `output_tokens`. `turn_flags` are the events in the window before a
  request plus the `prompt_snapshot` diffs, backfilled onto the request
  BEFORE the snapshot line (the vocabulary is `backend/turn_flags.py`'s
  `FLAG_*` constants); attribute a prompt-cache miss with a GROUP BY over
  them instead of re-reading the raw file.
- **Rollup grains** (SV-ROLLUP): `usage_rollup` (session × hour × model ×
  provider × is_main × long_context), `tool_rollup` (hour × project × model
  × tool), `tool_error_rollup` (+ error_kind), `dispatch_rollup` (hour ×
  project × agent_type × agent_model), `dispatch_brief_rollup` (hour ×
  project × agent_type × brief_ref, with summed prompt length),
  `ctx_cost_rollup` and `agent_rollup` (both carry `total_tokens` beside
  `cost_usd`), and `latency_rollup`.
- **`latency_rollup` is stored once per display bucket width**
  (`constants.LATENCY_BUCKETS`, epoch-aligned and few: percentiles do not
  compose), plus an all-projects row (`project_id = ''`): a project filter
  changes each group's population, so an all-projects p50 is not derivable
  from per-project ones.
- **A record above ITS model's long-context threshold bills the whole record
  on the meter**: the threshold is per-model data (pricing.json's
  `long_context_meters`, defaulting to the Codex meter's 272k), a
  Claude-format row of a meter member carries the flag too, and the flag
  persists on `records.long_context` (SV-DATED-RATES).
- **The rest is doctrine**; read the rule before touching its area: cost
  split (SV-COST-SPLIT), canonical dedup (SV-CANONICAL-FLAG), suppression
  (SV-SUPPRESSED-MODELS), incremental rollups (SV-ROLLUP), context intake
  stays psql-only (SV-CONTEXT-INTAKE), subset tokens (SV-SUBSET-TOKENS),
  version bumps (SV-PARSER-VERSION, SV-REPRICE), multi-bucket identity
  (SV-FILES-RECORDS), parser lockstep and self-containment (SV-PARSER-SPEC,
  SV-READ-ONLY-CANONICAL).
