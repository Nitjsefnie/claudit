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

Stored suite-cost and reparse budgets have no place in the threshold
document. Their base-vs-head gates compare measurements from the same
run; neither family has stored seeds, markers, or ratchets.

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
  added. `scripts/ci/comment_baseline.py` fails only for a measured share
  above its entry or an unrecorded share above 15%. Lower/equal shares,
  changed pairs without a rise, deleted-file and at/below-15% entries pass.
  The check runs after
  pytest and the JS toolchain install, on comment-only changes too.
  On master pushes only, `--tighten` lowers entries and drops deleted or
  at/below-15% files, through `thresholds.write`. `--seed` introduces the
  section once from the measured tree. No re-seed marker applies.
- The direction guard (`scripts/ci/thresholds_guard.py`, a step in
  `tests.yml`) makes the never-rules mechanical: on every PR and master
  push it compares the data against the base document and fails on a
  lowered coverage value, a raised entry, or
  an added entry under the frozen core families (the Python and `src/`
  JavaScript scope the size ratchet had when the guard landed) or for
  an unmeasured path. What stays legal is exactly the bots' move set
  — a raise, a tighten, a brand-new member's entries — plus a new
  measured family's one-time seed. Size and suppression seeds are pinned
  by committed-tree tests on the same merge ref; comment shares permit
  reductions until master tightens.
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
