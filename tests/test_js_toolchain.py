"""The JS/CI toolchain pins are a manifest Dependabot can see (issue #361).

Issue #361: `eslint.yml` installed `eslint@8.57.1` and `tests.yml` installed
`c8@10.1.3` inside a workflow `run:` line. Two release lines npm marks
deprecated, pinned where no update mechanism can reach them, with
`.github/dependabot.yml` carrying a hand-bump instruction ("Bump them by hand
when the github-actions PRs land") that the grouped action bump (#347) passed
straight over — the drift the issue describes.

The fix moves the toolchain to a committed `package.json` + `package-lock.json`
installed with `npm ci`, adds the npm entry to dependabot.yml so a bump opens
a PR, and migrates `.eslintrc.json` to `eslint.config.mjs` (ESLint 10 reads
no eslintrc: the `@eslint/eslintrc` dependency 8.57.1 carried is absent from
10.11.0's package.json). These tests pin the properties that made the drift
invisible:

- every tool version is an EXACT pin (the repo's standing pin doctrine), the
  manifest is private and carries no runtime `dependencies` — the frontend
  still has no npm dependency (CONTRIBUTING.md), so a `dependencies` block
  here would be exactly the bundler-adjacent creep that rule forbids;
- exactly ONE npm entry owns the manifest, the way exactly one pip entry owns
  each requirements file (tests/test_dependabot_coverage.py);
- no workflow pins an npm package inline again — the shape that hid the
  stale pins for a year;
- the flat config carries the five rules `.eslintrc.json` carried, with the
  one option ESLint 9 changed spelled out;
- the flat config still reads its environment, parser options and cross-file
  globals from where `.eslintrc.json` left them. The ONE deliberate
  behavioural difference in the migration is the browser environment, and
  pinning `globals` to an exact version is what pins it: a bump that moves
  the set has to be a reviewed diff, not a silent widening;
- the JS coverage gate still folds with c8 and still reads one decimal off
  `total.lines.pct`, against the committed javascript floor.

`npm ci` is not exercised here — this file must pass with no `node_modules`
(the pytest leg runs before either workflow installs). What CI actually
installs and runs IS the check: eslint over `src/**` on every push, and the
c8 fold gating the javascript ratchet.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_JSON = REPO_ROOT / "package.json"
PACKAGE_LOCK = REPO_ROOT / "package-lock.json"
FLAT_CONFIG = REPO_ROOT / "eslint.config.mjs"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# The tools the two gates need, and the exact version each is pinned at.
# Exact versions only: this repo pins deliberately (dependabot.yml's own
# header) so a tool release can never turn CI red on an unchanged commit.
# The values are the current supported releases as of issue #361:
# `npm view eslint version` -> 10.11.0, `npm view c8 version` -> 12.0.0.
# eslint-plugin-react's latest is unchanged at 7.37.5 and `globals` is what
# the flat config reads the browser environment from.
EXPECTED_DEV_DEPENDENCIES = {
    "c8": "12.0.0",
    "eslint": "10.11.0",
    "eslint-plugin-react": "7.37.5",
    "globals": "17.12.0",
}

# The rule set `.eslintrc.json` carried, rule for rule. The one change is
# `no-unused-vars`'s `caughtErrors`, whose DEFAULT ESLint 9 flipped from
# "none" to "all"; without spelling the old default out the gate reports
# every `catch (e)` the tree writes on purpose (four in src/app.jsx) and
# nothing that file did not report before.
EXPECTED_RULES = {
    "no-undef": "error",
    "no-unused-vars": ["error", {"caughtErrors": "none"}],
    "react/jsx-no-undef": ["error", {"allowGlobals": True}],
    "react/jsx-uses-vars": "error",
    "react/jsx-uses-react": "error",
}

# How many names the config declares as cross-file globals. The count, not
# the list: every name is checked against the tree by
# `test_flat_config_keeps_the_cross_file_globals`, so a stale one fails there
# and a dropped or added one fails here.
EXPECTED_CROSS_FILE_GLOBALS = 98

# `.jsx` is not a default lint target in flat config, so the files glob has
# to name it or the gate's own `src/**/*.jsx` argument matches nothing and
# eslint exits 2 ("all of the files matching the glob pattern … are
# ignored") — loud, but only after it has stopped linting the panels.
EXPECTED_LINT_GLOBS = ["**/*.js", "**/*.jsx"]

# An inline `<pkg>@<version>` on any npm command a workflow runs — the shape
# issue #361 is about, and it is invisible to every update mechanism. The
# command names are the ecosystem's own spellings, all of them: `install`, the
# `npm i` short form, `ci`, `add`, and `npx pkg@version` (which downloads
# without touching the manifest, so a bump of it is even less visible). A body
# is joined across backslash-continuations first, because splitting one install
# across two lines is the same regression one line lower.
_INLINE_NPM_PIN = re.compile(r"(?:\bnpm\s+(?:install|i|ci|add)\b|\bnpx\b)[^\n]*@\d")
_LINE_CONTINUATION = re.compile(r"\\\s*\n")


def _run_bodies(doc: dict) -> list[str]:
    """Every `run:` body, continuation-joined, the way the shell reads it."""
    return [_LINE_CONTINUATION.sub(" ", run) for run in _step_runs(doc)]


def _package() -> dict:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))


def _workflow(name: str) -> dict:
    doc = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return doc or {}


def _step_runs(doc: dict) -> list[str]:
    """Every `run:` body of every job, decoded the way the runner reads it."""
    runs: list[str] = []
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            run = (step or {}).get("run")
            if run:
                runs.append(run)
    return runs


def _flat_rules() -> dict:
    """The `rules: {…}` block of the flat config, as data.

    Read from the source rather than executed: node is not a prerequisite
    of this suite (tests/test_ci_gate_modules.py's own convention), and the
    block is a flat literal of strings and booleans. Each line is
    `'name': value,`; the values are JSON once the single quotes around
    them become double quotes.
    """
    text = FLAT_CONFIG.read_text(encoding="utf-8")
    block = re.search(r"^\s{4}rules: \{(.*?)^\s{4}\},", text, re.S | re.M)
    assert block, (
        f"{FLAT_CONFIG.name}: no `rules: {{…}}` block at four-space indent; "
        "the rule set this test pins could not be read, so a rewrite that "
        "dropped or renamed a rule would pass the suite")
    rules: dict = {}
    for line in block.group(1).splitlines():
        entry = re.match(r"^\s*'([^']+)':\s*(.+?),?\s*$", line)
        if not entry:
            continue
        name, value = entry.groups()
        rules[name] = json.loads(value.replace("'", '"'))
    return rules


def test_ci_tools_are_pinned_to_exact_versions() -> None:
    r"""No range, no tilde, no caret: a release cannot move CI under us.

    Equality against the expected map does the whole job — every value it
    accepts is an exact `\d+.\d+.\d+` by construction, so a separate shape
    loop would be a second guard that cannot fail. Editing
    EXPECTED_DEV_DEPENDENCIES to a range is a reviewed diff, and the
    environment assertions below are what make the `globals` entry there
    load-bearing.
    """
    dev = _package().get("devDependencies") or {}
    assert dev == EXPECTED_DEV_DEPENDENCIES, (
        "package.json devDependencies drifted from the pinned toolchain: "
        f"{dev!r} (expected {EXPECTED_DEV_DEPENDENCIES!r})")


def test_manifest_is_private_and_carries_no_runtime_dependencies() -> None:
    """The frontend still has no npm dependency (CONTRIBUTING.md).

    This manifest exists for CI tools only. A `dependencies` block — or a
    non-private manifest, which npm would let be published — is the bundler
    creep that rule forbids wearing a toolchain's clothes.
    """
    package = _package()
    assert package.get("private") is True, (
        "package.json is not private; nothing here is ever published, and "
        "the manifest must say so")
    assert "dependencies" not in package, (
        "package.json grew runtime dependencies; the frontend's React is "
        "loaded from a CDN and has no npm dependency by design")


def test_peer_override_pins_the_react_plugin_to_our_eslint() -> None:
    """eslint-plugin-react 7.37.5's peer range stops at eslint ^9.7.

    Every eslint 9.x is npm-deprecated and 10.x is outside the plugin's
    declared range, so a bare install is an ERESOLVE. The `overrides` entry
    resolves it declaratively, in the file Dependabot owns, so a future
    eslint bump needs no workflow edit: `$eslint` follows the root pin.
    Without it, `npm ci` refuses to install and BOTH js gates go red.
    """
    overrides = _package().get("overrides") or {}
    assert (overrides.get("eslint-plugin-react") or {}).get("eslint") == "$eslint", (
        "package.json lost the eslint-plugin-react peer override; npm ci "
        "fails with ERESOLVE without it (peer range ^3 || … || ^9.7 against "
        "a supported eslint 10.x)")


def test_the_lockfile_is_committed_and_matches_the_manifest() -> None:
    """`npm ci` installs the lockfile, not the manifest.

    A lockfile that is missing, or that names a different eslint than
    package.json pins, is the drift Dependabot cannot fix: npm ci refuses
    to install rather than resolve.
    """
    assert PACKAGE_LOCK.is_file(), (
        f"{PACKAGE_LOCK.name} is not committed; npm ci needs it and "
        "Dependabot bumps it in step with package.json")
    lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
    assert lock.get("lockfileVersion") >= 3, (
        "package-lock.json is not a modern lockfile; npm ci on this npm "
        "would not install from it")
    root = (lock.get("packages") or {}).get("") or {}
    assert root.get("devDependencies") == _package()["devDependencies"], (
        "package-lock.json's root devDependencies differ from "
        "package.json's; npm ci refuses a lockfile out of step with the "
        "manifest it was resolved from")
    # The root block is what Dependabot edits; the resolved node entries are
    # what npm actually installs. A lockfile whose root names one version and
    # whose node resolves another is the drift `npm ci` refuses — asserted
    # here so the refusal is a review finding rather than a red gate.
    for name, version in _package()["devDependencies"].items():
        node = (lock.get("packages") or {}).get(f"node_modules/{name}") or {}
        assert node.get("version") == version, (
            f"package-lock.json resolves {name} to {node.get('version')!r}, "
            f"not the pinned {version!r}; npm ci installs the resolved node, "
            "not the root block's pin")


def test_exactly_one_npm_entry_owns_the_manifest() -> None:
    """One owner per file, the way tests/test_dependabot_coverage.py holds
    the pip requirements files. A second npm entry double-opens every bump,
    which is how boto3 opened #164 and #165 sixteen seconds apart (#176).
    """
    doc = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8")) or {}
    npm_entries = [entry for entry in (doc.get("updates") or [])
                   if entry.get("package-ecosystem") == "npm"]
    assert len(npm_entries) == 1, (
        f"expected exactly one npm update entry, found {len(npm_entries)}: "
        f"{[e.get('directory') for e in npm_entries]}")
    directory = str(npm_entries[0].get("directory") or "").strip("/")
    assert directory == "", (
        f"the npm entry watches {directory!r}, which holds no package.json; "
        "the toolchain manifest is at the repository root")


def test_no_workflow_pins_an_npm_package_inline() -> None:
    """The shape issue #361 is about, wherever it reappears.

    Every workflow, both extensions: `.github/workflows` holds `*.yml` today
    and nothing stops the next workflow arriving as `*.yaml`, where a
    `*.yml`-only glob would read the tree as clean.
    """
    offenders = []
    paths = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    assert paths, "no workflow found; the scan would pass on an empty tree"
    for path in paths:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for run in _run_bodies(doc):
            for match in _INLINE_NPM_PIN.finditer(run):
                offenders.append(f"{path.name}: {match.group(0).strip()}")
    assert not offenders, (
        "a workflow installs an npm package with an inline @version, which "
        "no update mechanism can see (issue #361): " + "; ".join(offenders))


def test_both_js_gates_install_from_the_lockfile() -> None:
    """`npm ci` as the step's own command, not a word somewhere in it.

    `npm install` resolves against the registry and can rewrite the tree
    under a pinned manifest; `npm ci` installs the lockfile exactly, which
    is what makes the pin in package.json mean something. Matching the whole
    body for the substring would be satisfied by a comment or an `echo` in an
    unrelated step, so this reads the first command of the run.
    """
    for name in ("eslint.yml", "tests.yml"):
        commands = []
        for run in _run_bodies(_workflow(name)):
            commands += [line.strip() for line in run.splitlines() if line.strip()]
        assert any(re.match(r"^npm\s+ci\b", command) for command in commands), (
            f"{name} has no `npm ci` command; the toolchain must be installed "
            "from package-lock.json, not resolved at run time")


def test_eslint_runs_through_npx_no_install() -> None:
    """The documented local command must keep working unchanged.

    CONTRIBUTING.md and AGENTS.md both tell a contributor to run
    `npx --no-install eslint 'src/**/*.js' 'src/**/*.jsx'`. `--no-install`
    resolves the binary from node_modules, so it only works if the install
    landed at the repository root — the one place the documented command
    looks.
    """
    runs = _step_runs(_workflow("eslint.yml"))
    lint_steps = [run for run in runs if "eslint" in run and "npm ci" not in run]
    assert lint_steps, "eslint.yml has no lint step"
    assert any("npx --no-install eslint" in run for run in lint_steps), (
        "eslint.yml no longer runs `npx --no-install eslint`; the local "
        "command CONTRIBUTING.md documents must match the gate's")


def test_flat_config_keeps_the_eslintrc_rule_set() -> None:
    """The same five rules, with the same options, as `.eslintrc.json`.

    Read from the source by a line regex, so it proves what that reader can
    see: the first four-space `rules:` block, and only the entries shaped
    `'name': value,`. A spread, a second config object, or a double-quoted
    key would change the resolved rule set without changing what this reads.
    `tests/test_js_toolchain.py` cannot close that gap without node, which
    this suite does not require; the eslint gate over `src/**` is the
    backstop. The browser ENVIRONMENT is pinned separately, and is the one
    deliberate difference from `.eslintrc.json`.
    """
    rules = _flat_rules()
    assert rules == EXPECTED_RULES, (
        "the flat config's rule set drifted from the one .eslintrc.json "
        f"carried: {rules!r} (expected {EXPECTED_RULES!r})")


def test_flat_config_still_supplies_the_browser_environment() -> None:
    """`no-undef` has to know which names need no declaration.

    `.eslintrc.json` got that from `env: {browser: true}`, which eslint
    8.57.1 expanded through the globals@13.24.0 it bundles. The flat config
    gets it from `globals.browser` at whatever version is pinned — 17.12.0
    today, a DIFFERENT set: 464 names added, 23 removed, measured by name in
    both directions (see the config's own header).

    So the environment is pinned TRANSITIVELY, and that is worth pinning
    explicitly: the exact-version assertion in
    `test_ci_tools_are_pinned_to_exact_versions` is what freezes the set,
    and this test is what fails if a `globals` bump moves it. A Dependabot PR
    that changes these 1204 names is a change a reviewer should read, not one
    that arrives silently with a green gate.
    """
    text = FLAT_CONFIG.read_text(encoding="utf-8")
    assert "...globals.browser" in text, (
        f"{FLAT_CONFIG.name} no longer spreads globals.browser into "
        "languageOptions.globals; every browser global becomes an undefined "
        "identifier and the gate reports the whole tree")
    assert EXPECTED_DEV_DEPENDENCIES["globals"], (
        "the globals pin is gone, so the browser environment above is "
        "whatever npm resolves at install time")


def test_flat_config_keeps_the_parser_options() -> None:
    """`sourceType`, `ecmaVersion` and JSX parsing, all three load-bearing.

    Flat config's defaults are not eslintrc's: `sourceType` defaults to
    `module`, so a config that drops it parses every classic script in
    `src/` as a module. JSX parsing is off unless `ecmaFeatures.jsx` is set,
    and `ecmaVersion` is what supplies the ES built-ins (it is also what
    keeps `Intl` defined, the one browser-set name this migration drops).
    """
    text = FLAT_CONFIG.read_text(encoding="utf-8")
    for pattern, what in (
        (r"ecmaVersion:\s*2024", "ecmaVersion 2024 (the ES built-ins)"),
        (r"sourceType:\s*'script'", "sourceType 'script' (src/ are classic scripts)"),
        (r"ecmaFeatures:\s*\{\s*jsx:\s*true\s*\}", "ecmaFeatures.jsx (every panel is .jsx)"),
    ):
        assert re.search(pattern, text), (
            f"{FLAT_CONFIG.name} no longer sets {what}; flat config's default "
            "differs from eslintrc's, so dropping it changes what the gate parses")


def _declared_cross_file_globals() -> set[str]:
    text = FLAT_CONFIG.read_text(encoding="utf-8")
    block = re.search(r"const CROSS_FILE_GLOBALS = \{(.*?)^\};", text, re.S | re.M)
    assert block, (
        f"{FLAT_CONFIG.name}: no CROSS_FILE_GLOBALS block; the names the src "
        "tree defines for itself would be undefined identifiers")
    return set(re.findall(r"^\s*'?([A-Za-z_$][\w$]*)'?:", block.group(1), re.M))


def _names_the_tree_defines() -> set[str]:
    """Every name `src/` or `public/index.html` defines at the top level.

    `index.html` loads each `/src/*` file as a classic script, so a top-level
    `function`, `const`, `let` or `var` there is a global, and so is anything
    a file assigns to `window`. React and ReactDOM come from the CDN tags.
    """
    defined: set[str] = set()
    sources = list((REPO_ROOT / "src").rglob("*.js")) \
        + list((REPO_ROOT / "src").rglob("*.jsx")) \
        + [REPO_ROOT / "public" / "index.html"]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        defined |= set(re.findall(r"^function ([A-Za-z_$][\w$]*)", text, re.M))
        defined |= set(re.findall(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)", text, re.M))
        defined |= set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", text))
    return defined | {"React", "ReactDOM"}


def test_flat_config_keeps_the_cross_file_globals() -> None:
    """The list names nothing the tree stopped defining, and misses nothing.

    Two properties, both derived from the tree rather than restated here: no
    name is declared that `src/` or `public/index.html` no longer defines
    (a stale one is dead weight the next reader cannot check), and the list
    is exactly EXPECTED_CROSS_FILE_GLOBALS long — so a name dropped from the
    config fails on the count, and a name added to it fails on the count too.

    The list is a hand-maintained declaration, not a mechanical projection of
    the tree: `src/` defines 127 top-level functions and CROSS_FILE_GLOBALS
    names 98, because a name only needs declaring once another file
    references it bare. The enforcement is the gate — `no-undef` reports a
    cross-file reference the list is missing — not this test, which checks
    the list's two ends rather than the whole resolution.
    """
    declared = _declared_cross_file_globals()
    stale = sorted(declared - _names_the_tree_defines())
    assert not stale, (
        "CROSS_FILE_GLOBALS declares names nothing in src/ or "
        f"public/index.html defines any more: {stale}")
    assert len(declared) == EXPECTED_CROSS_FILE_GLOBALS, (
        f"CROSS_FILE_GLOBALS holds {len(declared)} names, not "
        f"{EXPECTED_CROSS_FILE_GLOBALS}; adding or dropping a cross-file "
        "global changes what no-undef accepts across the whole tree, so it "
        "belongs in a reviewed diff")


def test_flat_config_lints_js_and_jsx() -> None:
    """Flat config lints only .js/.cjs/.mjs by default.

    Every panel is .jsx, so an unlisted extension leaves the whole panel
    half of the tree unlinted; the gate then fails on its own `src/**/*.jsx`
    argument with exit 2 rather than reporting a clean run.
    """
    text = FLAT_CONFIG.read_text(encoding="utf-8")
    match = re.search(r"^\s{4}files: \[(.*?)\],", text, re.M)
    assert match, f"{FLAT_CONFIG.name}: no `files: [...]` entry"
    globs = re.findall(r"'([^']+)'", match.group(1))
    assert globs == EXPECTED_LINT_GLOBS, (
        f"the files glob is {globs!r}, not {EXPECTED_LINT_GLOBS!r}; .jsx "
        "is not a default flat-config target, so dropping it stops the gate "
        "linting every panel")


def test_js_coverage_gate_still_reports_one_decimal() -> None:
    """SV-CI-RATCHETS' javascript floor, fed the same number.

    `coverage-summary.json`'s `total.lines.pct` is raw; the gate rounds it
    to the one decimal the ratchet records and compares against the
    committed floor. The bump to c8 12 measured byte-identically against
    c8 10 (same per-file percentages, same total), so the recorded
    `measured` stays valid and no floor moves.
    """
    runs = _step_runs(_workflow("tests.yml"))
    measured = [run for run in runs if "total.lines.pct" in run]
    assert measured, (
        "tests.yml no longer reads total.lines.pct out of "
        "coverage-summary.json; the javascript ratchet has no measurement")
    assert any("toFixed(1)" in run for run in measured), (
        "the javascript coverage measurement is no longer rounded to one "
        "decimal, which is the format .github/ci-thresholds.json records")
    assert any("--check-coverage" in run and "--lines" in run for run in runs), (
        "the JavaScript coverage gate no longer checks itself against the "
        "committed javascript floor")
