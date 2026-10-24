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
and the `.claude/rules/` directory are authoritative; this one covers
process, plus the per-file [Module map](#module-map).

The rules that reject the most patches, in order:

| Rule | What it forbids |
|---|---|
| `SV-COST-SPLIT` | Pricing a cache write at a single rate. 5m is 1.25x input, 1h is 2x. |
| `SV-PARSER-SPEC` | Changing rate resolution in `backend/pricing.py` without mirroring `src/parser.js` (or vice versa). |
| `SV-RATE-DATA` | A rate anywhere but `src/pricing.json`, or a correction without a `PRICING_VERSION` bump. Log-backed histories are rewritten automatically; hand corrections require a human commit and bump. |
| `SV-RATE-REFRESH` | A sampled move dated anywhere but detection time, a log-backed history inconsistent with the complete listed-pricing log, taking one of a host's two in-region prices without a resolution recorded as data (a tag, or the cheaper of otherwise identical twins), or pinning a price value. |
| `SV-DATED-RATES` | Pricing a record at "now" instead of the record's own timestamp. |
| `SV-NO-LOCAL-UPLOAD` | Re-adding a file picker, drag-drop, or an upload endpoint. It was removed deliberately. |
| `SV-FIXTURE-SIZE` | Committing large fixtures. Parser fixtures stay under 1 KB. |

Do not "helpfully" add a bundler, an npm dependency, or a build step. The
frontend transpiles in the browser on purpose; that is a design decision,
not an oversight. The root `package.json` is the **CI toolchain only** —
eslint, eslint-plugin-react, AST coverage, `globals`, devDependencies and nothing
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

- **db** — PostgreSQL 16, built locally from the one-line Dockerfile CI
  pins (`.github/actions/postgres-image`; the same image CI's smoke leg
  runs from its cache). On its first boot, against an
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

React, ReactDOM and Babel ship in `public/vendor/` and load from the
server's `/vendor/` paths with SRI hashes over the stripped files.
Their upstream licence notices ship beside them in `LICENSES.txt`.
Google Fonts still loads off-site, with system-font fallbacks (README:
[Client-side internet requirement](README.md#client-side-internet-requirement)).

## Tests

```bash
python3 -m pytest tests/ -q             # full suite
python3 -m pytest tests/test_pricing.py -v
python3 -m pytest tests/ -q -m "not db" # portable subset — no database needed
```

Local suite commands inherit `CLAUDIT_THRESHOLDS_BASE_REV`. Without it,
committed-tree checks grant no removal exemptions. To test a declared
removal before master re-seeds, set it to the intended local judged base
(e.g. `CLAUDIT_THRESHOLDS_BASE_REV=$(git merge-base HEAD origin/master)`).
An unreadable base or range also grants no exemptions.

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

CI runs **twenty-five** workflows. `ci-gate.yml` owns the push/PR
surface: it starts on every push to `master` and every pull request
against it (a pull request's commits are checked once, with no review
gate first), classifies the changed paths, runs the eleven gate
workflows as reusable legs, and folds them into one verdict —
**`ci gate / aggregate`**. A documentation-only change (`*.md`,
`PRESENTATION.txt`, `examples/`, `.claude/`, licences) narrows the
expensive legs by classification instead of by `paths-ignore`, so
every check still reports rather than going MISSING. Prose changes in
`README.md`, `CONTRIBUTING.md` or `AGENTS.md` also skip the legs when their
pinned setup facts are unchanged. A documentation-only change to those
facts runs only the inline `setup-docs` job (`tests/test_docs_setup.py`);
unreadable setup sections or a code change run the full gate set, whose
tests leg includes the guard (issue #1054). A branch without
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
`scripts/ci/js_coverage.mjs` over `src/**/*.js` — the files node actually
executes, with the existing default test exclusions. It uses
ast-v8-to-istanbul with Acorn and writes the Istanbul JSON summary; its
`--lines` option checks the unrounded line percentage against the floor. The `.jsx`
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

The pinned `Nitjsefnie-Actions/comment-ratchet` action in
`.github/workflows/comment-share.yml` checks every pull request against
records at its base and tightens on every `master` push. It measures tracked
Python, JavaScript/TypeScript, YAML, shell and SQL files at a 15% target;
only files above that target carry exact `[comment bytes, non-whitespace bytes]`
records in `.github/comment-share.json`. Tighten writes the records into
the checkout; `.github/workflows/ratchet-push.yml` lands the tightened
records on `master`. The action's
[pinned README](https://github.com/Nitjsefnie-Actions/comment-ratchet/blob/2791c0c2ebdd3bb13c62d37771f766b04fa2fc29/README.md)
describes its measurement and ratchet rules.

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
| `ci-gate` | The aggregate gate. Starts on every push to `master` and pull request, classifies the changed paths (docs-only changes skip every leg; changes only to pinned setup facts in README.md, CONTRIBUTING.md or AGENTS.md run the inline `setup-docs` guard alone; unreadable setup sections or code changes run the full gate set, with the guard inside the tests leg; every check still reports), runs the eleven gate workflows as reusable legs, and folds them into one `ci gate / aggregate` verdict. The aggregate job also carries the two gate-integrity checks on every run, docs-only changes included: the merge-conflict-marker scan over the tracked tree, and `scripts/ci/commit_scopes.py`, which refuses a commit whose scope names a workflow under a non-`ci` type (see [Commit subjects](#commit-subjects)). Master-push runs are grouped per commit SHA, so a newer push never cancels an older run (issue #358); a deliberate cancel reads never-green. |
| `tests` | The `ci-gate` pytest leg runs the full suite with coverage, gates and ratchets the reparse hot path's per-phase bytecode counts — the per-phase CPU shares ride along as telemetry, not a gate (`.github/actions/reparse-bench`, `scripts/ci/reparse_bench.py`; the bench itself is documented in its own docstring) — measures ONE pytest pass over a pinned test-file fixture in bytecode instructions and fails the canary job if any phase exceeds its committed ceiling in `.github/ci-thresholds.json` (`.github/actions/suite-bench`, `scripts/ci/suite_bench.py`; a ratchet-down-only budget the tighten bot lowers when a cheaper run justifies it, never a wall-clock number: runner speed varies ~2x between jobs, but an instruction count is exact), and raises the committed coverage floors or tightens file-size and pylint-suppression baselines when the measurements justify it. The `Threshold direction guard` refuses lowered coverage, raised baselines, moved workload identity, hand-added core entries, and cost-budget raises without a `feat:`/`feat(scope):` commit. Entries or families may be removed only when their subject is gone or their number is wrong/no longer comparable, with same-tree before/after evidence for the latter. A `Thresholds-Removed: <entry>=<bug\|definition>` trailer in the judged range is required unless a path-typed baseline's file is gone; master seeds absent calibrations from its measurements. The ratchet data (the measured thresholds file and the suite-cost measurement) is staged and uploaded here; the push lives in `ratchet-push.yml`, a top-level `workflow_run` workflow keyed on the ci-gate run's completion (issue #479: the `master-push` deploy key's secret resolves empty inside a `workflow_call` callee, and a `GITHUB_TOKEN` push is rejected by the ruleset's required `aggregate` check). |
| `ratchet-push` | The data-only push half of the ratchets (issue #479): fires on completed `ci gate` and `comment share` runs, and only from successful master pushes. Its job takes the deploy key from the `master-push` GitHub environment, lists the triggering run's artifacts, downloads the measured thresholds file, suite-cost measurement and comment-share records (`comment-share-ratchet`) independently when present, tightens the suite-cost budgets from the measurement (a downward-only data operation; never raises), and pushes the ratchet commit. It runs no test, dependency or upstream code and stops its ssh-agent after the push. A push refused because master moved is dropped with a notice — the next master run measures and raises instead. Like the pricing bot's deploy-key push, the ratchet commit starts workflow runs, but a `.github/ci-thresholds.json`-only change stays silent in ci-gate by its paths-ignore. |
| `coverage-comment` | Posts the informational patch-coverage comment (issue #560). Fires on the ci-gate run's completion and only for a pull-request run, because `tests` is a `workflow_call` callee and produces no run of its own. It is the write-capable half of a boundary the `tests` leg cannot cross: that leg checks out and RUNS the proposed tree, so it holds `contents: read` and nothing more — before issue #560 it held `pull-requests: write` for the comment itself, which on a fork is read-only anyway, so no fork pull request ever saw the readout. Only the RENDERED BODY crosses, as an artifact. Every artifact byte is untrusted: the destination pull request is resolved from the `workflow_run` event's own `pull_requests`, the artifact's claim is only compared with it (a mismatch fails the step loudly), the body must be an ordinary file, is size-capped and refused rather than truncated, and reaches GitHub as a file argument — never executed, never shell-interpolated. No `actions/checkout`, no pull-request code, no `checks: write`; it publishes no check run, joins no aggregate, and is not a gate. A run without the artifact refreshes the marker so a previous commit's percentage cannot look current. If GitHub ever hands back an empty `pull_requests` array, the number is resolved from the base repository by merge-ref lookup instead, and the run posts **nothing** rather than posting a number it cannot prove is current. |
| `test-data` | Would a refresh-shaped change — moved rates, bumped version constants — break the suite? Perturbs the tree exactly that way (`scripts/ci/perturb_test_data.py`) and runs the full suite against it on the cached Postgres 16 container; no coverage, no ratchet — the verdict is the perturbed suite's pass/fail. Runs on three fixed seeds so the verdict is deterministic (the appended `from` stamps are decoupled from the seed, so any seed is safe), crossed with four pytest shards in a seed × shard matrix (issue #1123). Each job perturbs its own tree and runs its shard; the four shards cover the whole suite for each seed. A combine job reports each seed's verdict and per-phase wall/CPU totals with the shard breakdown. Its wall totals sum the shards' measured work; they are not elapsed workflow duration. A ci-gate leg, skipped on docs-only and setup-only changes. The second half of SV-TEST-DATA's enforcement. |
| `test-data-explore` | The daily explorer for `test-data`: draws three wall-clock seeds and runs the same full perturbed suite per seed, naming any failing seed in the run summary. Not a ci-gate leg — a red run means "look at this seed", never "block". |
| `gate-freshness` | Publishes the `gate freshness` check run on every open PR head, on each master push (all heads) and each `pull_request_target` event (the event's own head, filtered to the base it protects): red iff master holds a commit the head lacks whose changed paths hit the gate trigger set — the workflows, local actions and CI scripts, the ratchet data, the scanner/lint/type configs, the requirements files, the docker-smoke Compose override. A `.github/ci-thresholds.json` tighten triggers this workflow although the gate suites ignore it. Unreadable per-head compares publish red; a global read failure publishes nothing and exits nonzero. Advisory until the `require-ci-aggregate` ruleset requires the name `gate freshness`. |
| `codeql` | Security analysis for Python and JS; findings go to the Security tab, not the build. Also runs weekly as a standalone cron, because new queries only ever see code that changed after they shipped. |
| `scorecard` | OpenSSF Scorecard: measures the repository's supply-chain posture (branch protection, signed releases, pinned actions, review history) and uploads the SARIF to code scanning. Weekly on Saturday, and on manual dispatch. It is a **trend signal, not a gate** — it runs on no contribution event, so it is never a second verdict on a commit `ci-gate` already judges, and a red scorecard never blocks a release or a merge. Publishing (`publish_results`, over OIDC) happens only on the upstream default branch: a fork's run files into the fork's store, another branch's under this repository's name. The write permissions it needs (`security-events`, `id-token`) sit on the job, never on the workflow, because upstream refuses to publish from a workflow that declares write permissions above the job. |
| `audit` | `pip-audit` over every requirements file, resolving the full transitive tree. Also runs daily as a standalone cron — an advisory lands without a commit here to hang it on. |
| `panel-layout` | The rendered-layout gate (issue #631). Drives the real page in headless Chromium against the frozen payload set in `fixtures/layout/` at three viewport widths, and reads back every panel region's `getBoundingClientRect()`: each must sit inside its own panel, and no two may intersect. It exists because nothing else in the suite renders the dashboard, which is how issue #630's legend-over-chart reached a user with every other check green. One generic check over every panel, not a suite per panel; no database and no bucket. The browser driver is a devDependency like every other CI tool here. |
| `docker-smoke` | The container leg (issue #941): builds the `Dockerfile` image and boots the compose stack (`compose.yaml` — Postgres built from the pinned one-line Dockerfile with its first-boot init, the app on the `file://` fixture mirror) on a bare runner that carries no `CLAUDIT_R2_*` or legacy `R2_*` variables. Asserts the RESOLVED compose config is file-backed and credential-free before `up`, probes `/health` until the startup ingest finishes against the mounted mirror, and tears the stack down under `if: always()`. Both registry images come from the Actions cache (below): every compose call adds `.github/docker-smoke.compose.yaml`, which runs `db` from the cached Postgres tag with `pull_policy: never`, and the build finds the cached base in the local image store because nothing passes `--pull`. A ci-gate leg on every change outside the docs-only, setup-only, bot-data-only and thresholds-only classes. |
| `actionlint` | `actionlint` + `zizmor` over the workflow files themselves. A broken workflow does not go red, it silently stops running. Runs as a ci-gate leg on every non-docs change. |
| `release` | Starts when `ci gate` completes on a successful `master` push, then releases only a commit that itself changed `VERSION`, after every other check on that commit has passed. Dev versions (`X.Y.Z-dev`) tag nothing. A failed release leaves a red `release` run. |
| `release-dispatch` | Manual dispatch validates `X.Y.Z`, pushes its VERSION commit with the `master-push` deploy key, and ends there. On `release` completion, a keyless `decide` job attributes the run to the commit introducing master's current VERSION, then requires a successful release conclusion plus both tag and release probes before the keyed successor pushes the next patch `-dev`. A failed introducer's release leaves a red successor run and blocks that push; other commits' runs exit quietly. Dispatch and successor use separate concurrency groups. Master movement retries up to three times; rejection on unchanged master fails. |
| `version-guard` | Fails a master push or PR whose tree `VERSION` names an already-published release (an existing `v<VERSION>` tag). Under the dev-suffix discipline (`0.4.0-dev` between releases) this only ever fires on a missed bump. The hourly pricing bot's own commits are exempt; PRs get no carve-out. |
| `refresh-pricing` | Hourly (and on dispatch): re-fetches OpenRouter's per-provider prices and change logs, and the first-party vendor table (the id and serving host of every catalog model under a configured `openrouter.vendor.prefixes` namespace) by vendor prefix. Each run rewrites log-backed histories from the complete listed-pricing log through detection, overlays sampled search/context history, and merges identical dated states. First-seen hosts retain the history-floor import; sampled hosts stay append-only at detection with alternation. Changed data bumps `PRICING_VERSION` and commits to `master` after the suite passes. The manual `backfill_provider_rates.py` CLI shares the rewrite core. The push is a separate key-only job that takes the deploy key from the `master-push` GitHub environment, reachable only from master: it receives the tested data (the two data files, the commit message and the tested base) as an artifact, runs no test/dependency/upstream code, and stops its ssh-agent after the push. Unlike a `GITHUB_TOKEN` push, this starts workflow runs intentionally, so the hourly pricing commit gets a real aggregate verdict on master's tip. There is no in-run retry: a push refused because master moved is dropped with a notice, and the next hourly run re-fetches on the new tip, re-tests in the keyless job, and lands the data there. A red run means a host needs a human decision, such as one listing a model at two prices; every other host's moves are still published. See SV-RATE-REFRESH. |
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
needs a Postgres your user can `createdb` on. For a private cluster, run
`eval "$(scripts/scratch_pg.sh start .pg 5443)"`; use `scripts/scratch_pg.sh
env .pg 5443` to print its exports again, and `scripts/scratch_pg.sh stop .pg`
to stop and remove it. Every name carries a per-run
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

## Module map

What each file holds; `AGENTS.md` keeps only the family-level map.

```
backend/          — FastAPI application
  app.py          — startup/shutdown, route mounting, static serving,
                    index.html rewriting (cache-bust, auth injection)
  api.py          — router assembly + the small panel endpoints
                    (/api/me, /api/projects, /api/tool-usage,
                    /api/tool-error-rate, /api/activity-heatmap,
                    /api/cost-by-context, /api/cost-by-agent,
                    /api/reply-latency, /api/events SSE, /api/models,
                    /api/context-growth/{agg,session}); includes the
                    sub-routers below
  api_common.py   — shared endpoint helpers: dated-rate fold,
                    _parse_range/_bucket_seconds/_iso, HEATMAP_TZ
  rate_boundaries.py — per-(model, provider) rate-change instants, in
                    pricing.resolve's order (SV-DATED-RATES)
  timing.py       — CLAUDIT_TIMING flag, logger setup, Phases collector
  api_dashboard.py — /api/dashboard (sources/queries/build split)
  api_cache.py    — /api/cache (prompt-cache totals, TTL-split cost,
                    top requests)
  api_sessions.py — /api/sessions*, transcript, sidecar
  api_export.py   — /api/export PNG render subprocess
  api_web_metrics.py — POST /api/metrics beacon sink and GET
                    /api/web-metrics p50/p75 readout
  constants.py    — dependency-free shared constants; keeps the
                    api/ingest import graph acyclic
  turn_flags.py   — pre-request events, folded onto the NEXT request as
                    records.turn_flags
  parse.py        — JSONL → records, ctx_turns, rate_limit_hits,
                    tool_uses (incl. per-call churn); the SV-PARSER-SPEC
                    Claude path; parse_file() sniffs the format and
                    dispatches lane formats
  agent_sidecar.py — agent meta.json sidecar classification: role,
                    teammate marking, teammate_name; parses no transcript
  prompt_gate.py  — XML-open deny-by-default prompt gate: a user text
                    opening with a tag is harness-injected unless on the
                    pasted_content keep-list; denied texts count no
                    prompt and split no ctx turn; latency anchors separately
  reply_timing.py — independent Claude reply/generation clocks: reply latency
                    anchors on non-interrupt user text; generation spans use
                    any non-replayed user line through a message.id's last line;
                    CLAUDIT_COLLECT_GENERATION_SPEED via explicit policy
                    (registry default on; Claude only, other lanes NULL)
  codex_fork.py   — the Codex rollout's head: first declared model,
                    fork detection (forked_from_id), replay-prefix
                    marking for the canonical rank (issue #687)
  parse_codex.py  — Codex rollouts: cumulative-token differencing,
                    cross-file request identity, long-context meter,
                    apply_patch churn
  parse_kimi.py   — both Kimi wire formats (kimi-code, legacy kimi-cli)
  kimi_content.py — kimi-code content and tool-call helpers
  parse_common.py — machinery shared by all parsers: per-file state,
                    turn bookkeeping, billing-row builder, tool-result
                    settling and error_kind, _dispatch_prompt_shape,
                    ctx-turn builders
  json_shape.py   — decoded-JSON object/array guards
  parse_lanes.py  — sniff_format and the lane→claudit row adapter
                    (to_claudit); legacy Kimi tool ids namespaced by file
                    key; lane default roles → DEFAULT_AGENT_TYPE
  key_layout.py   — object key → (project, session, is_main) for both
                    layouts (Claude's <project>/<session>/<stem>.jsonl,
                    lanes' sessions/<project>/<session>/wire.jsonl);
                    pairs subagent transcripts with their meta.json
  lane_markers.py — stored project.json markers (etag + reader version);
                    refetched only when missing or stale
  lane_projects.py — lane-project identity: stored hash→project_id
                    fallback, marker→stored→hash resolution, stall rekey
  project_aliases.py — per-deploy alias fold (SV-PROJECT-ALIASES)
  branding.py     — brand values → title/meta/logo/sign-in page/export
                    filename (SV-BRAND-ESCAPE)
  tool_errors.py  — tool-result text and the harness-generic failure
                    classification (SV-WHY-COLUMNS)
  target_paths.py — lexical target paths in the transcript's namespace
                    (Windows drive/UNC/device vs POSIX incl. Git-Bash
                    /c); no filesystem lookup
  bash_argv.py    — churn for ARGV-array commands (Codex `monitor`),
                    handed to bash_churn's scanners
  bash_reads.py   — read/write targets and whole-vs-slice from Bash text
                    (SV-CONTEXT-INTAKE)
  bash_literals.py — bounded shell words and literal output, provenance
                    kept; no expansion or execution
  bash_loops.py   — heredoc repeat count from enclosing literal loops
  bash_dash_c.py  — Python stdin/`-c` matching and inline tokenization
  bash_churn.py   — churn from Bash text: heredoc writes, git-apply/patch
                    hunks, literal python read/replace/write bodies, and
                    the paths they write; counting rules in SV-BASH-CHURN
  bash_churn_errors.py — whether a heredoc write survives a later
                    stage's failure
  bash_heredocs.py — what a heredoc opener's context means (patch,
                    python, file write) and diff-body hunk counts; split
                    from bash_churn for size
  pricing.py      — model and (model, provider) rate resolution; logic
                    only (SV-RATE-DATA, SV-PROVIDER-RATES)
  pricing_load.py — loads src/pricing.json's rate tables, checked, at
                    import; pricing.py re-exports them (SV-RATE-DATA)
  long_context.py — the long-context meter's global constants (threshold,
                    multipliers) — the DEFAULT a member without a
                    long_context_meters entry keeps; dependency-free; the
                    meter membership and per-model thresholds are
                    pricing.json data (long_context_models,
                    long_context_meters), re-exported through
                    pricing.LONG_CONTEXT_MODELS/_METERS
  rate_fingerprint.py — per-(model, provider) digest of the rate data
                    resolve() consults, plus the pricing modules' source;
                    the reprice pass's clean restamp proves a row clean
                    from it (SV-REPRICE)
  ingest.py       — R2 walk, reparse decision, two-phase persistence,
                    ingest_done SSE
  ingest_walk.py  — project identity and stored-version helpers
  ingest_persist.py — `_persist`: one file per transaction, rows
                    written by COPY
  ingest_progress.py — /health: progress readout, the cache block
                    (outcome counters + pool gauges), last-ingest row
  ingest_timing.py — per-run timing state
  ingest_scope.py — per-run dirty scope and derived-state fingerprint
                    (SV-ROLLUP, SV-SCHEMA-AUTOAPPLY)
  ingest_fetch.py — fetch/parse worker pools, cooperative abort, thread
                    and process pipelines (CLAUDIT_INGEST_PARSE_PROCESSES /
                    CLAUDIT_INGEST_PERSIST_THREADS); re-exported via ingest so
                    tests keep their patch seams
  ingest_resolve.py — per-item outcome pairing (resolve /
                    resolve_futures), failure booking and the fetch
                    error classes; split from ingest_fetch for size
  ingest_rollup_state.py — suppression, canonical flags,
                    replayed-model adoption, teammate resolution
  ingest_rollup_hourly.py — the seven hour-keyed rollup rebuilds
  ingest_rollup_latency.py — latency_rollup rebuilds
  ingest_rollup_web_metrics.py — web_metrics_rollup rebuilds, over the
                    beacon retention window
  ingest_rollups.py — compatibility re-exports of the derived rebuilds (their order
                    lives in ingest.py)
  ingest_reprice.py — the reprice pass: recomputes cost_usd and
                    records.long_context in place, no R2 fetch
                    (SV-REPRICE)
  ingest_orphans.py — orphan sweeps: file rows whose R2 key is gone,
                    unreferenced projects
  ingest_scan.py  — the ingest's listing scan: transcripts paired with
                    meta.json sidecars (_Wire), lane markers
  ingest_runs.py  — ingest_runs row lifecycle: open at run start, close
                    at run end; failure summary for /health
  ingest_lockwatch.py — liveness guard for the db-wide ingest advisory
                    lock; a lost lock aborts the run
  ingest_warm.py  — response-cache warming after an ingest or restart
                    (warm_common, WARM_RANGES)
  r2.py           — S3 client with file:// mirror fallback; get_object
                    consults the deploy's disk blob cache when handed an
                    etag (#684)
  auth.py         — PBKDF2-SHA256 hashing/verification
  login.py        — /login, /logout, /login/guest, rate limiting
  login_page.py   — the sign-in page's HTML template and response
                    builder; every slot escaped at its own site, CSP
                    nonce on the inline style (SV-BRAND-ESCAPE)
  login_workers.py — runs login CPU work without releasing its
                    admission slots early (WorkerReservation)
  session.py      — HMAC session cookies, auth middleware, guest sentinel
  events.py       — thread-safe SSE broadcaster
  sse.py          — SSE body iteration and ownership of its waiters
  db.py           — viz_pool (claudit) and auth_pool (auth DB,
                    READ-ONLY); pools never join across DBs
  cache.py        — LRU for raw transcript bytes (256 MB, 20-min idle)
  cache_outcomes.py — the response cache's outcome counters; invalidate()
                    logs the since-last-invalidate window, /health the totals
  blob_cache.py   — disk blob cache for unchanged R2 objects, keyed
                    (bucket-qualified key, etag), raw compressed bytes,
                    best-effort, mtime-LRU prune (#684)
  web_metrics.py  — the beacon sink's closed vocabulary and its write;
                    the rollup grain's only writer
  schema*.sql     — applied at every startup as one script under one
                    stamp, in db.SCHEMA_PATHS order (SV-SCHEMA-AUTOAPPLY)

public/           — index.html (React/Babel bootstrap; rewritten per
                    request to inject window.BACKEND_URL, window.IS_GUEST
                    and mtime ?v= cache-busts), app.css (dark theme)

src/              — served at /src/* (in-browser Babel)
  app.jsx         — shell, routing, dashboard fetcher, SSE, synthetic
                    preview
  parser.js       — browser transcript parser (SV-PARSER-SPEC)
  rates.js        — browser rate resolution, pricing.resolve's mirror
                    (SV-RATE-DATA); what computeSessionStats prices
                    through; the Claude-path meter decision
                    (longContextFlagFor)
  pricing.json    — every rate table (SV-RATE-DATA)
  parser-lanes.js — browser lane sniff + shared helpers + the Kimi
                    parsers + window.LONG_CONTEXT_* +
                    longContextThresholdFor
                    (SV-PARSER-SPEC lockstep)
  ctx-input.js    — window.usageCtxInput, moved out of parser.js (the
                    browser half of backend/ctx_input.py)
  parser-codex.js — browser codex lane parser: cumulative-token
                    differencing, first-declared-model attribution,
                    replay-prefix marking, multi-load record dedup
                    (SV-PARSER-SPEC lockstep)
  record-dedup.js — browser half of the canonical winner rule
                    (SV-CANONICAL-FLAG): an original beats a replayed
                    copy, an attributed copy an unattributed one
  dashboard-binning.js — bin width, never finer than the server bucket
  dashboard-fetch.js — the Overview's /api/dashboard request state:
                    loading / empty / error outcomes (plain JS, so node
                    can execute it)
  dashboard-summary.jsx — the Overview's status block
  dashboard-charts.jsx — core SVG panels
  dashboard-charts-extra.jsx — context growth, cache TTL panels
  context-growth-view.jsx — context growth components
  detail-pane.jsx — session detail / inspector
  sessions-list.jsx — sessions page: sortable table of reconstructed
                    sessions
  timeline-selection.js — shared Inspector timeline-selection: the one
                    clamped selection index every consumer derives from
  event-helpers.jsx — event formatting helpers
  synthetic-data.js — synthetic dashboard data
  views/          — cache-view.jsx, context-growth-view-v2.jsx

scripts/plots/    — DB-backed usage plot (ccusage_plot_db.py, _render.py,
                    _timeline.py) over `records`; visual parity with
                    upstream nhz-io/ccusage-plot

tests/            — pytest suite (conftest.py forces file-mode R2 and
                    test-safe env defaults)

fixtures/         — parser/, codex/, r2_mini/ (SV-FIXTURE-SIZE)

examples/         — claudit.service

.claude/rules/    — Doctrine — a cross-cutting core plus the
                    path-scoped domain files (SV-* rules)
```

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
- the gates run and their outcomes, in the template's Testing checklist,
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
