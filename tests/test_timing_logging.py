"""Operational INFO messages reach the log without CLAUDIT_TIMING (issue
#378).

timing.py attaches the claudit logger's only handler and INFO level, so
before the fix a deployment without the flag discarded every INFO line —
the reprice summary, the rollback-guard skip count, rekey and eviction
notices — and kept only warnings. The flag named for timing must gate
the TIMING lines themselves (Phases.done returns early without it), not
the logger's existence.
"""
import importlib
import logging

from backend import timing


def _reload_without_flag(monkeypatch) -> None:
    """Reload the module with the flag off — the suite's default import
    state (conftest pins CLAUDIT_TIMING=0), so nothing leaks forward."""
    monkeypatch.setenv("CLAUDIT_TIMING", "0")
    importlib.reload(timing)


def test_info_configured_without_the_timing_flag(monkeypatch):
    """With the flag off, the claudit logger still carries its handler
    and INFO level: operational INFO reaches the journal by default."""
    _reload_without_flag(monkeypatch)
    logger = logging.getLogger("claudit")
    assert logger.handlers, (
        "the claudit logger must carry its handler without CLAUDIT_TIMING")
    assert logger.getEffectiveLevel() <= logging.INFO
    assert logging.getLogger("claudit.ingest").isEnabledFor(logging.INFO)


def test_timing_lines_stay_gated_without_the_flag(monkeypatch, caplog):
    """The flag still gates the TIMING lines: without it a finished
    Phases emits nothing, while an ordinary INFO line on the same logger
    reaches the captured root handler (propagation untouched)."""
    _reload_without_flag(monkeypatch)
    logger = logging.getLogger("claudit.api")
    with caplog.at_level(logging.INFO):
        ph = timing.Phases("probe", logger=logger)
        ph.mark("step", 0.5)
        ph.done(extra="kept")
        logger.info("operational line")
    assert [r.getMessage() for r in caplog.records] == ["operational line"]


def test_flag_on_still_emits_timing_lines(monkeypatch, caplog):
    """CLAUDIT_TIMING=1 keeps emitting TIMING lines (no noise added, no
    capability lost)."""
    monkeypatch.setenv("CLAUDIT_TIMING", "1")
    importlib.reload(timing)
    try:
        assert timing.TIMING_ON
        with caplog.at_level(logging.INFO):
            ph = timing.Phases("probe",
                               logger=logging.getLogger("claudit.api"))
            ph.mark("step", 0.5)
            ph.done()
        assert any(r.getMessage().startswith("TIMING probe")
                   for r in caplog.records)
    finally:
        # Leave the module as the suite's default import built it.
        monkeypatch.undo()
        importlib.reload(timing)
