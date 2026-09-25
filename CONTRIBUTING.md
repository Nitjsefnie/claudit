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
[`.claude/rules/claudit-doctrine.md`](.claude/rules/claudit-doctrine.md),
which holds the invariants as numbered rules (`SV-*`). Those two files are
authoritative; this one only covers process.

The rules that reject the most patches, in order:

| Rule | What it forbids |
|---|---|
| `SV-COST-SPLIT` | Pricing a cache write at a single rate. 5m is 1.25x input, 1h is 2x. |
| `SV-PARSER-SPEC` | Changing rate resolution in `backend/pricing.py` without mirroring `src/parser.js` (or vice versa). |
| `SV-RATE-DATA` | A rate anywhere but `src/pricing.json`, or rewriting an entry of its history instead of appending one. |
| `SV-RATE-REFRESH` | Back-dating, editing or deleting a provider-rate entry, taking one of a host's two in-region prices without a resolution recorded as data (a tag, or the cheaper of otherwise identical twins), or pinning a price value. |
| `SV-DATED-RATES` | Pricing a record at "now" instead of the record's own timestamp. |
| `SV-NO-LOCAL-UPLOAD` | Re-adding a file picker, drag-drop, or an upload endpoint. It was removed deliberately. |
| `SV-FIXTURE-SIZE` | Committing large fixtures. Parser fixtures stay under 1 KB. |

Do not "helpfully" add a bundler, an npm dependency, or a build step. The
frontend transpiles in the browser on purpose; that is a design decision,
not an oversight.

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

cp backend/.env.example .env      # then edit: DATABASE_URL_VIZ, R2_*, ADMIN_TOKEN
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt

python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

No R2 credentials? Point `R2_ENDPOINT` at a directory instead — the
client walks the tree in `file://` mode:

```bash
R2_ENDPOINT=file:///path/to/transcripts/
```

`fixtures/r2_mini/` is a tiny mirror you can point at to get a working
instance in seconds.

## Tests

```bash
python3 -m pytest tests/ -q             # full suite
python3 -m pytest tests/test_pricing.py -v
python3 -m pytest tests/ -q -m "not db" # portable subset — no database needed
```

The `db` marker splits the suite for CI's portability matrix (Linux,
macOS and Windows × Python 3.13/3.14 run everything that needs no
PostgreSQL). It is applied mechanically, derived from fixture usage —
a test whose fixture closure reaches a fixture registered in
`tests/db_marker.py` is marked for you — and the few tests that reach
the server without any DB fixture carry `@pytest.mark.db` explicitly;
`tests/test_db_marker.py` holds both halves in place against the source.

CI runs **fourteen** separate workflows — on pushes to `master` and on pull
requests against it; a pull request's commits are checked once, with no
review gate first, and a branch without a PR is checked by dispatching
(`gh workflow run <workflow-file> --ref <branch>`). A green suite is a
small fraction of the gate. These five you can and should run locally
before pushing:

```bash
python3 -m pytest tests/ -q --cov=backend            # tests (+ coverage)
git ls-files -co --exclude-standard '*.py' | xargs pylint      # lint, gate 1
git ls-files -co --exclude-standard '*.py' | xargs pycodestyle # lint, gate 2
pyright                                                        # types
npx --no-install eslint 'src/**/*.js' 'src/**/*.jsx'           # eslint
python3 scripts/ci/smoke.py                                    # smoke
```

Use `-co --exclude-standard`, not a bare `git ls-files`. CI lints the
committed tree so its own plain `git ls-files` is right *there*; locally
it is a trap, because a brand-new module is untracked until you stage it
and pylint will report a clean run over every file except the one you
just wrote.

`pip install -r requirements-dev.txt -r requirements-test.txt` gets the
pinned toolchain. Coverage is gated at 82% — a ratchet set under the
current number, not a target.

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
| `codeql` | Security analysis for Python and JS; findings go to the Security tab, not the build. Also runs weekly, because new queries only ever see code that changed after they shipped. |
| `audit` | `pip-audit` over every requirements file, resolving the full transitive tree. Runs daily — an advisory lands without a commit here to hang it on. |
| `actionlint` | `actionlint` + `zizmor` over the workflow files themselves. A broken workflow does not go red, it silently stops running. |
| `speed` | Runs the last release's suite and yours on the same runner, interleaved, and fails if the tests present in both got more than 30% slower. |
| `release` | Cuts a tagged release when `VERSION` changes on `master`, after every other check on that commit has passed. A dev version (`X.Y.Z-dev`) skips everything — nothing is tagged. |
| `version-guard` | Fails a master push or PR whose tree `VERSION` names an already-published release (an existing `v<VERSION>` tag). Under the dev-suffix discipline (`0.4.0-dev` between releases) this only ever fires on a missed bump. The hourly pricing bot's own commits are exempt; PRs get no carve-out. |
| `refresh-pricing` | Hourly (and on dispatch): re-fetches OpenRouter's per-provider prices, appends every moved or new rate effective from the moment it was seen, bumps `PARSER_VERSION`, and commits to `master` after the suite passes. A red run means a host needs a human decision, such as one listing a model at two prices; every other host's moves are still committed. See SV-RATE-REFRESH. |
| `claim` | Lets a contributor without write access take an issue: comment `/claim` on an open, unassigned issue and the workflow assigns you; `/unclaim` and `/release` remove only your own assignment. Runs no repository code — it talks to the GitHub API only. See [Claiming an issue](#claiming-an-issue). |
| `pr gate` | Checks every non-draft PR's description against `.github/PULL_REQUEST_TEMPLATE.md` and requires a reference to an issue assigned to the PR's author. A non-conforming PR gets a comment naming what is missing and is closed; the gate reopens it once the description is corrected. Never runs pull-request code. See [Pull requests](#pull-requests). |

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
  ingests the same mirror at `INGEST_WORKERS=1` and `=8` and requires
  byte-identical output. If you touch ingest concurrency, this is the test
  that catches you.

## If you change how cost is computed

Bump `PARSER_VERSION`, the code constant in `backend/constants.py`, in
the same commit as the change. Every file reparses on the next ingest;
without the bump, stored `cost_usd` values keep the old rates and the
dashboard silently mixes them. Mention the bump in your PR so deployers
know a reparse is coming.

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
  and `.eslintrc.json`, and CI enforces all four. Formatting opinions
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
