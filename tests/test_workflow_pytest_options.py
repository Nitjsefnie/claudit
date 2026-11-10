"""Every pytest command in a workflow or composite action reports durations and dumps a hung test's stacks.

Fails on a `run:` command with the word `pytest` or `py.test`, bare or by
path (`pip install pytest` too), that lacks `--durations`, as
`--durations=N` or `--durations N`, or `-o faulthandler_timeout=N`, N whole
seconds above 0. Every word naming faulthandler_timeout must be that pair:
pytest takes a name's last override, so another spelling (`-oNAME=N`,
`--override-ini`) or a 0 anywhere fails.

Not read: a pytest a script launches or one in a `bash -c` string, an
eval, a variable or a function; `-p no:faulthandler`, `--durations-min`,
PYTEST_ADDOPTS, configuration files; whether the job outlasts the dump.
Commands are split by test_workflow_fetch_retry.py's lexer.
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


def _pytest_commands(script: str) -> list[tuple[list[str], list[str]]]:
    """(words, what they lack) per pytest command in a `run:` script."""
    return [(words, _lacks(words)) for words, _ in _commands(script)
            if any(word.rsplit("/", 1)[-1] in PYTEST for word in words)]


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
    "across a continuation": "python -m pytest tests -q \\\n  " + BOTH,
    "inside a substitution": 'out="$(' + RUN + BOTH + ')"',
}

NOT_PYTEST = {
    "the shard lister": "python3 scripts/ci/pytest_shards.py --shard 1 --of 4",
    "a report named after pytest": "cat pytest-junit.xml",
    "a comment": "# python -m pytest tests -q",
    "a quoted message": 'echo "python -m pytest tests -q"',
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
    "the second of two commands": (RUN + BOTH + "\n" + RUN + "--durations=20", [[], [NO_DUMP]]),
    "options on the next command": (RUN.strip() + "\necho " + BOTH, [[NO_DUMP, NO_DURATIONS]]),
}


@pytest.mark.parametrize("script", ACCEPTED.values(), ids=ACCEPTED)
def test_a_pytest_command_with_both_options_passes(script):
    assert _lacking(script) == [[]]


@pytest.mark.parametrize("script", NOT_PYTEST.values(), ids=NOT_PYTEST)
def test_a_command_that_only_names_pytest_is_not_judged(script):
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
