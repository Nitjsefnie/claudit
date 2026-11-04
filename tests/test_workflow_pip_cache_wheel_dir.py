"""Tests of the wheel directory, write and audit rules of test_workflow_pip_cache."""
from __future__ import annotations

import pytest

import tests.test_workflow_pip_cache as workflow
from tests.test_workflow_pip_cache import (
    AUDIT, BACKEND_INSTALL, BARE_AUDIT, DEV_AUDIT, EXPRESSION_DIR, HASHED_PYYAML, PREPARED, WHEEL, WHEEL_DIR,
    WRAPPED_AUDIT, _backend_sites, _chain, _commands, _env_audit, _install_sites, _lines, _names,
    _sites_without_hashed_pyyaml_first, _step, _wheel_dir, _write_workflow,
)


OTHER_DIR = '"$RUNNER_TEMP/other"'
COPY = f"cp evil.whl {WHEEL_DIR}/"
WRITES = [
    "pip wheel --no-binary=PyYAML pyyaml==6.0.3 -w " + WHEEL_DIR,
    WHEEL.replace("pyyaml.txt", "test.txt"),
    f"pip download -d {WHEEL_DIR} pyyaml==6.0.3",
    COPY,
    COPY + "  # stage the wheel",
    f"mv evil.whl {WHEEL_DIR}",
    f"echo x >{WHEEL_DIR}/evil.whl",
    f'cp evil.whl "{EXPRESSION_DIR}/"',
    WHEEL.replace(WHEEL_DIR, OTHER_DIR) + " --cache-dir " + WHEEL_DIR,
    WHEEL.replace(WHEEL_DIR, OTHER_DIR) + f" --log {WHEEL_DIR}/pip.log",
]
SIBLINGS = ("pyyaml-wheel-other", "pyyaml-wheel.bak", "pyyaml-wheel2", "pyyaml-wheel_x", "PyYAML-wheel", "other")
HARMLESS = ["echo ok", f"  # cp evil.whl {WHEEL_DIR}/"] + [f'cp evil.whl "$RUNNER_TEMP/{name}/"' for name in SIBLINGS]


def _wheel(flag: str, name: str = "pyyaml-wheel") -> str:
    return WHEEL.replace(f" -w {WHEEL_DIR}", f" {flag}\"$RUNNER_TEMP/{name}\"")


def _judged(tmp_path, monkeypatch, steps):
    _write_workflow(tmp_path, {"job": steps})
    monkeypatch.setattr(workflow, "ROOT", tmp_path)
    sites = _install_sites()
    return _backend_sites(sites), _sites_without_hashed_pyyaml_first(sites)


@pytest.mark.parametrize("accepted,near_miss", [
    *[([HASHED_PYYAML, _wheel(flag), AUDIT], [HASHED_PYYAML, _wheel(flag, "other"), AUDIT])
      for flag in ("-w", "--wheel-dir ", "--wheel-dir=")],
    ([PREPARED, AUDIT],
     [HASHED_PYYAML, _wheel("-w="), AUDIT]),
    ([HASHED_PYYAML, _wheel("-w ", "other") + " -w " + WHEEL_DIR, AUDIT],
     [HASHED_PYYAML, WHEEL + " -w " + OTHER_DIR, AUDIT]),
    ([HASHED_PYYAML, _wheel("-w", "other") + " --wheel-dir " + WHEEL_DIR, AUDIT],
     [HASHED_PYYAML, _wheel("--wheel-dir ") + " -w" + OTHER_DIR, AUDIT]),
    ([HASHED_PYYAML, _wheel("--wheel-dir=", "other") + " -w" + WHEEL_DIR, AUDIT],
     [HASHED_PYYAML, WHEEL + " --wheel-dir=" + OTHER_DIR, AUDIT]),
    *[([PREPARED, _env_audit(EXPRESSION_DIR)], [PREPARED, near]) for near in (
        _env_audit(EXPRESSION_DIR.replace("pyyaml", "other")),
        _env_audit(EXPRESSION_DIR.replace("temp", "name")),
        _env_audit("$RUNNER_TEMP/pyyaml-wheel"),
        _env_audit(EXPRESSION_DIR + " /opt/other"),
        _env_audit(EXPRESSION_DIR, "PIP_FIND_LINK"),
        _step(BARE_AUDIT, "env", "${{ fromJSON(inputs.env) }}"))],
    ([HASHED_PYYAML, WHEEL, _env_audit(EXPRESSION_DIR)],
     [HASHED_PYYAML, _step(WHEEL, "env", {"PIP_FIND_LINKS": EXPRESSION_DIR}), BARE_AUDIT]),
    ([HASHED_PYYAML, WHEEL.replace(WHEEL_DIR, "/opt/wheels"), _env_audit("/opt/wheels")],
     [HASHED_PYYAML, WHEEL.replace(WHEEL_DIR, "/opt/wheels"), _env_audit("/opt/other")]),
    ([HASHED_PYYAML, WHEEL.replace(WHEEL_DIR, f'"{EXPRESSION_DIR}"'), _env_audit(EXPRESSION_DIR)],
     [HASHED_PYYAML, WHEEL.replace(WHEEL_DIR, f'"{EXPRESSION_DIR}"'), _env_audit("/opt/wheels")]),
    ([HASHED_PYYAML, WHEEL.replace("$RUNNER_TEMP", "${RUNNER_TEMP}"), _env_audit(EXPRESSION_DIR)],
     [HASHED_PYYAML, WHEEL.replace("$RUNNER_TEMP", "${HOME}"), _env_audit(EXPRESSION_DIR)]),
    ([HASHED_PYYAML, WHEEL.replace("$RUNNER_TEMP", "${{  runner.temp  }}"), _env_audit(EXPRESSION_DIR)],
     [HASHED_PYYAML, WHEEL.replace("$RUNNER_TEMP", "$HOME"), _env_audit(EXPRESSION_DIR)]),
    ([HASHED_PYYAML, WHEEL.replace("$RUNNER_TEMP", "${RUNNER_TEMP}"), _env_audit(EXPRESSION_DIR)],
     [HASHED_PYYAML, WHEEL.replace("$RUNNER_TEMP", "${RUNNER_TEMP}"), _env_audit(EXPRESSION_DIR.replace("pyyaml", "other"))]),
    ([PREPARED, AUDIT.replace("PIP_FIND_LINKS=", "PIP_FIND_LINKS=/opt/other PIP_FIND_LINKS=")],
     [PREPARED, AUDIT.replace(f"PIP_FIND_LINKS={WHEEL_DIR}", f"PIP_FIND_LINKS={WHEEL_DIR} PIP_FIND_LINKS=/opt/other")]),
    ([PREPARED, _step(AUDIT, "env", {"PIP_FIND_LINKS": "/opt/other"})],
     [PREPARED, _step(AUDIT.replace("pyyaml-wheel", "other"), "env", {"PIP_FIND_LINKS": EXPRESSION_DIR})]),
    ([HASHED_PYYAML, WHEEL.replace("$RUNNER_TEMP", "$RUNNER_TEMP_X"), AUDIT.replace("$RUNNER_TEMP", "$RUNNER_TEMP_X")],
     [HASHED_PYYAML, WHEEL.replace("$RUNNER_TEMP", "$RUNNER_TEMP_X"), _env_audit("${{ runner.temp }}_X/pyyaml-wheel")]),
    ([_chain(PREPARED, "echo ok"), AUDIT],
     [_chain(PREPARED, COPY), AUDIT]),
    *[([PREPARED, first, second], [PREPARED, first, _lines(COPY, second)]) for first, second in (
        (AUDIT, AUDIT), (AUDIT, WRAPPED_AUDIT), (WRAPPED_AUDIT, AUDIT), (AUDIT, AUDIT + " --requirement requirements-dev.txt"))],
    *[([PREPARED, DEV_AUDIT.replace("pip-audit", good), AUDIT], [PREPARED, DEV_AUDIT.replace("pip-audit", bad), AUDIT])
      for good, bad in (("pip-audit", "echo pip-audit"), ("pip_audit", "echo pip_audit"),
                        ("python -m pip_audit", "echo python -m pip_audit"),
                        ("pip-audit", "pip-auditor"), ("pip-audit", "pip-audit-x"))],
    ([HASHED_PYYAML, WHEEL.replace(WHEEL_DIR, "$HOME/w"), DEV_AUDIT.replace(WHEEL_DIR, "$HOME/w"), AUDIT.replace(WHEEL_DIR, "$HOME/w")],
     [HASHED_PYYAML, WHEEL.replace(WHEEL_DIR, "$HOME/w"), DEV_AUDIT.replace(WHEEL_DIR, "$HOME/w").replace("pip-audit", "echo pip-audit"),
      AUDIT.replace(WHEEL_DIR, "$HOME/w")]),
    *[([PREPARED, DEV_AUDIT, AUDIT], [PREPARED, DEV_AUDIT + " " + tail.format("true"), AUDIT])
      for tail in ("; {}", "|| {}", "| {}", "& {}", "> {}", ">> {}", "< {}", "$({})", "`{}`", "x#y; {}", "$# | {}")],
    *[([PREPARED, DEV_AUDIT, AUDIT], [PREPARED, DEV_AUDIT.replace('wheel" ', f'wheel"{tail} '), AUDIT]) for tail in ("x", ".x", "-x")],
    ([HASHED_PYYAML, WHEEL.replace(WHEEL_DIR, "/opt/w"), DEV_AUDIT.replace(WHEEL_DIR, "/opt/w"), AUDIT.replace(WHEEL_DIR, "/opt/w")],
     [HASHED_PYYAML, WHEEL.replace(WHEEL_DIR, "/opt/w"), DEV_AUDIT.replace(WHEEL_DIR, "/opt/w/sub"), AUDIT.replace(WHEEL_DIR, "/opt/w")]),
    ([PREPARED, DEV_AUDIT, AUDIT], [PREPARED, DEV_AUDIT.replace("pyyaml-wheel", "pyyaml-wheel/sub"), AUDIT]),
    ([PREPARED, BACKEND_INSTALL, AUDIT], [PREPARED, BACKEND_INSTALL + f" PIP_FIND_LINKS={WHEEL_DIR}", AUDIT]),
    *[([HASHED_PYYAML, WHEEL.replace(WHEEL_DIR, "/opt/W"), DEV_AUDIT.replace(WHEEL_DIR, "/opt/W") + " -o /opt/" + case, AUDIT.replace(WHEEL_DIR, "/opt/W")],
       [HASHED_PYYAML, WHEEL.replace(WHEEL_DIR, "/opt/W"), DEV_AUDIT.replace(WHEEL_DIR, "/opt/W") + " -o /opt/W/x", AUDIT.replace(WHEEL_DIR, "/opt/W")])
      for case in ("w", "W-x")],
    *[([PREPARED, DEV_AUDIT + " " + good, AUDIT], [PREPARED, DEV_AUDIT + " " + bad, AUDIT])
      for good, bad in (('--desc "a;b|c>d"', "--desc a;b|c>d"), ("'oops'", "'oops"))],
    ([PREPARED, BACKEND_INSTALL, AUDIT],
     [PREPARED, BACKEND_INSTALL + " --find-links " + WHEEL_DIR, AUDIT]),
    ([PREPARED, "pip wheel -r backend/requirements.txt", AUDIT],
     [PREPARED, "pip wheel -r backend/requirements.txt -w " + WHEEL_DIR, AUDIT]),
    *[([PREPARED, _env_audit(EXPRESSION_DIR.replace("{{ runner.temp }}", spelling))],
       [PREPARED, _env_audit(EXPRESSION_DIR.replace("runner.temp", "runner.tmp"))])
      for spelling in ("{{runner.temp}}", "{{  runner.temp  }}", "{{\trunner.temp}}", "{{runner.temp\t}}")],
    *[([PREPARED, DEV_AUDIT + tail, AUDIT], [PREPARED, DEV_AUDIT + tail + " ; ", AUDIT]) for tail in (" || exit", " ||exit 12")],
    ([PREPARED, _env_audit(EXPRESSION_DIR)], [PREPARED, _env_audit(EXPRESSION_DIR.replace("runner.temp", "runner_temp"))]),
    ([PREPARED, _step("echo ok", "if", "always()"), AUDIT],
     [PREPARED, _step(COPY, "if", "always()"), AUDIT]),
    ([PREPARED, _lines(AUDIT, COPY)],
     [PREPARED, _lines(COPY, AUDIT)]),
    ([PREPARED, _lines("  # " + COPY, AUDIT)],
     [PREPARED, _lines(COPY, AUDIT)]),
    ([PREPARED, "echo ok  # stage the wheel", AUDIT],
     [PREPARED, COPY + "  # stage the wheel", AUDIT]),
    ([PREPARED, _step(_wheel("--wheel-dir "), "if", "always()"), AUDIT],
     [PREPARED, _step(_wheel("--wheel-dir ").replace(" --require-hashes", ""), "if", "always()"), AUDIT]),
    ([PREPARED, _step(_wheel("-w"), "continue-on-error", True), AUDIT],
     [PREPARED, _step(_wheel("-w").replace(" --require-hashes", ""), "continue-on-error", True), AUDIT]),
])
def test_hashed_pyyaml_check_pairs_each_spelling_with_a_near_miss(tmp_path, monkeypatch, accepted, near_miss):
    assert accepted != near_miss
    assert (_judged(tmp_path / "accepted", monkeypatch, accepted), _judged(tmp_path / "near", monkeypatch, near_miss)) == (
        (["sample.yaml:job"], []), (["sample.yaml:job"], ["sample.yaml:job"]))


@pytest.mark.parametrize("place,before_audit", [
    (lambda command: [command, HASHED_PYYAML, WHEEL, AUDIT], True),
    (lambda command: [HASHED_PYYAML, command, WHEEL, AUDIT], True),
    (lambda command: [HASHED_PYYAML, WHEEL, command, AUDIT], True),
    (lambda command: [HASHED_PYYAML, WHEEL, AUDIT, command], False),
    (lambda command: [PREPARED, _lines(command, AUDIT)], True),
    (lambda command: [PREPARED, _lines(AUDIT, command)], False),
])
@pytest.mark.parametrize("command,writes", [(command, True) for command in WRITES]
                         + [(command, False) for command in HARMLESS])
def test_hashed_pyyaml_check_flags_a_write_into_the_wheel_dir_only_before_the_audit(
        tmp_path, monkeypatch, command, writes, place, before_audit):
    assert _judged(tmp_path, monkeypatch, place(command)) == (
        ["sample.yaml:job"], ["sample.yaml:job"] if writes and before_audit else [])


def test_a_write_in_one_job_does_not_taint_another(tmp_path, monkeypatch):
    _write_workflow(tmp_path, {"bad": [PREPARED, COPY, AUDIT], "good": [PREPARED, AUDIT],
                               "quiet": ["echo " + BACKEND_INSTALL]})
    monkeypatch.setattr(workflow, "ROOT", tmp_path)
    sites = _install_sites()
    assert (_backend_sites(sites), _sites_without_hashed_pyyaml_first(sites)) == (
        ["sample.yaml:bad", "sample.yaml:good"], ["sample.yaml:bad"])


@pytest.mark.parametrize("words,wheel_dir", [
    (["-w", "d"], "d"),
    (["-wd"], "d"),
    (["-w=d"], "=d"),
    (["--wheel-dir", "d"], "d"),
    (["--wheel-dir=d"], "d"),
    (["--wheel-dir=d=e"], "d=e"),
    (["--wheel-dirs=d"], None),
    (["--wheel-dirs", "d"], None),
    (["--wheel-dir"], None),
    (["-w", "a", "-w", "b"], "b"),
    (["-w", "a", "--wheel-dir=b"], "b"),
    (["--wheel-dir=a", "-w", "b"], "b"),
    (["--wheel-dir", "a", "-wb"], "b"),
    (["-wa", "--wheel-dir", "b"], "b"),
    (["-w", "a", "-w"], None),
    (["pip", "wheel", "-r", "x"], None),
])
def test_wheel_dir_is_the_last_value_the_way_pip_reads_it(words, wheel_dir):
    assert _wheel_dir(words) == wheel_dir


@pytest.mark.parametrize("text,named", [("cp x dir/", True), ('cp x "dir"', True), ("echo x >dir/f", True)]
                         + [(f"cp x dir{suffix}/", False) for suffix in ("-o", ".o", "2", "_o")] + [("cp x DIR/", False)])
def test_a_text_names_a_directory_only_as_a_whole_path(text, named):
    assert _names(text, "dir") is named


def test_commands_expand_the_runner_temp_variable_and_carry_the_step_links():
    script = _lines('pip wheel -w "$RUNNER_TEMP/a" -w $RUNNER_TEMP_X/b -w ${RUNNER_TEMP}/c -w ${HOME}/d', "echo it's $RUNNER_TEMP")
    assert [_commands(script), _commands("pip wheel -w a", 5), _commands("echo hi", "x")] == [
        [('pip wheel -w "${{ runner.temp }}/a" -w $RUNNER_TEMP_X/b -w ${{ runner.temp }}/c -w ${HOME}/d',
          ["pip", "wheel", "-w", "${{ runner.temp }}/a", "-w", "$RUNNER_TEMP_X/b", "-w", "${{ runner.temp }}/c",
           "-w", "${HOME}/d"]),
         ("echo it's ${{ runner.temp }}", [])],
        [("pip wheel -w a", ["PIP_FIND_LINKS=5", "pip", "wheel", "-w", "a"])],
        [("echo hi", [])]]


def test_a_backend_path_in_text_that_is_not_a_command_is_not_a_site():
    assert _backend_sites({"site": [(False, _commands("echo " + BACKEND_INSTALL))]}) == []


@pytest.mark.parametrize("audit", [AUDIT, DEV_AUDIT])
@pytest.mark.parametrize("chain", [
    "{audit} ; cp x {dir}/", "{audit};cp x {dir}/", "{audit} && cp x {dir}/", "{audit} || cp x {dir}/",
    "{audit} || exit 1 ; cp x {dir}/", "{audit} & cp x {dir}/", "{audit}&cp x {dir}/", "{audit} | tee {dir}/x",
    "{audit} > {dir}/x", "{audit} >> {dir}/x", "{audit} < {dir}/x", "{audit}<{dir}/x",
    '{audit} "$(cp x {dir}/)"', "{audit} $(cp x {dir}/)", "{audit} `cp x {dir}/`", '{audit} "`cp x {dir}/`"',
    "{audit} -o {dir}/x", "{audit} --cache-dir={dir}/c", "TMPDIR={dir} {audit}", "{audit} x#y; cp x {dir}/", "{audit} $# | tee {dir}/x",
    '{audit} -o "$RUNNER_TEMP"/pyyaml-wheel/x', "{audit} -o $RUNNER_TEMP/pyyaml-wheel/x", "{audit} --cache-dir=${{{{ runner.temp }}}}/pyyaml-wheel"])
def test_a_write_chained_onto_an_earlier_audit_is_still_seen(tmp_path, monkeypatch, audit, chain):
    assert _judged(tmp_path, monkeypatch, [PREPARED, chain.format(audit=audit, dir=WHEEL_DIR), AUDIT]) == (
        ["sample.yaml:job"], ["sample.yaml:job"])
