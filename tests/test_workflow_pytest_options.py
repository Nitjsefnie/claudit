"""Every pytest command in a workflow or composite action reports durations and dumps a hung test's stacks.

A `run:` command, split by test_workflow_fetch_retry.py's lexer, runs
pytest when a word is `pytest` or `py.test`, bare, by path or joined to an
option cluster's `m` (`-mpytest`); `pip install pytest` counts too. It
fails without `--durations` (`=N` or ` N`) or `-o faulthandler_timeout=N`,
N whole seconds above 0. Every word naming faulthandler_timeout must be
that pair: pytest takes a name's last override, so `-oNAME=N`,
`--override-ini` or a 0 anywhere fails.

Not read: a pytest another program launches or a python program imports;
one inside a longer word, a quoted string (`bash -c`, a quoted eval) or an
assignment (a variable, an alias); a suffixed name (`pytest.exe`,
`-m pytest.__main__`); a function call's arguments (its body is read);
`-p no:faulthandler`, the step's `env:`, configuration files,
`--durations-min`; whether the job outlasts the dump.
"""
from __future__ import annotations

import pytest

from tests.test_workflow_fetch_retry import _commands, _steps
from tests.workflow_checks import ROOT

PYTEST = {"pytest", "py.test"}
STALL = "faulthandler_timeout"
NO_DUMP = "pytest without `-o faulthandler_timeout=N`, N whole seconds above 0"
NO_DURATIONS = "pytest without `--durations`"
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
    return [NO_DUMP] * (not dumps) + [NO_DURATIONS] * (not timed)


def _is_pytest(word: str) -> bool:
    return (word.partition("m")[2] if word.startswith("-") else word.rsplit("/", 1)[-1]) in PYTEST


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
    "the timeout twice": RUN + "-o faulthandler_timeout=60 " + BOTH,
    "bare pytest": "pytest tests -q " + BOTH,
    "pytest by path": ".venv/bin/pytest tests -q " + BOTH,
    "py.test": "py.test tests -q " + BOTH,
    "the module joined to -m": "python -mpytest tests -q " + BOTH,
    "the module joined to a cluster": "python -Impytest tests -q " + BOTH,
    "under another runner": "coverage run -m pytest tests -q " + BOTH,
    "in a function body": "run() { " + RUN + BOTH + "; }\nrun",
    "an unquoted eval": "eval " + RUN + BOTH,
    "across a continuation": "python -m pytest tests -q \\\n  " + BOTH,
    "inside a substitution": 'out="$(' + RUN + BOTH + ')"',
}

NOT_PYTEST = {
    "the shard lister": "python3 scripts/ci/pytest_shards.py --shard 1 --of 4",
    "a report named after pytest": "cat pytest-junit.xml",
    "a comment": "# python -m pytest tests -q",
    "a quoted message": 'echo "python -m pytest tests -q"',
    "a bare -m": "python -m coverage report -m",
    "a longer name ending in pytest": "scripts/ci/retry.sh pip download flake8-pytest",
}

UNSEEN = {
    "a bash -c string": "bash -c '" + RUN.strip() + "'",
    "a quoted eval": 'eval "' + RUN.strip() + '"',
    "a variable's value": "PY=pytest\n$PY tests -q",
    "a quoted variable's value": 'CMD="' + RUN.strip() + '"\n$CMD',
    "an alias": "alias t=pytest\nt tests -q",
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
    "only a minimum duration": (RUN + "-o faulthandler_timeout=240 --durations-min=1", [[NO_DURATIONS]]),
    "an install naming pytest": ("scripts/ci/retry.sh pip install pytest", [[NO_DUMP, NO_DURATIONS]]),
    "the module joined to -m, no durations": ("python -mpytest tests -q -o faulthandler_timeout=240", [[NO_DURATIONS]]),
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
def test_a_pytest_the_docstring_calls_unread_is_not_judged(script):
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
