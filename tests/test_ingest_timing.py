import logging
import re
import time

import pytest

from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture as ingest_fresh_db_fixture,
    _mini_r2_env_fixture as ingest_mini_r2_env,
)
from backend import ingest, timing


_DERIVED_PHASES = (
    "suppressed", "reprice", "aliases", "canonical", "teammates",
    "usage_rollup", "tool_rollup", "tool_error_rollup",
    "dispatch_rollup", "dispatch_brief_rollup", "latency_rollup",
    "ctx_cost_rollup", "agent_rollup",
)
_INGEST_PHASES = (
    "open_run", "scope", "list", "markers", "lane_ids", "plan", "existing",
    "fetch_parse", "persist", "orphans", "orphan_projects",
    *_DERIVED_PHASES, "close_run", "notify", "warm", "finish",
)


def _timing_lines(caplog):
    return [record.getMessage() for record in caplog.records
            if record.name == "claudit.ingest"
            and record.getMessage().startswith("TIMING ")]


def _assert_phase_line(line):
    phase_positions = [line.index(f"{phase}=") for phase in _INGEST_PHASES]
    assert phase_positions == sorted(phase_positions)
    assert all(re.search(rf"(?:^|\s){phase}=\d+ms(?:\s|$)", line)
               for phase in _INGEST_PHASES)
    suppressed = re.search(r"\bsuppressed=(\d+)ms", line)
    assert suppressed and int(suppressed.group(1)) >= 200


def _assert_sum_gap(line):
    total_match = re.search(r"\btotal=([\d.]+)ms", line)
    sum_match = re.search(r"\bsum=([\d.]+)ms", line)
    gap_match = re.search(r"\bgap=([\d.]+)ms", line)
    assert total_match and sum_match and gap_match
    total, summed, gap = (float(match.group(1))
                          for match in (total_match, sum_match, gap_match))
    assert summed <= total
    assert gap <= max(100, total * 0.1)


def test_phases_done_reports_total_sum_and_gap(monkeypatch, caplog):
    ticks = iter((10.0, 10.035))
    monkeypatch.setattr(timing.time, "perf_counter", lambda: next(ticks))
    monkeypatch.setattr(timing, "TIMING_ON", True)
    phases = timing.Phases("synthetic", account=True)
    phases.mark("first", 0.012)
    phases.mark("second", 0.008)

    with caplog.at_level(logging.INFO, logger="claudit.api"):
        phases.done(outcome="ok")

    line = next(record.getMessage() for record in caplog.records
                if record.getMessage().startswith("TIMING synthetic "))
    assert "total=35ms" in line
    assert "sum=20ms" in line
    assert "gap=15ms" in line
    assert "first=12ms second=8ms outcome=ok" in line


def test_phases_done_keeps_api_line_shape_without_accounting(
        monkeypatch, caplog):
    ticks = iter((10.0, 10.035))
    monkeypatch.setattr(timing.time, "perf_counter", lambda: next(ticks))
    monkeypatch.setattr(timing, "TIMING_ON", True)
    phases = timing.Phases("synthetic")
    phases.mark("first", 0.012)
    phases.mark("second", 0.008)

    with caplog.at_level(logging.INFO, logger="claudit.api"):
        phases.done(outcome="ok")

    line = next(record.getMessage() for record in caplog.records
                if record.getMessage().startswith("TIMING synthetic "))
    assert line == (
        "TIMING synthetic total=35ms first=12ms second=8ms outcome=ok")


def test_ingest_logs_every_phase_and_run_counts(
        fresh_db, mini_r2_env, monkeypatch, caplog):
    monkeypatch.setattr(timing, "TIMING_ON", True)
    original = ingest.purge_suppressed

    def slow_suppression():
        time.sleep(0.21)
        return original()

    monkeypatch.setattr(ingest, "purge_suppressed", slow_suppression)

    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        result = ingest.run_ingest(trigger="manual")

    lines = _timing_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    _assert_phase_line(line)
    assert "sum=" in line and "gap=" in line
    assert "listed=5" in line and "todo=5" in line
    assert "inserted=5" in line and "reparsed=0" in line
    assert "deleted=0" in line and "changed=" in line
    assert "outcome=ok" in line

    _assert_sum_gap(line)
    assert result["error"] is None


def test_ingest_timing_is_silent_when_flag_is_off(
        fresh_db, mini_r2_env, monkeypatch, caplog):
    monkeypatch.setattr(timing, "TIMING_ON", False)

    def unexpected_collector(*args, **kwargs):
        raise AssertionError("disabled timing created a collector")

    monkeypatch.setattr(timing, "Phases", unexpected_collector)
    real_run = ingest._run_ingest_locked  # pylint: disable=protected-access

    def check_no_timing_context(trigger):
        assert ingest._RUN_TIMING.get() is None  # pylint: disable=protected-access
        return real_run(trigger)

    monkeypatch.setattr(ingest, "_run_ingest_locked", check_no_timing_context)

    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        ingest.run_ingest(trigger="manual")

    assert not _timing_lines(caplog)


def test_timing_logger_failure_preserves_delegate_exception(monkeypatch):
    monkeypatch.setattr(timing, "TIMING_ON", True)
    expected = ValueError("original ingest failure")

    def fail_run(trigger):
        raise expected

    def fail_log(*args, **kwargs):
        raise RuntimeError("timing logger failed")

    monkeypatch.setattr(ingest, "_run_ingest_locked", fail_run)
    monkeypatch.setattr(ingest.log, "info", fail_log)

    with pytest.raises(ValueError, match="original ingest failure") as raised:
        ingest.run_ingest_locked("manual")

    assert raised.value is expected


def test_timing_logger_failure_preserves_success_summary(monkeypatch):
    monkeypatch.setattr(timing, "TIMING_ON", True)
    expected = {
        "r2_listed": 0,
        "inserted": 0,
        "reparsed": 0,
        "deleted": 0,
        "aborted": False,
        "error": None,
    }

    def fail_log(*args, **kwargs):
        raise RuntimeError("timing logger failed")

    monkeypatch.setattr(ingest, "_run_ingest_locked", lambda trigger: expected)
    monkeypatch.setattr(ingest.log, "info", fail_log)

    assert ingest.run_ingest_locked("manual") is expected


def test_aborted_ingest_still_logs_its_outcome(
        fresh_db, mini_r2_env, monkeypatch, caplog):
    monkeypatch.setattr(timing, "TIMING_ON", True)
    real = ingest._fetch_parse_persist  # pylint: disable=protected-access

    def abort_after_listing(todo, parser_version, failed, seen_keys):
        ingest._SHUTDOWN.set()  # pylint: disable=protected-access
        return real(todo, parser_version, failed, seen_keys)

    monkeypatch.setattr(ingest, "_fetch_parse_persist", abort_after_listing)
    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        try:
            result = ingest.run_ingest(trigger="manual")
        finally:
            ingest._SHUTDOWN.clear()  # pylint: disable=protected-access

    lines = _timing_lines(caplog)
    assert len(lines) == 1
    assert "outcome=aborted" in lines[0]
    assert result["aborted"] is True
