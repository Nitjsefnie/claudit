"""The hourly refresh's commit body carries only what the run changed.

The standing blocks — the vanished-host set (rows kept) and the sampled
host classifications — are dropped from the commit message and kept in
the run's own stdout report. A committing run records both as the
``vanished`` and ``sampled`` keys of the state file beside the pricing
document (src/pricing_notices.json, which carries the notice
baseline), so a vanished set that changes or a classification that
changes shows as a diff of that file instead of re-printing in every
commit body.

The vanish case drives the shared Run harness; the sampled case drives
the log harness in tests/test_provider_rate_log_refresh, whose
disagreement payload samples its host at detection time.
"""
from __future__ import annotations

import json
import re

from tests.refresh_fixture_builders import _endpoint
from tests.test_provider_rate_refresh import (
    Run, _move_openinference, GLM, NEWCOMER)
from tests.test_provider_rate_log_refresh import (
    MODEL, HOST, RATE_A, RATE_B, RATE_C, _run)

_STATE_KEYS = ["notices", "sampled", "vanished"]
_STANDING_PREFIXES = ("  vanished  ", "  sampled   ")


def _standing_lines(report: str) -> list[str]:
    return [line for line in report.splitlines()
            if line.startswith(_STANDING_PREFIXES)]


def _state_only_headers(report: str) -> list[str]:
    """Model headers whose section carries nothing BUT standing lines: after
    the drop the whole section is empty, so the commit body must not carry
    the header either."""
    lines = report.splitlines()
    headers = []
    for index, line in enumerate(lines):
        if index == 0 or line.startswith(" ") or not line.endswith(")"):
            continue
        section = []
        for follow in lines[index + 1:]:
            if not follow.startswith(" "):
                break
            section.append(follow)
        if section and all(f.startswith(_STANDING_PREFIXES) for f in section):
            headers.append(line)
    return headers


def _vanished_run(tmp_path, capsys) -> tuple[Run, str, str]:
    """A committing run that moves OpenInference and loses Cloudflare."""
    run = Run(tmp_path)
    _move_openinference(run)
    run.endpoints(GLM)[:] = [e for e in run.endpoints(GLM)
                             if e["provider_name"] != "Cloudflare"]
    rc, out, _ = run(capsys)
    assert rc == 0
    return run, out, run.commit_msg.read_text(encoding="utf-8")


def test_the_commit_body_drops_the_standing_blocks_the_report_keeps(
        tmp_path, capsys):
    run, out, message = _vanished_run(tmp_path, capsys)
    standing = _standing_lines(out)
    assert any("Cloudflare" in line for line in standing), out

    _, _, body = message.partition("\n\n")
    for line in standing:
        assert line not in body
    empty_headers = _state_only_headers(out)
    assert empty_headers, f"the fixture must produce state-only sections: {out}"
    for header in empty_headers:
        assert header not in body
    assert f"{GLM} ({run.doc()['openrouter']['models'][GLM]['id']})" in body
    assert re.search(r"changed\s+OpenInference", body), body


def test_a_committing_run_records_the_standing_state(tmp_path, capsys):
    _run_state = tmp_path / "pricing_notices.json"
    _vanished_run(tmp_path, capsys)

    state = json.loads(_run_state.read_text(encoding="utf-8"))
    assert list(state) == _STATE_KEYS
    assert "Cloudflare" in state["vanished"][GLM]
    assert all(isinstance(hosts, list)
               for hosts in state["vanished"].values())
    assert all(isinstance(classifications, dict)
               for classifications in state["sampled"].values())
    assert all(isinstance(reason, str)
               for classifications in state["sampled"].values()
               for reason in classifications.values())


def test_the_sampled_block_leaves_the_commit_body_and_is_recorded(
        tmp_path, capsys):
    states = [("2030-12-31T23:00:00Z", RATE_A),
              ("2031-01-01T00:10:00Z", RATE_B)]
    commit_msg = tmp_path / "commit-msg.txt"

    rc, out, err, pricing_path, _ = _run(
        tmp_path, capsys, history=states, endpoint_rates=RATE_C,
        commit_msg=commit_msg)
    assert rc == 0 and not err

    sampled = _standing_lines(out)
    assert any(line.startswith("  sampled   ") for line in sampled), out
    message = commit_msg.read_text(encoding="utf-8")
    for line in sampled:
        assert line not in message
    assert "  sampled   " not in message
    assert re.search(rf"changed\s+{re.escape(HOST)}", message), message

    state = json.loads(
        pricing_path.with_name("pricing_notices.json").read_text(
            encoding="utf-8"))
    # The recorded classification is the SAME display literal the run's
    # stdout report printed — the writer and the renderer cannot drift.
    assert state["sampled"][MODEL][HOST] in out
    assert state["sampled"] == {MODEL: {HOST: "no matching log state"}}
    assert state["vanished"] == {}


def test_the_commit_message_names_the_counts_and_carries_the_report(
        tmp_path, capsys):
    run = Run(tmp_path)
    _move_openinference(run)
    run.endpoints(GLM).append(_endpoint("Newcomer", NEWCOMER))
    rc, out, _ = run(capsys)
    assert rc == 0
    message = run.commit_msg.read_text(encoding="utf-8")
    subject, _, body = message.partition("\n\n")
    assert subject == "Refresh OpenRouter provider rates: 1 changed, 1 new"
    # The body is the run's report minus the standing vanished/sampled
    # blocks, which the stdout report keeps: the body is a
    # section of that report, never a superset of it.
    assert "OpenRouter provider rates, detected" in body
    assert re.search(r"changed\s+OpenInference", body)
    assert re.search(r"new\s+Newcomer", body)
    for line in out.splitlines():
        if line.startswith(_STANDING_PREFIXES):
            assert line not in body
    assert "Co-Authored-By" not in message
