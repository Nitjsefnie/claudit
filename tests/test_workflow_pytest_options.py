"""Every pytest command in a workflow or composite action reports durations and dumps a hung test's stacks.

A `run:` command, as test_workflow_fetch_retry.py's lexer splits it, is a
pytest command when any word of it, whatever the word's role, is `pytest`
or `py.test` after its last `/` or after its first `m` (`-mpytest`). It
fails without `--durations` (`=N` or ` N`) or `-o faulthandler_timeout=N`,
N in digits above 0, and with `no:faulthandler` in a word (that `-p`
value turns the dump off). Every word naming faulthandler_timeout must be
that pair: pytest takes a name's last override, so `-oNAME=N`,
`--override-ini` or a 0 anywhere fails.

A command that only names pytest so is judged too and fails closed:
`pip install pytest`, an assignment, alias or option holding its path.

Unseen: a pytest no word names so (`pytest.exe`, `-m pytest.__main__`,
`PY=pytest`, a `${{ }}` expression, a script that imports or starts it),
a quoted string's inner words, and the use of a value: options at a
variable's, alias's or function's call do not count. Not read: the
environment outside the command's own words (PYTEST_ADDOPTS exported, in
an `env:` or `$GITHUB_ENV`), configuration files, `--durations-min`, the
job's time limit.
"""
from __future__ import annotations

import pytest

from tests.test_workflow_fetch_retry import _commands, _steps
from tests.workflow_checks import ROOT

PYTEST = {"pytest", "py.test"}
STALL = "faulthandler_timeout"
NO_DUMP = "pytest without `-o faulthandler_timeout=N`, N in digits above 0"
NO_DURATIONS = "pytest without `--durations`"
NO_PLUGIN = "pytest with `no:faulthandler`, which turns the dump off"
KNOWN = {(".github/workflows/tests.yml", "pytest-shards"),
         (".github/workflows/tests.yml", "pytest-thresholds"),
         (".github/workflows/tests.yml", "pytest-portable"),
         (".github/workflows/refresh-pricing.yml", "suite"),
         (".github/workflows/ci-gate.yml", "setup-docs"),
         (".github/actions/coverage-gate/action.yml", "composite")}


def _armed(word: str) -> bool:
    name, _, value = word.partition("=")
    return name == STALL and value.isdecimal() and int(value) > 0


def _lacks(words: list[str]) -> list[str]:
    named = [(before, word) for before, word in zip([""] + words, words) if STALL in word]
    dumps = bool(named) and all(before == "-o" and _armed(word) for before, word in named)
    timed = any(word == "--durations" or word.startswith("--durations=") for word in words)
    blocked = any("no:faulthandler" in word for word in words)
    return [NO_DUMP] * (not dumps) + [NO_PLUGIN] * blocked + [NO_DURATIONS] * (not timed)


def _is_pytest(word: str) -> bool:
    return word.rsplit("/", 1)[-1] in PYTEST or word.partition("m")[2] in PYTEST


def _pytest_commands(script: str) -> list[tuple[list[str], list[str]]]:
    """(words, what they lack) per pytest command in a `run:` script."""
    return [(words, _lacks(words)) for words, _ in _commands(script) if any(map(_is_pytest, words))]


def _lacking(script: str) -> list[list[str]]:
    return [lacks for _, lacks in _pytest_commands(script)]


BOTH = "-o faulthandler_timeout=240 --durations=20"
RUN = "python -m pytest tests -q "

ACCEPTED = {
    "both options": RUN + BOTH,
    "under coverage": RUN + "--tb=short -ra " + BOTH + " --cov=backend --cov-report=",
    "files from a substitution": "python -m pytest $(cat scripts/ci/node_test_files.txt) -q --no-header " + BOTH,
    "exec, a marker expression": 'exec python -m pytest $files -q -m "not db" ' + BOTH + " --junitxml=pytest-junit.xml",
    "durations as two words": RUN + "-o faulthandler_timeout=240 --durations 20",
    "the durations of every test": RUN + "-o faulthandler_timeout=240 --durations=0",
    "a quoted override": RUN + '-o "faulthandler_timeout=240" --durations=20',
    "another override beside it": RUN + "-o cache_dir=cache " + BOTH,
    "another plugin off": RUN + "-p no:cacheprovider " + BOTH,
    "a plugin name that blocks nothing": RUN + "-p no:_pytest.faulthandler " + BOTH,
    "the timeout twice": RUN + "-o faulthandler_timeout=60 " + BOTH,
    "bare pytest": "pytest tests -q " + BOTH,
    "pytest by path": ".venv/bin/pytest tests -q " + BOTH,
    "py.test": "py.test tests -q " + BOTH,
    "the module joined to -m": "python -mpytest tests -q " + BOTH,
    "the module joined to a cluster": "python -Impytest tests -q " + BOTH,
    "under another runner": "coverage run -m pytest tests -q " + BOTH,
    "a pathed option value": "docker run --entrypoint=/usr/bin/pytest img tests -q " + BOTH,
    "in a function body": "run() { " + RUN + BOTH + "; }\nrun",
    "an unquoted eval": "eval " + RUN + BOTH,
    "across a continuation": "python -m pytest tests -q \\\n  " + BOTH,
    "inside a substitution": 'out="$(' + RUN + BOTH + ')"',
    "the plugin off in an exported PYTEST_ADDOPTS, not read": 'export PYTEST_ADDOPTS="-p no:faulthandler"\n' + RUN + BOTH,
}

NOT_PYTEST = {
    "the shard lister": "python3 scripts/ci/pytest_shards.py --shard 1 --of 4",
    "a report named after pytest": "cat pytest-junit.xml",
    "a comment": "# python -m pytest tests -q",
    "a quoted message": 'echo "python -m pytest tests -q"',
    "a bare -m": "python -m coverage report -m",
    "a module named mpytest": "python -mmpytest tests -q",
    "a longer name ending in pytest": "scripts/ci/retry.sh pip download flake8-pytest",
}

UNSEEN = {
    "a bash -c string": "bash -c '" + RUN.strip() + "'",
    "a quoted eval": 'eval "' + RUN.strip() + '"',
    "a variable's value": "PY=pytest\n$PY tests -q",
    "a quoted variable's value": 'CMD="' + RUN.strip() + '"\n$CMD',
    "an alias": "alias t=pytest\nt tests -q",
    "a bare option value": "docker run --entrypoint=pytest img tests -q",
    "an expression": "python -m ${{ inputs.module }} tests -q",
    "a write to GITHUB_ENV": 'echo "PYTEST_ADDOPTS=-p no:faulthandler" >> "$GITHUB_ENV"',
    "a suffixed executable": "pytest.exe tests -q",
    "a module under pytest": "python -m pytest.__main__ tests -q",
    "a program importing pytest": "python -c 'import sys, pytest; sys.exit(pytest.main())'",
    "a script that launches it": "python scripts/ci/perturbed_leg_bench.py --seed 1",
}

REFUSED = {
    "neither option": (RUN.strip(), [[NO_DUMP, NO_DURATIONS]]),
    "no timeout": (RUN + "--durations=20", [[NO_DUMP]]),
    "no durations": (RUN + "-o faulthandler_timeout=240", [[NO_DURATIONS]]),
    "timeout 0": (RUN + "-o faulthandler_timeout=0 --durations=20", [[NO_DUMP]]),
    "timeout with a sign": (RUN + "-o faulthandler_timeout=+240 --durations=20", [[NO_DUMP]]),
    "timeout with a unit": (RUN + "-o faulthandler_timeout=240s --durations=20", [[NO_DUMP]]),
    "timeout in a variable": (RUN + '-o "faulthandler_timeout=$LIMIT" --durations=20', [[NO_DUMP]]),
    "timeout without a value": (RUN + "-o faulthandler_timeout --durations=20", [[NO_DUMP]]),
    "a later 0": (RUN + BOTH + " -o faulthandler_timeout=0", [[NO_DUMP]]),
    "an earlier 0": (RUN + "-o faulthandler_timeout=0 " + BOTH, [[NO_DUMP]]),
    "a later override spelled another way": (RUN + BOTH + " --override-ini=faulthandler_timeout=0", [[NO_DUMP]]),
    "the long option": (RUN + "--override-ini faulthandler_timeout=240 --durations=20", [[NO_DUMP]]),
    "the long option, joined": (RUN + "--override-ini=faulthandler_timeout=240 --durations=20", [[NO_DUMP]]),
    "the short option, joined": (RUN + "-ofaulthandler_timeout=240 --durations=20", [[NO_DUMP]]),
    "no -o before it": (RUN + "faulthandler_timeout=240 --durations=20", [[NO_DUMP]]),
    "another option's name": (RUN + "-o my_faulthandler_timeout=240 --durations=20", [[NO_DUMP]]),
    "the faulthandler plugin off": (RUN + BOTH + " -p no:faulthandler", [[NO_PLUGIN]]),
    "the faulthandler plugin off, joined": (RUN + BOTH + " -pno:faulthandler", [[NO_PLUGIN]]),
    "the plugin off in an inline PYTEST_ADDOPTS": ('PYTEST_ADDOPTS="-p no:faulthandler" ' + RUN + BOTH, [[NO_PLUGIN]]),
    "only a minimum duration": (RUN + "-o faulthandler_timeout=240 --durations-min=1", [[NO_DURATIONS]]),
    "an install naming pytest": ("scripts/ci/retry.sh pip install pytest", [[NO_DUMP, NO_DURATIONS]]),
    "the module joined to -m, no durations": ("python -mpytest tests -q -o faulthandler_timeout=240", [[NO_DURATIONS]]),
    "a pathed assignment, options at its use": ("PY=.venv/bin/pytest\n$PY tests -q " + BOTH, [[NO_DUMP, NO_DURATIONS]]),
    "a substituted assignment, options at its use": ("PY=$(command -v pytest)\n$PY tests -q " + BOTH, [[NO_DUMP, NO_DURATIONS]]),
    "an array, options at its use": ("cmd=(" + RUN.strip() + ')\n"${cmd[@]}" ' + BOTH, [[NO_DUMP, NO_DURATIONS]]),
    "a pathed alias, options at its use": ("alias t=.venv/bin/pytest\nt tests -q " + BOTH, [[NO_DUMP, NO_DURATIONS]]),
    "a quoted string of one word": ("bash -c 'pytest'", [[NO_DUMP, NO_DURATIONS]]),
    "the module flag in an assignment": ("FLAGS=-mpytest\npython $FLAGS tests -q " + BOTH, [[NO_DUMP, NO_DURATIONS]]),
    "options on a function's call": ('run() { python -m pytest "$@"; }\nrun tests ' + BOTH, [[NO_DUMP, NO_DURATIONS]]),
    "the second of two commands": (RUN + BOTH + "\n" + RUN + "--durations=20", [[], [NO_DUMP]]),
    "options on the next command": (RUN.strip() + "\necho " + BOTH, [[NO_DUMP, NO_DURATIONS]]),
}


@pytest.mark.parametrize("script", ACCEPTED.values(), ids=ACCEPTED)
def test_a_pytest_command_with_both_options_passes(script):
    assert _lacking(script) == [[]]


@pytest.mark.parametrize("script", NOT_PYTEST.values(), ids=NOT_PYTEST)
def test_a_command_that_only_names_pytest_is_not_judged(script):
    assert _lacking(script) == []


@pytest.mark.parametrize("script", UNSEEN.values(), ids=UNSEEN)
def test_a_pytest_the_docstring_calls_unseen_is_not_judged(script):
    assert _lacking(script) == []


@pytest.mark.parametrize("script,lacks", REFUSED.values(), ids=REFUSED)
def test_a_pytest_command_without_an_option_is_refused(script, lacks):
    assert _lacking(script) == lacks


def test_every_workflow_pytest_command_reports_durations_and_dumps_a_hung_test():
    violations, sites = [], set()
    for path, job, step, _ in _steps():
        for words, lacks in _pytest_commands(step.get("run") or ""):
            where = path.relative_to(ROOT).as_posix()
            sites.add((where, job))
            violations += [f"{where}: {job}: {reason}: {' '.join(words)}" for reason in lacks]
    assert KNOWN <= sites, f"pytest commands no longer read: {sorted(KNOWN - sites)}"
    assert not violations, "\n".join(violations)
