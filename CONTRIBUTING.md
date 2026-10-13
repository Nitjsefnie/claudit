# Contributing to claudit

Issues and pull requests are welcome — especially if your numbers disagree
with ours. This project is a cost-accounting tool, so a report that says
"your figure is wrong and here is the arithmetic" is the most valuable
thing you can send.

## LLM and agent contributions are welcome

You may use an LLM or a coding agent to write your contribution. There is
no penalty, no separate review queue, and no expectation that you rewrite
its output by hand. Much of this repo was built that way.

Two conditions, and they are about honesty rather than provenance:

1. **Disclose the model** with a trailer on each commit it authored:

   ```
   Co-Authored-By: <Model Name> <noreply@example.com>
   ```

   e.g. `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. One
   primary-author trailer per commit.

2. **Do not submit claims you have not verified.** This matters more here
   than in most repos, because plausible-looking cost arithmetic is very
   easy to generate and very hard to spot as wrong. If your PR says a
   change makes ingest faster, or fixes a miscount, paste the command and
   its real output. "Tests pass" without the run is not evidence.

If a maintainer's reply reads like it was drafted by an agent, it probably
was. That is fine in both directions.

### If you are an agent reading this

Read [`AGENTS.md`](AGENTS.md) first — it is the architecture and
conventions brief, written for you. Then read
[`.claude/rules/`](.claude/rules/),
which holds the invariants as numbered rules (`SV-*`). The `AGENTS.md` brief
and the `.claude/rules/` directory are authoritative; this one only covers
process.

The rules that reject the most patches, in order:

| Rule | What it forbids |
|---|---|
| `SV-COST-SPLIT` | Pricing a cache write at a single rate. 5m is 1.25x input, 1h is 2x. |
| `SV-PARSER-SPEC` | Changing rate resolution in `backend/pricing.py` without mirroring `src/parser.js` (or vice versa). |
| `SV-RATE-DATA` | A rate anywhere but `src/pricing.json`, or rewriting an entry of its history instead of appending one. |
| `SV-RATE-REFRESH` | A sampled provider-rate move dated anywhere but detection time, a log-backed move dated anywhere but OpenRouter's change point, editing or deleting a provider-rate entry outside the one-time human-reviewed backfill, taking one of a host's two in-region prices without a resolution recorded as data (a tag, or the cheaper of otherwise identical twins), or pinning a price value. |
| `SV-DATED-RATES` | Pricing a record at "now" instead of the record's own timestamp. |
| `SV-NO-LOCAL-UPLOAD` | Re-adding a file picker, drag-drop, or an upload endpoint. It was removed deliberately. |
| `SV-FIXTURE-SIZE` | Committing large fixtures. Parser fixtures stay under 1 KB. |

Do not "helpfully" add a bundler, an npm dependency, or a build step. The
frontend transpiles in the browser on purpose; that is a design decision,
not an oversight. The root `package.json` is the **CI toolchain only** —
eslint, eslint-plugin-react, c8, `globals`, devDependencies and nothing
else, installed with `npm ci` and read by nothing the app serves. Adding a
runtime `dependencies` block there is the creep this paragraph forbids.

**Scope.** claudit exposes usage *statistics* — it does not analyse session
quality, review "what went wrong", or run an AI auditor/chatbot over your
transcripts (see [Scope](README.md#scope) in the README). A PR that adds that
kind of qualitative-analysis feature will be declined regardless of how well
it is built; it is a different product. Numbers in, numbers out.

## Getting it running

Requires **Python 3.13+** and a local **PostgreSQL** you can create
databases in.

```bash
createdb claudit
psql claudit -f backend/schema.sql

# The auth DB is external — this repo owns no schema for it. Startup
# aborts on db.schema_check() unless users exists and carries the two
# columns the login lookup reads (user_id integer, config JSONB).
createdb claudit_auth
psql claudit_auth -c "CREATE TABLE users (user_id BIGINT PRIMARY KEY, \
config JSONB NOT NULL DEFAULT '{}'::jsonb)"

cp backend/.env.example .env      # then edit: CLAUDIT_DATABASE_URL_VIZ, CLAUDIT_DATABASE_URL_AUTH, CLAUDIT_R2_*, CLAUDIT_ADMIN_TOKEN
                                  # CLAUDIT_DATABASE_URL_AUTH=postgresql:///claudit_auth (the .env.example default,
                                  # postgresql:///users, names a database this block does not create)
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt

python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

`scripts/ci/smoke.py` builds the same minimal `users` table for its own
fixture; see the README's [Quick start](README.md#quick-start) for what
populates it and why it stays out of this repository.

No R2 credentials? Point `CLAUDIT_R2_ENDPOINT` at a directory instead — the
client walks the tree in `file://` mode:

```bash
CLAUDIT_R2_ENDPOINT=file:///path/to/transcripts/
```

`fixtures/r2_mini/` is a tiny mirror you can point at to get a working
instance in seconds.

## Running with Docker

Prerequisite: **Docker Engine with the Compose plugin** — the
`docker compose` command (Compose V2 or later), not the legacy standalone
`docker-compose`. No Python, no local PostgreSQL, and no `.env` — the stack
boots from a clean checkout:

```bash
docker compose up
```

That builds the root `Dockerfile` and starts two services:

- **db** — a digest-pinned PostgreSQL 16 (the same image CI uses; a
  local `docker compose up` pulls it directly). On its first boot, against an
  empty data directory,
  `compose/postgres-init.sql` creates the `claudit` application database and
  the external `claudit_auth` database with its two-column `users` table —
  the floor `db.schema_check()` demands, spelled out in
  [Getting it running](#getting-it-running) above. The application's own
  tables are **not** created there: schema applies automatically at
  startup (`db.apply_schema()`, SV-SCHEMA-AUTOAPPLY), so a restart after
  editing `backend/schema*.sql` is enough.
- **app** — the image, waiting until db reports healthy.

`/health` answers at **<http://localhost:8000/health>** as soon as the app
has applied the schema and can reach Postgres; the body carries `ok`, the
build `version`, and the startup ingest's progress behind it. `docker
compose ps` shows both services' health.

Both published ports are overridable, so a box already running Postgres or
a meter composes up without colliding:

```bash
POSTGRES_HOST_PORT=15432 CLAUDIT_HOST_PORT=18000 docker compose up -d
```

In-container `/api/export` needs `CLAUDIT_EXPORT_PYTHON` pointed at an interpreter with
the renderer's dependencies (e.g. a derived image), while the rest of the
service runs stock.

**Teardown:** `docker compose down -v` — the `-v` drops the named `pgdata`
volume along with the containers and the network, so the next
`docker compose up` starts from empty databases and re-runs the first-boot
init script. A plain `docker compose down` keeps the volume, and the
databases (and the data already ingested into them) are still there next
time.

**Storage defaults to the bundled fixtures.** `CLAUDIT_R2_ENDPOINT` points at the
repository's `fixtures/r2_mini/`, mounted read-only into the container at
`/opt/claudit/fixtures`, so a fresh stack ingests a working sample with no
credentials and makes no outbound calls at run time — the same "no
credentials, no network, nothing to rot" default `smoke.yml` documents.
For a real bucket, override the R2 variables through a `.env` file in the
repository root — the file Compose reads for the `${VAR:-default}` defaults
in `compose.yaml`, and the same file the bare-metal route seeds from
`backend/.env.example`:

```bash
CLAUDIT_R2_ENDPOINT=https://<account>.r2.cloudflarestorage.com
CLAUDIT_R2_BUCKET=claude
CLAUDIT_R2_ACCESS_KEY_ID=...
CLAUDIT_R2_SECRET_ACCESS_KEY=...
```

Note that a `.env` written for the bare-metal route is inherited by Compose
as-is: its socket DSNs (`CLAUDIT_DATABASE_URL_VIZ=postgresql:///claudit`) and host
paths (`CLAUDIT_R2_ENDPOINT=file:///path/to/r2/`) do not resolve inside a
container. Delete a variable to fall back to the compose default, or give
it a container-reachable value — `CLAUDIT_DATABASE_URL_VIZ`/`CLAUDIT_DATABASE_URL_AUTH` at
host `db`, port 5432, and `CLAUDIT_R2_ENDPOINT` at a path inside the container.
An exported shell variable beats `.env` in Compose's precedence, so a
leftover `CLAUDIT_DATABASE_URL_VIZ` in your shell overrides the file too.

**The browser still needs the internet.** React, ReactDOM and Babel load
from unpkg.com at run time in the page (README:
[Client-side internet requirement](README.md#client-side-internet-requirement));
containerising the server does not make the dashboard offline-capable.

## Tests

```bash
python3 -m pytest tests/ -q             # full suite
python3 -m pytest tests/test_pricing.py -v
python3 -m pytest tests/ -q -m "not db" # portable subset — no database needed
```

The portable subset needs no database, but a handful of its tests read
the tree's own git metadata (the module-size baseline and Dependabot
coverage counts discover files with `git ls-files`; the thresholds
document's byte-canonicality pin reads `HEAD` via `git cat-file`; the
`.gitignore`/VERSION pin runs `git check-ignore`). From a plain source
archive — a tree with no git metadata of its own, including one
unpacked inside another git checkout — those tests **skip** with a
reason naming that; run them from a git checkout for full coverage.

The `db` marker splits the suite for CI's portability matrix (Linux,
macOS and Windows × Python 3.13/3.14 run everything that needs no
PostgreSQL). It is applied mechanically, derived from fixture usage —
a test whose fixture closure reaches a fixture registered in
`tests/db_marker.py` is marked for you — and the few tests that reach
the server without any DB fixture carry `@pytest.mark.db` explicitly;
`tests/test_db_marker.py` holds both halves in place against the source.

CI runs **twenty-three** workflows. `ci-gate.yml` owns the push/PR
surface: it starts on every push to `master` and every pull request
against it (a pull request's commits are checked once, with no review
gate first), classifies the changed paths, runs the eleven gate
workflows as reusable legs, and folds them into one verdict —
**`ci gate / aggregate`**. A documentation-only change (`*.md`,
`PRESENTATION.txt`, `examples/`, `.claude/`, licences) narrows the
expensive legs by classification instead of by `paths-ignore`, so
every check still reports rather than going MISSING — except a change
to `README.md`, `CONTRIBUTING.md` or `AGENTS.md`, which runs every leg
whatever else it touches: `tests/test_docs_setup.py` pins those three
setup blocks, so the tests leg is the one that would fail a docs-only
break of them (issue #477). A branch without
a PR is checked by dispatching the gate on it (`gh workflow run
ci-gate.yml --ref <branch>`). A green suite is a small fraction of
the gate. These five you can and should run locally
before pushing:

```bash
python3 -m pytest tests/ -q --cov=backend            # tests (+ coverage)
git ls-files -co --exclude-standard '*.py' | xargs pylint      # lint, gate 1
git ls-files -co --exclude-standard '*.py' | xargs pycodestyle # lint, gate 2
pyright                                                        # types
npx --no-install eslint 'src/**/*.js' 'src/**/*.jsx'           # eslint
python3 scripts/ci/smoke.py                                    # smoke
```

The eslint line needs the node toolchain installed first — `npm ci`, the
same command both JS gates run. It installs `package.json`'s pinned
devDependencies (and only those) from `package-lock.json`; nothing it
puts in `node_modules` is served, built or shipped.

Use `-co --exclude-standard`, not a bare `git ls-files`. CI lints the
committed tree so its own plain `git ls-files` is right *there*; locally
it is a trap, because a brand-new module is untracked until you stage it
and pylint will report a clean run over every file except the one you
just wrote.

`pip install -r requirements-dev.txt -r requirements-test.txt` gets the
pinned toolchain. Coverage is a self-raising ratchet, not a target, and
it covers both shipped source families. The python measurement is the
full pytest run (`--cov=backend`); the javascript measurement runs the
node-executing tests under `NODE_V8_COVERAGE` and folds the result with
c8 over `src/**/*.js` — the files node actually executes. The `.jsx`
panels sit outside that boundary (node parses no JSX, and the tests
exercise them only through eval'd fragments, which V8 attributes to the
eval, not the source file), so extending coverage to them means a real
browser toolchain, not a wider flag. Both measured values and their
floors (always 1.5 below) are committed data in
`.github/ci-thresholds.json`, and CI raises each floor on master when a
run measures more than 1.5 above its recorded value. The same file holds
the reparse hot path's budget (`scripts/ci/reparse_bench.py`): one
record per phase of a parse pass, each phase carrying the share of that
run's own CPU work AND the bytecodes it retires per file, both recorded
as costs with their floors the ceilings 1.5 above the recorded values.
Only the bytecodes GATE (issue #513): a share is a proportion of a timed
run rather than a count of work, its runner spread measures wider than
that 1.5-point gap, and it moves when the corpus mix shifts between
formats of different parse cost even with no code path slower and every
count under its own ceiling — so it is printed and recorded as telemetry,
compared against by nothing, and the ratchet tightens only the counts,
one phase at a time, and only when a phase got cheaper. That is the
inversion the coverage family's `floor = measured - 1.5` does not have,
because higher coverage is better and a lower CPU cost is. The same file caps
every Python file's line count and every tracked `src/**/*.js(x)` file
too (production 500 / test 700): files over the cap carry baseline
entries that CI lowers as they shrink, and entries are never added or
raised by hand — grow a file by moving code into a new module instead.

The comment-share baseline (`comment_share_baseline`) holds exact
`[comment bytes, non-whitespace bytes]` pairs for every tracked Python,
JS/JSX/MJS/CJS, YAML and shell source above 15% (extensionless shell
scripts included). Comparisons use integer cross-products; display rounding
never affects the gate. `scripts/ci/comment_baseline.py` fails on a rise
or an unrecorded file above 15%. `--matches-tree` also requires the exact
measured pairs; `--tighten` lowers entries and drops files at or below 15%.
The introducing change uses `--seed` once, through `thresholds.write`;
an established entry can never rise or be added. The tests canary installs
node/espree from the lockfile and PyYAML from `requirements-test.txt`, then
runs the check after the JS toolchain install, including on comment-only changes. Docs-only
changes skip it only when the classifier finds no measured source,
including source under `examples/` or `.claude/` and extensionless shell
files identified by their checked-out shebang. Bot-data-only refreshes
require an exact pricing path set and a constants diff consisting solely
of one removed and one added `PRICING_VERSION` assignment that differ
only in the numeric digit run inside its string literal; every other byte,
including the line terminator, must match. Other
constants changes and unreadable files run the full gate set.
Master tightens with the other baselines; `ratchet-push` publishes the
measured thresholds artifact without running the measurement again.

Beside that tree-level ratchet, a pull request also gets an
**informational patch-coverage readout** — which of the lines the branch
added the suite executed (`scripts/ci/diff_coverage.py`), written to the
run summary and posted as one self-updating PR comment. It is never a
gate and never a required check: a reviewer judges the number, and a
comment that fails to post cannot redden the run. The `tests` leg
computes the readout and uploads it; the top-level `coverage-comment`
workflow posts it, because that leg checks out and RUNS the proposed
tree and so must hold no token that can write (issue #560 — which is also
why a fork pull request, whose token is read-only inside the leg, now
gets the comment at all). Every CI job also
declares `timeout-minutes`, so a hung step loses its runner in minutes
rather than at GitHub's 360-minute default.

Run these against an environment with the **pinned** runtime deps
installed too (`pip install -r backend/requirements.txt`). `pyright`
resolves third-party types from the installed packages, so a stale local
psycopg makes it disagree with CI — that is how a psycopg 3.3 typing
change once passed locally and turned `types` red on the push. If you
keep a separate venv, point pyright at it: `pyright --pythonpath
/path/to/venv/bin/python`.

The rest need GitHub and run on their own:

| Workflow | What it does |
| --- | --- |
| `ci-gate` | The aggregate gate. Starts on every push to `master` and pull request, classifies the changed paths (docs-only changes skip the expensive legs — except a change to README.md, CONTRIBUTING.md or AGENTS.md, which runs every leg so the setup-docs pin fails on the same push; every check still reports), runs the eleven gate workflows as reusable legs, and folds them into one `ci gate / aggregate` verdict. The aggregate job also carries the two gate-integrity checks on every run, docs-only changes included: the merge-conflict-marker scan over the tracked tree, and `scripts/ci/commit_scopes.py`, which refuses a commit whose scope names a workflow under a non-`ci` type (see [Commit subjects](#commit-subjects)). Master-push runs are grouped per commit SHA, so a newer push never cancels an older run (issue #358); a deliberate cancel reads never-green. |
| `tests` | The `ci-gate` pytest leg runs the full suite with coverage, gates and ratchets the reparse hot path's per-phase bytecode counts — the per-phase CPU shares ride along as telemetry, not a gate (`.github/actions/reparse-bench`, `scripts/ci/reparse_bench.py`; the bench itself is documented in its own docstring) — measures ONE pytest pass over a pinned test-file fixture in bytecode instructions and fails the canary job if any phase exceeds its committed ceiling in `.github/ci-thresholds.json` (`.github/actions/suite-bench`, `scripts/ci/suite_bench.py`; a ratchet-down-only budget the tighten bot lowers when a cheaper run justifies it, never a wall-clock number: runner speed varies ~2x between jobs, but an instruction count is exact), and raises the committed coverage floors or tightens file-size, comment-share and pylint-suppression baselines when the measurements justify it. The `Threshold direction guard` step also compares `.github/ci-thresholds.json` against the base document and fails a PR or master push that lowers a coverage value, raises an entry, or hand-adds one under the frozen core families (issue #388), so the never-raise/never-lower rule does not depend on review; the bots' raise/tighten and the two sanctioned seeds (a new member, a new measured family) stay legal. The ratchet data (the measured thresholds file and the suite-cost measurement) is staged and uploaded here; the push lives in `ratchet-push.yml`, a top-level `workflow_run` workflow keyed on the ci-gate run's completion (issue #479: the `master-push` deploy key's secret resolves empty inside a `workflow_call` callee, and a `GITHUB_TOKEN` push is rejected by the ruleset's required `aggregate` check). |
| `ratchet-push` | The data-only push half of the ratchets (issue #479): fires on the ci-gate run's completion, and only from a green master push whose tests leg staged data. Its job takes the deploy key from the `master-push` GitHub environment, lists the triggering run's artifacts, downloads the measured thresholds file and suite-cost measurement when present, tightens the suite-cost budgets from the measurement (a downward-only data operation; never raises), and pushes the ratchet commit. It runs no test, dependency or upstream code and stops its ssh-agent after the push. A push refused because master moved is dropped with a notice — the next master run measures and raises instead. Like the pricing bot's deploy-key push, the ratchet commit starts workflow runs, but a `.github/ci-thresholds.json`-only change stays silent in ci-gate by its paths-ignore. |
| `coverage-comment` | Posts the informational patch-coverage comment (issue #560). Fires on the ci-gate run's completion and only for a pull-request run, because `tests` is a `workflow_call` callee and produces no run of its own. It is the write-capable half of a boundary the `tests` leg cannot cross: that leg checks out and RUNS the proposed tree, so it holds `contents: read` and nothing more — before issue #560 it held `pull-requests: write` for the comment itself, which on a fork is read-only anyway, so no fork pull request ever saw the readout. Only the RENDERED BODY crosses, as an artifact. Every artifact byte is untrusted: the destination pull request is resolved from the `workflow_run` event's own `pull_requests`, the artifact's claim is only compared with it (a mismatch fails the step loudly), the body must be an ordinary file, is size-capped and refused rather than truncated, and reaches GitHub as a file argument — never executed, never shell-interpolated. No `actions/checkout`, no pull-request code, no `checks: write`; it publishes no check run, joins no aggregate, and is not a gate. A run without the artifact refreshes the marker so a previous commit's percentage cannot look current. If GitHub ever hands back an empty `pull_requests` array, the number is resolved from the base repository by merge-ref lookup instead, and the run posts **nothing** rather than posting a number it cannot prove is current. |
| `test-data` | Would a refresh-shaped change — moved rates, bumped version constants — break the suite? Perturbs the tree exactly that way (`scripts/ci/perturb_test_data.py`) and runs the full suite against it on the cached Postgres 16 container; no coverage, no ratchet — the verdict is the perturbed suite's pass/fail. Runs on three fixed seeds so the verdict is deterministic (the appended `from` stamps are decoupled from the seed, so any seed is safe) — one matrix job per seed, in parallel (issue #456), so the leg's wall is a single suite run. A ci-gate leg, skipped on docs-only changes that touch none of the three setup docs. The second half of SV-TEST-DATA's enforcement. |
| `test-data-explore` | The daily explorer for `test-data`: draws three wall-clock seeds and runs the same full perturbed suite per seed, naming any failing seed in the run summary. Not a ci-gate leg — a red run means "look at this seed", never "block". |
| `gate-freshness` | Publishes the `gate freshness` check run on every open PR head, on each master push (all heads) and each `pull_request_target` event (the event's own head, filtered to the base it protects): red iff master holds a commit the head lacks whose changed paths hit the gate trigger set — the workflows, local actions and CI scripts, the ratchet data, the scanner/lint/type configs, the requirements files, the docker-smoke Compose override. A `.github/ci-thresholds.json` tighten triggers this workflow although the gate suites ignore it. Unreadable per-head compares publish red; a global read failure publishes nothing and exits nonzero. Advisory until the `require-ci-aggregate` ruleset requires the name `gate freshness`. |
| `codeql` | Security analysis for Python and JS; findings go to the Security tab, not the build. Also runs weekly as a standalone cron, because new queries only ever see code that changed after they shipped. |
| `scorecard` | OpenSSF Scorecard: measures the repository's supply-chain posture (branch protection, signed releases, pinned actions, review history) and uploads the SARIF to code scanning. Weekly on Saturday, and on manual dispatch. It is a **trend signal, not a gate** — it runs on no contribution event, so it is never a second verdict on a commit `ci-gate` already judges, and a red scorecard never blocks a release or a merge. Publishing (`publish_results`, over OIDC) happens only on the upstream default branch: a fork's run files into the fork's store, another branch's under this repository's name. The write permissions it needs (`security-events`, `id-token`) sit on the job, never on the workflow, because upstream refuses to publish from a workflow that declares write permissions above the job. |
| `audit` | `pip-audit` over every requirements file, resolving the full transitive tree. Also runs daily as a standalone cron — an advisory lands without a commit here to hang it on. |
| `panel-layout` | The rendered-layout gate (issue #631). Drives the real page in headless Chromium against the frozen payload set in `fixtures/layout/` at three viewport widths, and reads back every panel region's `getBoundingClientRect()`: each must sit inside its own panel, and no two may intersect. It exists because nothing else in the suite renders the dashboard, which is how issue #630's legend-over-chart reached a user with every other check green. One generic check over every panel, not a suite per panel; no database and no bucket. The browser driver is a devDependency like every other CI tool here. |
| `docker-smoke` | The container leg (issue #941): builds the `Dockerfile` image and boots the compose stack (`compose.yaml` — digest-pinned Postgres with its first-boot init, the app on the `file://` fixture mirror) on a bare runner that carries no `CLAUDIT_R2_*` or legacy `R2_*` variables. Asserts the RESOLVED compose config is file-backed and credential-free before `up`, probes `/health` until the startup ingest finishes against the mounted mirror, and tears the stack down under `if: always()`. Both registry images come from the Actions cache (below): every compose call adds `.github/docker-smoke.compose.yaml`, which runs `db` from the cached Postgres tag with `pull_policy: never`, and the build finds the cached base in the local image store because nothing passes `--pull`. A ci-gate leg on every change that is neither docs-only nor bot-data-only. |
| `actionlint` | `actionlint` + `zizmor` over the workflow files themselves. A broken workflow does not go red, it silently stops running. Runs as a ci-gate leg on every non-docs change. |
| `release` | Cuts a tagged release when `VERSION` changes on `master`, after every other check on that commit has passed. A dev version (`X.Y.Z-dev`) skips everything — nothing is tagged. |
| `version-guard` | Fails a master push or PR whose tree `VERSION` names an already-published release (an existing `v<VERSION>` tag). Under the dev-suffix discipline (`0.4.0-dev` between releases) this only ever fires on a missed bump. The hourly pricing bot's own commits are exempt; PRs get no carve-out. |
| `refresh-pricing` | Hourly (and on dispatch): re-fetches OpenRouter's per-provider prices and change logs, and the first-party vendor table (the id and serving host of every catalog model under a configured `openrouter.vendor.prefixes` namespace) by vendor prefix. A uniquely joined host appends logged moves at their change points; other hosts are sampled at detection time with the alternation check. The run bumps `PRICING_VERSION` when it appends and commits to `master` after the suite passes. The one-time `backfill_provider_rates.py` rewrite is run separately after human review, as is `collapse_oscillating_rates.py`, which gives every oscillating (TOGGLE or BAND) provider row its `band` — the range the host moves inside, priced by the time-weighted mean over the window that formed it — so an in-range move appends nothing and commits nothing. The push is a separate key-only job that takes the deploy key from the `master-push` GitHub environment, reachable only from master: it receives the tested data (the two data files, the commit message and the tested base) as an artifact, runs no test/dependency/upstream code, and stops its ssh-agent after the push. Unlike a `GITHUB_TOKEN` push, this starts workflow runs intentionally, so the hourly pricing commit gets a real aggregate verdict on master's tip. There is no in-run retry: a push refused because master moved is dropped with a notice, and the next hourly run re-fetches on the new tip, re-tests in the keyless job, and lands the data there. A red run means a host needs a human decision, such as one listing a model at two prices; every other host's moves are still published. See SV-RATE-REFRESH. |
| `claim` | Lets a contributor without write access take an issue: comment `/claim` on an open, unassigned issue and the workflow assigns you; `/unclaim` and `/release` remove only your own assignment. Runs no repository code — it talks to the GitHub API only. See [Claiming an issue](#claiming-an-issue). |
| `pr-gate` | Checks every non-draft PR's description against `.github/PULL_REQUEST_TEMPLATE.md` and requires a reference to an issue assigned to the PR's author. A non-conforming PR gets a comment naming what is missing and is closed; the gate reopens it once the description is corrected. Never runs pull-request code. See [Pull requests](#pull-requests). |
| `secrets` | gitleaks sweeps the full tree and the full git history for credentials — a digest-pinned binary under the default ruleset, findings redacted in the public log (commit + path + rule, never the matched string). Runs daily, because a push carrying `[skip ci]` or a commit that predates the workflow is scanned the next morning rather than never. Complements `scripts/secrecy-check.sh`, the local literal-based check for this repo's own known-sensitive strings. |

CI never depends on an anonymous registry image pull succeeding at that
moment when the Actions cache has the image. One composite,
`.github/actions/cached-image/action.yml`, restores an image archive by an
exact key; on a miss it pulls from public ECR with three bounded attempts
and backoff, then tries the same reference on `mirror.gcr.io` with its own
three bounded attempts. Only a successful pull from either source is
tagged and saved. Concurrent saves may lose the race without failing the
job. A loaded archive carries a local tag, and callers use that tag, never
a registry reference.

The Postgres image's identity lives in `.github/actions/postgres-image/action.yml`:
the full pinned digest, the key (digest plus runner OS/architecture), the
`mirror.gcr.io/library/postgres` fallback at the same digest, and the local
tag `claudit-postgres:<digest>`. The five database workflows (`tests`,
`test-data`, `test-data-explore`, `smoke`, `refresh-pricing`) start
Postgres through `.github/actions/postgres/action.yml` after checkout,
which gets that image, starts the tag with `--pull never`, and waits for
TCP readiness; there are no job `services:` or separate wait steps in
these five workflows. Credentials remain `postgres` and the
published localhost port remains 5432. The hourly `refresh-pricing` job
runs only on master and restores even when rates are unchanged, warming
master's entry for PR reads and keeping it active within the cache's
seven-day unused-entry expiry. Its dependency installs still use no cache.
`docker-smoke` restores that same Postgres entry. It also caches the
Dockerfile's base — its `FROM` reference on public ECR, with
`mirror.gcr.io/library/python` at the same tag as fallback — saved under
the Dockerfile's exact reference. That base is tag-pinned, so its key adds
the ISO week (UTC): an entry is restored only in the week it was saved, so
the build never uses a base more than seven days old. The leg runs on pull
requests and master pushes alike, except for a change that is docs-only or
bot-data-only; the first master push in a week that runs it saves that
week's entry for PR reads, and a week with no such push leaves each PR to
pull and cache its own on its first run. A test keeps the workflow's base
reference equal to the Dockerfile's `FROM`. A cold cache needs access to at
least one registry; if both sources fail, the job reports the cache miss
and each registry's answers in the log.

Python dependency caches follow one rule: restore on any event, save only
from a push of `master`. `refresh-pricing` leaves its dependencies
uncached. The explicit `actions/cache/restore` / `actions/cache/save`
steps replace `cache: pip`, whose post-job save ran after the
pull-request checkout, so untrusted PR code would have written the
cross-run cache a later master run installs from.
`tests/test_workflow_pip_cache.py` pins the shape.

If you are changing dependencies, run `pip-audit -r backend/requirements.txt
-r requirements-dev.txt -r requirements-test.txt` too.

The suite creates and drops its own `claudit_test_run_*` databases, so it
needs a Postgres your user can `createdb` on. Every name carries a per-run
tag from `tests/scratch_db.py`, so two suites can share one server; a new
test module creates and drops its databases through that helper only (a
guard test fails otherwise). A run drops what it created even when tests
fail, and sweeps leftovers of killed runs once they are six hours old and
their run no longer holds its lease connection. It does not touch your real
data and never contacts R2.

Two tests are worth knowing about before you touch pricing:

- `tests/test_pricing_data.py` drives the real `src/parser.js` through
  `node` and asserts it and `backend/pricing.py` price every row of
  `src/pricing.json` exactly as the file says, either side of every
  cutover. Change one side's resolution logic only and this fails, by
  design. Skips its node cases if `node` is absent — do not take a skip
  as a pass.
- `tests/test_ingest.py::test_parallel_ingest_matches_sequential_exactly`
  ingests the same mirror at `CLAUDIT_INGEST_WORKERS=1` and `=8` and requires
  byte-identical output. If you touch ingest concurrency, this is the test
  that catches you. Its sibling in
  `tests/test_ingest_pipeline.py` pins the process-pool pipeline
  (issue #309) against the same bar: forkserver parse children plus persist
  threads must land exactly what the in-process pipeline lands, and
  `test_process_pool_persists_survive_a_per_file_failure` pins per-file
  failure isolation there.

## If you change how cost is computed

Which constant you bump depends on what changed. A cost-only change —
the rates in `src/pricing.json`, or how they are applied — bumps
`PRICING_VERSION`, the code constant in `backend/constants.py`, in the
same commit: the next ingest reprices stored `cost_usd` from each
record's own stored columns, with no reparse and no R2 fetch (see
SV-REPRICE). A change to parser semantics bumps `PARSER_VERSION`
instead, and every file reparses on the next ingest. Without the
matching bump, stored rows keep the old numbers and the dashboard
silently mixes them. Mention the bump in your PR so deployers know a
reprice or reparse is coming.

Rates live in `src/pricing.json`, which both `backend/pricing.py` and
`src/parser.js` read. Record a price change by appending an entry to the
row's history; never edit an existing one.

## House style

- **Python** — `from __future__ import annotations` at the top of every
  module. Type hints throughout. Raw SQL via psycopg3, no ORM.
- **SQL** — parameterised (`%s`) always. Never interpolate a value into a
  query string.
- **JS/JSX** — ES2020-ish, React function components, shared helpers hung
  on `window.`. No transpile step beyond in-browser Babel.
- **Naming** — `snake_case` in Python, `camelCase` in JS, singular SQL
  table names.
- Lint config lives in `.pylintrc`, `setup.cfg`, `pyrightconfig.json`
  and `eslint.config.mjs`, and CI enforces all four. Formatting opinions
  (line length in particular) are switched off on purpose — match the
  surrounding file for style, and treat anything the linters do flag
  as a real finding.

## Claiming an issue

You do not need write access to take an issue: comment exactly `/claim`
on an open, unassigned issue and the claim workflow assigns you.
`/unclaim` (or `/release`, the same command under two names) hands it
back, removing only your own assignment. The whole comment must be
exactly the command — `please /claim this` claims nothing — and a
carried issue number (`/claim #7`) must name the issue it is commented
on. Bot comments, pull requests and closed issues are ignored, and a
refused command is answered on the issue, not silently dropped.

### Commit subjects

Commits follow the familiar `type(scope): summary` shape. One rule of it
is gated: a workflow's `name:` may be a scope only with the `ci` type —
the type disambiguates shared names, `ci(tests)` changes the tests
workflow, while `fix(tests)` fixes the suite. `tests` is the one scope
that is both a workflow name and a subject of its own; a second shared
name needs its own sentence here before the check exempts it.
`scripts/ci/commit_scopes.py` refuses the violating shape in the
aggregate job on every pull request.

## Pull requests

Open the description from [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md):
an admission gate checks every non-draft PR's description against that
template and requires the PR to reference an issue assigned to its
author — claim the issue first (see [Claiming an issue](#claiming-an-issue)).
A non-conforming PR gets a comment naming what is missing and is
closed; once the description is corrected, the gate reopens it. The
gate never checks out or executes pull-request code, and an
agent-authored PR meets the same template.

Small and single-purpose beats large and comprehensive. In the
description, include:

- what changed and why,
- the actual output of the tests you ran,
- for a performance change, a before and after measurement rather than an
  assertion that it should be faster.

A bug report that pins down *where* the arithmetic goes wrong is worth as
much as a patch, and is often easier to review. If you are unsure whether
something is a bug or intended, open an issue and ask — a wrong premise
caught early is cheaper than a correct fix to the wrong problem.

Configuration uses `CLAUDIT_` names (SV-ENV-PREFIX). Unprefixed names shipped
in v0.4.0 remain deprecated fallbacks for one release; a prefixed value wins.
Rename old `.env` keys to avoid startup warnings. Unreleased settings such as
`GUEST_LOGIN` and the blob-cache settings have no fallback.
