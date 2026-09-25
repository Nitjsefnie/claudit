"""SV-RATE-REFRESH: an alternating price — a listing whose rates return to
an earlier recent entry's value is reported for a human, not appended; a
schedule on either side exempts the row.

Driven the same way as test_provider_rate_refresh.py, whose fixture
builders these share: fixture payloads over the seeded view, never the
network.
"""
from __future__ import annotations

from tests.test_provider_rate_refresh import (
    GLM, RATE_FIELDS, STAMP, Run, _overrides, _per_token)


# --- an alternating price -----------------------------------------------------


def _alternating_row(run: Run, first_from: str, second_from: str) -> tuple[dict, dict]:
    """OpenInference on GLM with a hand-written history: rates A at
    `first_from`, B at `second_from` (the newest), the payload still
    listing A."""
    seeded = run.doc()["providers"][GLM]["OpenInference"][-1]
    a = {f: seeded[f] for f in RATE_FIELDS}
    b = {**a, "output": seeded["output"] * 2}
    run.edit(lambda doc: doc["providers"][GLM].update({"OpenInference": [
        {"from": first_from, **a}, {"from": second_from, **b}]}))
    return a, b


def test_a_flip_back_to_recent_rates_is_reported_not_appended(tmp_path, capsys):
    """Rates returning within the lookback to an earlier recent entry's
    value, with no schedule on either side, read as an alternation: the row
    is left for a human, since an hourly flip-flop would otherwise be
    appended on every change."""
    run = Run(tmp_path)
    version = run.pricing_version()
    _alternating_row(run, "2030-12-28T00:00:00Z", "2030-12-31T00:00:00Z")
    before = run.snapshot()
    rc, out, _ = run(capsys)
    assert rc == 0
    assert run.snapshot() == before, "the row is left untouched"
    assert "no rate moved" in out
    assert "alternating price" in out
    assert f"{GLM} via OpenInference" in out
    assert "2030-12-28T00:00:00Z" in out
    assert run.pricing_version() == version, "no append, no bump"


def test_a_genuine_move_still_appends(tmp_path, capsys):
    run = Run(tmp_path)
    a, _ = _alternating_row(run, "2030-12-28T00:00:00Z", "2030-12-31T00:00:00Z")
    moved = {**a, "output": a["output"] * 3}
    run.endpoint(GLM, "OpenInference")["pricing"]["completion"] = _per_token(
        moved["output"])
    version = run.pricing_version()
    rc, out, _ = run(capsys)
    assert rc == 0
    assert "alternating price" not in out
    assert run.doc()["providers"][GLM]["OpenInference"][-1] == {"from": STAMP, **moved}
    assert run.pricing_version() == version + 1, "a real move still bumps"


def test_a_match_older_than_the_lookback_does_not_fire(tmp_path, capsys):
    run = Run(tmp_path)
    a, _ = _alternating_row(run, "2030-12-24T00:00:00Z", "2030-12-31T00:00:00Z")
    rc, out, _ = run(capsys)
    assert rc == 0
    assert "alternating price" not in out
    assert run.doc()["providers"][GLM]["OpenInference"][-1] == {"from": STAMP, **a}


def test_a_match_exactly_at_the_lookback_does_not_fire(tmp_path, capsys):
    """2030-12-25 is exactly `at` minus 7 days; the lookback is exclusive
    (`>` against the cutoff), so the boundary entry is outside it and the
    move appends like any older one."""
    run = Run(tmp_path)
    a, _ = _alternating_row(run, "2030-12-25T00:00:00Z", "2030-12-31T00:00:00Z")
    rc, out, _ = run(capsys)
    assert rc == 0
    assert "alternating price" not in out
    assert run.doc()["providers"][GLM]["OpenInference"][-1] == {"from": STAMP, **a}


def test_a_newest_entry_with_a_schedule_is_exempt_from_the_alternation_notice(
        tmp_path, capsys):
    run = Run(tmp_path)
    a, b = _alternating_row(run, "2030-12-28T00:00:00Z", "2030-12-31T00:00:00Z")
    schedule = [{"days": ["sunday"], "rates": {f: b[f] for f in RATE_FIELDS}}]
    run.edit(lambda doc: doc["providers"][GLM]["OpenInference"][-1].update(
        schedule=schedule))
    rc, out, _ = run(capsys)
    assert rc == 0
    assert "alternating price" not in out
    assert run.doc()["providers"][GLM]["OpenInference"][-1] == {"from": STAMP, **a}


def test_an_incoming_listing_with_a_schedule_is_exempt_from_the_alternation_notice(
        tmp_path, capsys):
    run = Run(tmp_path)
    a, _ = _alternating_row(run, "2030-12-28T00:00:00Z", "2030-12-31T00:00:00Z")
    schedule = [{"days": ["sunday"], "rates": {f: a[f] for f in RATE_FIELDS}}]
    run.endpoint(GLM, "OpenInference")["pricing"]["overrides"] = _overrides(schedule)
    rc, out, _ = run(capsys)
    assert rc == 0
    assert "alternating price" not in out
    assert run.doc()["providers"][GLM]["OpenInference"][-1] == {
        "from": STAMP, **a, "schedule": schedule}
