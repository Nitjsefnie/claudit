---
paths:
  - "**/.github/workflows/**"
  - "**/.github/actions/**"
  - "**/.github/ci-thresholds.json"
  - "**/.gitleaks.toml"
  - "**/scripts/ci/**"
  - "**/tests/**"
---

# claudit Doctrine — CI and tests

## CI thresholds are self-raising ratchets (SV-CI-RATCHETS)

Coverage and module size are governed by
`.github/ci-thresholds.json`, validated by `scripts/ci/thresholds.py`
and enforced in `tests.yml` — never hand-set numbers in a workflow.

- Coverage: per language (`python`, `javascript`), `measured` and
  `floor = measured − 1.5`. On a `master` push,
  `scripts/ci/ratchet.py --language <language>` raises a calibration
  only when the run beats `measured` by more than the 1.5 hysteresis.
  Never lower a floor by hand, for any reason.
- Python measures the full pytest run (`--cov=backend`). JavaScript
  runs the node-executing tests under `NODE_V8_COVERAGE`, folded with
  c8 over `src/**/*.js` — the files node executes. `.jsx` panels are
  outside it: node parses no JSX, and parity tests' eval'd fragments
  are attributed to the eval. Each language gates against its own
  floor in `tests.yml`.
- **The gap sits ABOVE the measured value for every cost family, and
  BELOW it for every quality family.** This is not a typo: coverage is
  a QUALITY (higher is better, `floor = measured - 1.5`); a cost
  family's `floor` is the CEILING the recorded value may be exceeded
  by, `floor = measured + 1.5`, and only a run that got CHEAPER moves
  it. Writing a cost family the coverage way round puts the ceiling
  below the measurement that recorded it, and every later run at that
  measurement fails a gate no change can satisfy.
- Reparse (`reparse`): one record per phase of one reparse pass
  (`scripts/ci/reparse_bench.py`, called from `tests.yml` through
  `.github/actions/reparse-bench`), each phase carrying TWO instruments
  because each sees something the other cannot — and only ONE of them
  gates (issue #513):
  - `bytecodes` — hundreds of bytecode instructions per file, counted
    with `sys.monitoring`'s INSTRUCTION event
    (`scripts/ci/reparse_phases.py`). Exact: the same tree retires the
    same number on a loaded machine and an idle one, under any
    `PYTHONHASHSEED`. It catches the uniform per-file slowdown, and
    needs no amplification to be stable; its gap is sized for
    interpreter drift, not for noise. This is the GATED instrument: the
    enforced set is `thresholds.REPARSE_GATED_METRICS`, which the gate
    reads, and it fails closed — a count that was not taken fails the
    gate, because a gate whose only instrument is missing must not read
    as a pass.
  - `share` — the phase's percent of that run's own CPU,
    `time.process_time()` (never wall). Scale-free inside one run; it
    shows where the pass spends its work, and it is still measured,
    printed and recorded — but it is TELEMETRY: nothing compares it
    against the recorded share budgets and the ratchet never tightens
    them. A proportion of a timed run is not a count of work: its
    runner-to-runner spread measures 1.9-2.7 points against the 1.5-point
    gap it would be judged against (#500, #506), and it moves when the
    corpus MIX shifts between formats of different parse cost even with
    no code path slower and every count under its own ceiling (PR #512).
    The measurement doctrine's own principle decides it: counters gate,
    derived proportions do not. The share members STAY in
    `.github/ci-thresholds.json`, carrying the reading they recorded, and
    the loader and the direction guard keep validating and defending them
    like any recorded number; nothing fails on them.
  Both are costs: `floor = measured + 1.5`, and
  `scripts/ci/reparse_ratchet.py` mirrors `ratchet.py` with every
  comparison reversed, one gated phase at a time. The pass is
  decomposed by wrapping the callables the real path calls, and the
  residual — everything they do not account for — is a phase of its
  own, named and ratcheted, so an unmeasured phase cannot hide inside a
  measured one. `perf stat`'s machine-instruction count rides along as
  a CROSS-CHECK and nothing more: it prices a whole process rather
  than a phase, and needs a counter facility a hosted runner may
  refuse. When an instrument is unavailable the bench prints NOT
  MEASURED with the reason — an absent measurement must never read as
  a passing one. Never raise a floor by hand, for any reason. A
  parse-semantics change that legitimately spends the headroom takes
  the reparse family's own sanctioned re-seed — the delete under
  `[reparse-re-seed]`, then a runner-measured seed of the final tree,
  exactly the suite-cost path — and while the family is absent the
  reparse gate prints `no reparse budget — re-seed in flight` and exits
  0 (issue #698). Every run that wrote its measurement uploads it as the
  `reparse-measurement` workflow artifact — the runner-measurement
  source a reparse seed is fed through `reparse_ratchet.py --seed`, the
  way `suite-measurement` feeds the suite-cost seed.
- Module size (`module_size_baseline`): every tracked `*.py` under
  `backend/`, `scripts/`, `tests/`, every tracked `src/**/*.js(x)`, and
  the shipped SQL, CSS and workflow-YAML families (`backend/*.sql`,
  `public/*.css`, `.github/workflows/*.yml|yaml`) is capped per file
  (production 500 / test 700; everything but `tests/` is production)
  by `scripts/ci/size_baseline.py`. Entries are never added or
  raised by hand — an outgrown file moves code into a new module. CI
  tightens an entry as its file shrinks and drops it once back under
  the ceiling. A new file family may be seeded exactly once, at current
  line counts, through the loader's own writer; every later run follows
  the rule unchanged.
- Pylint suppressions (`pylint_suppression_baseline`): the per-file
  count of inline `pylint: disable`/`disable-next` comments naming a
  complexity check (`too-many-*`) over tracked `*.py` under `backend/`
  and `scripts/` (`tests/` stays outside), checked and tightened by
  `scripts/ci/suppression_baseline.py` under the same never-added,
  never-raised, only-shrinks rules as module size.
- Comment share (`comment_share_baseline`): exact `[comment bytes,
  non-whitespace bytes]` pairs for tracked Python, JS/JSX/MJS/CJS,
  YAML and shell sources above 15%, including extensionless shell.
  Integer cross-products compare shares without rounding. Unrecorded
  files stay at or below 15%; established entries never rise or get
  added. `scripts/ci/comment_baseline.py --matches-tree` checks the
  measured pairs after pytest and the JS toolchain install in the tests
  canary, on comment-only changes too. Master's `--tighten` lowers entries and drops those at
  or below 15%, through `thresholds.write`. `--seed` introduces the
  section once from the measured tree. No re-seed marker applies.
- Suite cost (`suite_cost`): per-phase instruction counts (millions of
  bytecode instructions, one decimal; phases `collection`, `run`,
  `residual`) of ONE pytest pass over the pinned fixture
  `scripts/ci/suite_bench_files.txt`, measured by
  `scripts/ci/suite_bench.py` with `sys.monitoring`'s INSTRUCTION
  events (the `process_time` fallback is telemetry that fails closed
  at `--check`). This is the suite-cost gate's instrument — runner wall is void, and a
  count is exact within one environment state (the bench records its
  across-state bound) — and the gap sits ABOVE the measured value because the number is a
  cost ceiling, not a quality floor. The measured workload loads the
  bounded `scripts/ci/suite_pricing_doc.json`, not the deployed
  document: the bench rebinds the loaded tables to it before any
  counted window opens (bench-side injection; the deployed app gains no
  override — `backend/` reads no environment and always loads
  `src/pricing.json`), so a data-only refresh cannot move the gate;
  `tests/test_suite_cost_pricing_growth.py` fails when the measured
  phases move with rows appended to the deployed document (issue
  #840). Seeded via the loader's writer
  (`suite_ratchet.py --seed`, only when the member is absent) FROM A
  RUNNER MEASUREMENT — the environment the gate and the master-push
  record path share; `residual` is environment-sensitive, so a
  non-runner seed would gate runners red; tightened by the master-push
  bot (`suite_ratchet.py --tighten`, past the 1.5 hysteresis, both
  fields, never raised). The interpreter micro is pinned in the
  workflows that run the bench. The budgets are BOUND TO THE WORKLOAD
  THEY MEASURED: the fixture's tree-scanning tests (the db-marker
  derivation, the version-literal guard, the scratch-DB scan) spend
  per-line over ``tests/``, so a change to that tree's content moves
  the counts with no fixture, interpreter or machine change. Every
  measurement records the workload identity `tests_tree_lines` (the
  tests/*.py line total) whenever its tree has one, and a seed writes
  it beside the budgets when its reading carries it; the gate names a
  drifted identity when a phase breaches instead of reading it as
  slowness, and the guard refuses a hand-moved identity on an
  established family — the sanctioned re-seed, measured on the tree
  the seed describes, is the only writer. Sanctioned RE-SEED: when the recorded
  workload legitimately changes — the fixture list, the interpreter
  pin, the code the pinned tests execute, or the scanned tree's
  content (issue #524: the day's 345.9→348.6 step was #518's +643 test
  lines moving the scans, not a runner change) — it lands as TWO reviewed
  gate-definers, never one. The first deletes the stale member under
  the commit-message marker `[suite-cost-re-seed]`
  (`scripts/ci/reseed.py`) — the only way `thresholds.load` accepts a
  family-absent document; the suite-cost gate then prints `no suite_cost
  budget — re-seed in flight` and exits 0, and the guard reads the
  removal as legal. Each cost family's re-seed runs under its OWN
  marker — suite_cost under `[suite-cost-re-seed]`, reparse under
  `[reparse-re-seed]` — the same bounded walk, fail closed, one probe
  per family: a marker buys that family's ABSENCE only, never a raised
  budget and never another family's absence (issue #698). The second
  seeds the new counts through the family's own writer —
  `suite_ratchet.py --seed` for suite_cost, `reparse_ratchet.py
  --seed` for reparse — FROM A RUNNER MEASUREMENT, cited by run id in
  the PR body, never hand-derived.
  Against a base that predates the family, those entries are a
  brand-new member's seed. One change cannot do both (the guard refuses
  an upward move the base carries): the delete is its own commit and
  its message carries the marker. The marker's scope is that delete —
  the seed commit does not carry it, and it is the commit that restores
  the family; a restoring document is valid either way. Every helper
  that re-validates a loader-produced document asks the same verdict
  the loader asked (`thresholds.verdict()`); `normalise` takes it as a
  required argument, so no helper can read strict by omission.

  **The measuring run measured the tree master will hold once the seed
  lands, the seed change's own edits included.** A budget binds to the
  tree it was measured on, so a measured tree master does not hold
  records numbers no later run reproduces — and the recorded identity
  catches the resulting drift only when it lies in `tests/` — while the
  guard admits no hand correction of either, so the repair is another
  delete+seed pair.
  Two properties of the run decide it, both read off the canary: the
  run RAN the tests leg (a docs-only or bot-data push skips that leg and
  carries no artifact), and the bench RAN (its step is gated on the
  suite and the JS coverage having succeeded, so a run whose suite
  failed carries no measurement either). Which run carries both follows
  from whether the window's changes have merged, not from choice:

  - They have MERGED: the `suite-measurement` artifact of the newest
    commit the window takes whose ci-gate run RAN the tests leg. A
    pricing-bot or docs-only push skips it, so the LAST commit is not
    always a usable source. The measured tree must equal master's tree
    at the seed, apart from `.github/ci-thresholds.json` and
    documentation-only paths; when a commit outside those landed after
    the newest measuring run, take a fresh measurement of the final
    tree by dispatching ci-gate (`gh workflow run ci-gate.yml --ref
    master`) — a `workflow_dispatch` run reads no changed paths, so it
    runs the full gate set, the tests leg and its `suite-measurement`
    artifact included.
  - They are still OPEN — the canary went red on a pull request, which
    includes the seed PR itself: that same artifact from the seed PR's
    OWN ci-gate run on the commit carrying its FINAL CODE AND TESTS,
    the last push before the data-only one. So the seed change lands
    its code and tests, waits for that run, seeds from its artifact,
    and regenerates the family in a later push touching nothing but
    `.github/ci-thresholds.json`. That later run is the CHECK, not the
    source: it must be green and name no workload-identity drift, and
    it is the first run to read the seeded identity back. Its own
    values cannot come from its own run — they must exist before the
    seed commit is written — so the source is always an earlier run.

  Both sources are measured BEFORE the merge: the artifact is retained
  for a day, and for a seed change touching ONLY
  `.github/ci-thresholds.json` a master push is path-ignored by
  ci-gate, so no master run exists afterwards to re-measure against.

  The measured tree MUST NOT CHANGE between the measuring run and the
  merge; the seed's own write of `.github/ci-thresholds.json`, and
  documentation-only paths, are the only permitted differences. `tests/`
  is the part of that tree the recorded identity detects
  (`tests_tree_lines`), but the budgets bind to every
  line the pinned tests execute — a pricing bot's `src/pricing.json` and
  `backend/constants.py` reach them through `tests/conftest.py` — so a
  code line landing after the measuring run invalidates it the same way.
  When the tree did change, the source is a fresh run of the FINAL tree;
  recorded numbers are never adjusted to fit.

  **The window is bounded.** `reseed._MAX_VISITS` caps the marker walk.
  On master each commit is one visit, and this repository rebase-merges,
  so master carries no merge commits and a pull request lands as one
  commit per commit it carries. A pull request's merge-ref run also
  visits GitHub's merge commit and every commit of the pull request,
  interleaved breadth-first with master's, so a multi-commit in-window
  pull request reaches the cap sooner: in-window pull requests land as
  one commit each, and the window closes while HEAD is fewer than ten
  visits from the delete. Past that the walk fails CLOSED: it reads as
  no marker, and the family-absent document is gated as any other is.

  **Closing a window is not re-opening one.** Two separate facts, and
  the next re-seed needs both: the walk reached a family-present commit
  — the window this marker opened is closed, so the delete's marker can
  never exempt a later family-absent document — and a NEW marked delete
  precedes the new seed. A seed already on master supplies the first;
  the second is a fresh pair, never the standing one re-used.
- The direction guard (`scripts/ci/thresholds_guard.py`, a step in
  `tests.yml`) makes the never-rules mechanical: on every PR and master
  push it compares the data against the base document and fails on a
  lowered coverage value, a RAISED reparse budget, a raised entry, or
  an added entry under the frozen core families (the Python and `src/`
  JavaScript scope the size ratchet had when the guard landed) or for
  an unmeasured path. What stays legal is exactly the bots' move set
  — a raise, a tighten, a brand-new member's entries — plus a new
  measured family's one-time seed and a cost family's REMOVAL, the
  latter admitted only on a commit carrying that family's own re-seed
  marker (`[suite-cost-re-seed]` for suite_cost, `[reparse-re-seed]`
  for reparse). A marker buys the absence, never a raised budget
  (the upward-move refusal stands). The truth of every seed is pinned
  by the committed-document-matches-tree tests, which run on the same
  merge ref.
- Coverage numbers carry exactly one decimal (`92.0`, never `92` or
  `92.00`) — what the ratchet writes, `coverage --precision=1`
  measures and the JS gate's `toFixed(1)` reads. The document is
  always the loader's canonical bytes; never hand-edit it.
- The bot's raise/tighten commit on master touches only the data file
  and must not re-trigger workflows: every gate workflow's push
  trigger (`version-guard.yml` included) ignores
  `.github/ci-thresholds.json`. A new gate workflow carries the
  exemption over.

## Tests never pin repository-managed data (SV-TEST-DATA)

A test NEVER depends on data the repository changes by design — the
rate rows in `src/pricing.json`, the committed `PARSER_VERSION` /
`PRICING_VERSION` / `MARKER_READER_VERSION`, or the host/model set the
refresh maintains (SV-RATE-REFRESH). Assert against synthetic data, or
values derived at run time (`str(int(constants.PRICING_VERSION) + 1)`).
A rounded expectation is exact only when computed by the same algorithm
over the same values in the same order as the code under test;
otherwise tolerate one unit in the rounded place (plus the code's
accumulated rounding).

One more legitimate shape: a literal rate vector pinned at a FIXED
PAST instant inside an already-closed dated window. Closed windows are
immutable (SV-RATE-DATA), so the pin is refresh- and
perturbation-invariant; it is the only way to test end-exclusivity and
promotion boundaries against known vectors. Stamp the reference
instant before every appended cutover (a fixed past `from`, strictly
before the seeded data); `tests/test_provider_pricing.py`'s SEEDED
comment is the exemplar.

Enforcement has two halves. The perturbed-data CI leg
(`scripts/ci/perturb_test_data.py`) runs the suite on a tree where each
rate row gains five appended entries per run — ×2, ×0.37, a per-row
irregular factor, five independent per-field factors, and a
single-field move, in seeded-shuffled row order — and the version
constants are bumped, so a hidden dependency fails as a test, not as a
broken refresh. The guard, `tests/test_no_pinned_version_literals.py`,
scans `tests/` for a literal assigned to any of the three constants
and for live-row shapes: a read of the committed document
(`pricing.PRICING_JSON`), a module-level bind of the `pricing.json`
path, and a `rate_for` / `resolve` / `compute_cost` call naming a live
model or host. An inline `# sv-test-data: allow` comment with a reason
excuses a site; a marker whose site is gone fails the guard as rot.
