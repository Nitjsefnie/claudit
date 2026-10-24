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
  `scripts/ci/js_coverage.mjs` over `src/**/*.js` — the files node executes. `.jsx` panels are
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
  a passing one. Budget raises require a judged feature commit under
  the direction guard's rule below. Every run that wrote its measurement
  uploads it as the `reparse-measurement` workflow artifact. An absent
  family gates no recorded budget; master seeds it from that run's
  measurement through `reparse_ratchet.py`'s tighten path.
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
- Comment share: `.github/workflows/comment-share.yml` runs the pinned
  `Nitjsefnie-Actions/comment-ratchet` action on every pull request,
  checking against the records at its base, and tightens on every
  `master` push. The action measures tracked Python, JavaScript/TypeScript,
  YAML, shell and SQL files at a 15% target; only files above it carry
  exact `[comment bytes, non-whitespace bytes]` records in
  `.github/comment-share.json`. Tighten writes the checkout's records;
  `.github/workflows/ratchet-push.yml` lands the tightened records on
  `master`. Keep the action pinned and its records separate from
  `.github/ci-thresholds.json`.
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
  #840). Seeded and tightened FROM A RUNNER MEASUREMENT by the
  master-push bot (`suite_ratchet.py --tighten`, past the 1.5 hysteresis
  for existing budgets, both fields, never raised). The gate and the
  master-push record path share that environment; `residual` is
  environment-sensitive, so a non-runner seed would gate runners red.
  The interpreter micro is pinned in the
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
  established family. A fresh seed binds the budgets to the measured
  tree; a breach never licenses moving the recorded identity.
- Any entry or family may be removed in a PR for exactly two reasons:

  1. What it measures is gone from the tree: its file, test or family
     was deleted, renamed, split or merged. A successor is seeded fresh.
  2. The recorded number is wrong or no longer comparable: a fixed
     measurement bug, or a tool, tool-version or counting change that
     redefines the metric. Show before and after numbers on the SAME tree.

  A breach is never a reason: over a cost budget, trim tests; under a
  coverage floor, add tests. The before/after numbers are review evidence
  in the seat's report to the manager, never the PR body or PR comments.
- The direction guard (`scripts/ci/thresholds_guard.py`, a step in
  `tests.yml`) enforces the mechanically checkable removal rule: a removed
  entry whose subject still exists is refused unless the judged range
  carries a `Thresholds-Removed: <entry>=<bug|definition>` trailer naming
  it. Tokens are exact; only the path-typed baseline families
  (`module_size_baseline`, `pylint_suppression_baseline`) admit a bare
  family name declaring their entries, while `coverage`, `suite_cost`
  and `reparse` require full entry names.
  Path-typed baseline entries whose file is gone need no declaration.
  Coverage languages, `suite_cost` and `reparse` always need one.
  Missing or unreadable history fails closed. The judged range is
  merge-base..HEAD on a PR, the pushed range on a push; declarations do
  not replace review of the removal reason and measurement evidence.
- An absent entry or family is seeded from its measurement on the next
  full master run: `ratchet.py` for coverage; `reparse_ratchet.py`'s tighten
  path from the reparse measurement; `suite_ratchet.py --tighten` from
  the suite measurement through `ratchet-push.yml`; and the two
  baseline writers (`size_baseline.py`, `suppression_baseline.py`)
  with `--tighten` for their entries; coverage
  without a calibration still measures and reports, but gates no floor.
  A thresholds-only change schedules no measurement; a valid removal
  ships with the code change or subject deletion that justifies it, and
  the only thresholds-only removal is an entry whose file is already
  gone and needs no measurement.
  The baseline checks honour an approved declared removal by exempting
  exactly the declared path entries from the unrecorded default ceiling
  until master's ratchet seeds the successor.
- The direction guard also refuses lowered coverage, raised baseline
  entries, hand-added entries under the frozen core families or for
  unmeasured paths, and moved workload identity. Suite-cost and reparse
  budget raises require a subject starting with `feat:` or `feat(scope):`
  in the judged range. Missing, unreadable or empty ranges prove no
  feature and refuse raises. The feature exception admits numeric cost
  budget raises only; every other refusal stands. The bots' coverage
  raises, cost and baseline tightens, and measured seeds remain legal.
- Coverage numbers carry exactly one decimal (`92.0`, never `92` or
  `92.00`) — what the ratchet writes, `coverage --precision=1`
  measures and the JS gate's `toFixed(1)` reads. The document is
  always the loader's canonical bytes; never hand-edit it.
- The bot's raise/tighten commit on master touches only
  `.github/ci-thresholds.json` or `.github/comment-share.json`.
  `ci-gate.yml` and `version-guard.yml` ignore both paths so data-only
  commits restart neither the suite nor the version guard. The `comment
  share` workflow deliberately runs on every master push, including a
  ratchet data push; unchanged records stage no artifact and start no
  further push.

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

One more legitimate shape: a literal rate vector on a synthetic row pinned
at a FIXED PAST instant inside an already-closed dated window, testing
end-exclusivity and promotion boundaries against known vectors. The
perturbation leg only appends entries stamped after every real stamp, so
closed-window pins stay perturbation-invariant. A live row never backs a
rate pin: the automated refresh can rewrite any log-backed history,
including its closed windows (SV-RATE-REFRESH). Tests seed their own rows
and histories; `tests/test_provider_pricing.py`'s `synthetic_seeded_provider`
fixture and `SEEDED` instant are the closed-window exemplar. Live-tree
assertions may compare a resolution or cost with the same row loaded in
that run, so a rewrite moves both sides together. Reads of
`openrouter.models` keys alone are catalog configuration, not rate pins.

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
model or host. An inline `# sv-test-data: allow` comment with an invariance
reason excuses a site; a marker whose site is gone fails the guard as rot.
The guard checks these shapes and marker placement; reading each allowed
site verifies its stated invariance basis.
