"""SV-RATE-REFRESH: a banded log row appends at most ONE widened entry.

An oscillating host's price is recorded once, as its range, so the hourly
run commits nothing while the price moves inside that range. Driven the
way tests/test_provider_rate_log_refresh.py drives its own cases — full
`_refresh_model` runs over fixture payloads, never the network — and
sharing its machinery the way the alternation tests share
test_provider_rate_refresh's.
"""
from __future__ import annotations

import json
import shutil
import subprocess

from backend import pricing

from tests.test_provider_rate_log_refresh import (
    HOST, MODEL, RATE_A, RATE_B, RATE_C, ROOT, STAMP, _doc, _entry, _run, refresh)

RATE_D = {"fresh": 0.9, "create_5m": 0.9, "create_1h": 0.9,
          "read": 0.09, "output": 2.9}
RATE_MID = {"fresh": 0.25, "create_5m": 0.25, "create_1h": 0.25,
            "read": 0.015, "output": 0.75}
RATE_B_LOW = {"fresh": 0.05, "create_5m": 0.05, "create_1h": 0.05,
              "read": 0.005, "output": 0.25}


def _band_of(*rates: dict) -> dict:
    """A band spanning the levels, per rate field."""
    return {field: [min(level[field] for level in rates),
                    max(level[field] for level in rates)]
            for field in pricing.RATE_FIELDS}


def _banded_row(*levels: dict, undated: bool = False,
                mean: dict | None = None) -> list[dict]:
    """A row in the shape a collapse leaves: its newest entry is banded and
    priced by `mean`, the time-weighted mean of the levels the host moved
    between, which equals NO individual level.

    That is load-bearing. A newest entry priced at a LISTED level is
    swallowed by the pre-existing `rates == previous` dedup in
    _new_log_states, which returns before the band is ever consulted — so a
    fixture built that way gives the band no test reaching it, and deleting
    the branch that stops the churn leaves the suite green.

    `undated` gives the banded entry no `from` and makes the row the single
    entry a collapse leaves for a history that began without one, which is
    38 of the 54 rows issue #640 collapses.
    """
    banded = {"from": None if undated else "2030-12-31T23:00:00Z",
              **(mean if mean is not None else RATE_MID), "band": _band_of(*levels)}
    return [banded] if undated else [_entry(None, levels[0]), banded]


def _log_refresh(tmp_path, capsys, *, row, states, endpoint_rates=None,
                 version=71):
    """One refresh run over a stored row and a log series, plus the file it
    would have written and the constants beside it."""
    return _run(tmp_path, capsys, history=states, hosts={HOST: row},
                endpoint_rates=endpoint_rates, version=version)


def test_a_listing_inside_a_band_appends_nothing_and_commits_nothing(tmp_path, capsys):
    row = _banded_row(RATE_A, RATE_B)
    states = [("2030-12-31T22:00:00Z", RATE_A), ("2031-01-01T00:10:00Z", RATE_B)]
    unchanged = json.dumps(_doc({HOST: row}), indent=2, sort_keys=True) + "\n"

    rc, out, err, pricing_path, constants_path = _log_refresh(
        tmp_path, capsys, row=row, states=states)

    assert rc == 0 and not err
    assert "no rate moved" in out
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    assert saved["providers"][MODEL][HOST] == row
    assert pricing_path.read_text(encoding="utf-8") == unchanged, "no write at all"
    assert saved["provider_rates_fetched"] == "2026-01-01T00:00:00Z"
    assert 'PRICING_VERSION = "71"' in constants_path.read_text(encoding="utf-8")


def test_a_listing_outside_a_band_appends_exactly_one_widened_entry(tmp_path, capsys):
    row = _banded_row(RATE_A, RATE_B)
    states = [("2030-12-31T23:00:00Z", RATE_B),
              ("2031-01-01T00:10:00Z", RATE_C),
              ("2031-01-01T00:20:00Z", RATE_C)]

    rc, out, err, pricing_path, constants_path = _log_refresh(
        tmp_path, capsys, row=row, states=states)

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    history = saved["providers"][MODEL][HOST]
    assert len(history) == len(row) + 1, history
    widened = history[-1]
    assert widened["from"] == STAMP
    assert widened["band"]["fresh"] == [0.2, 0.4], "the band grew to cover the move"
    assert widened["band"]["read"] == [0.01, 0.03]
    assert widened["band"]["output"] == [0.7, 0.9]
    assert {f: widened[f] for f in pricing.RATE_FIELDS} == RATE_MID, \
        "priced as before: a widening does not reprice"
    assert saved["provider_rates_fetched"] == STAMP
    assert 'PRICING_VERSION = "72"' in constants_path.read_text(encoding="utf-8")
    assert "1 log entries" not in out, "the widen is one entry, not a replay"


def test_every_state_outside_the_band_is_covered_by_the_one_entry(tmp_path, capsys):
    """Three states: two outside and in between, both the same way. One entry
    is appended and the band grows to the last one's level."""
    row = _banded_row(RATE_A, RATE_B)
    states = [("2030-12-31T23:00:00Z", RATE_B),
              ("2031-01-01T00:05:00Z", RATE_C),
              ("2031-01-01T00:10:00Z", RATE_D)]

    rc, _, err, pricing_path, _ = _log_refresh(
        tmp_path, capsys, row=row, states=states)

    assert rc == 0 and not err
    history = json.loads(pricing_path.read_text(encoding="utf-8"))[
        "providers"][MODEL][HOST]
    assert len(history) == len(row) + 1
    assert history[-1]["band"]["fresh"] == [0.2, 0.9]


def test_escapes_in_opposite_directions_both_end_up_inside_the_band(tmp_path, capsys):
    """0.9 then 0.05 inside one window: recording only the last leaves the
    0.9 excursion outside the range the row claims to have covered, and the
    next run churns on it again."""
    row = _banded_row(RATE_A, RATE_B)
    states = [("2030-12-31T23:00:00Z", RATE_B),
              ("2031-01-01T00:05:00Z", RATE_D),
              ("2031-01-01T00:10:00Z", RATE_B_LOW)]

    rc, _, err, pricing_path, _ = _log_refresh(
        tmp_path, capsys, row=row, states=states)

    assert rc == 0 and not err
    history = json.loads(pricing_path.read_text(encoding="utf-8"))[
        "providers"][MODEL][HOST]
    assert len(history) == len(row) + 1
    assert history[-1]["band"]["fresh"] == [0.05, 0.9], "both excursions are inside"


def test_a_band_entry_priced_at_the_mean_does_not_hide_a_move_past_it(tmp_path, capsys):
    """The dedup against the newest entry's rates compares against the band's
    MEAN, which is not any listed state: a log state equal to it is skipped
    as no change (it is inside the band anyway), and the state after it is
    not."""
    row = _banded_row(RATE_A, RATE_MID)
    assert row[-1]["fresh"] == RATE_MID["fresh"]
    states = [("2030-12-31T23:00:00Z", RATE_MID),
              ("2031-01-01T00:10:00Z", RATE_D)]

    rc, _, err, pricing_path, _ = _log_refresh(
        tmp_path, capsys, row=row, states=states, endpoint_rates=RATE_D)

    assert rc == 0 and not err
    history = json.loads(pricing_path.read_text(encoding="utf-8"))[
        "providers"][MODEL][HOST]
    assert len(history) == len(row) + 1
    assert history[-1]["band"]["fresh"] == [0.25, 0.9]


def test_a_log_state_equal_to_the_bands_mean_alone_is_no_news(tmp_path, capsys):
    """The pre-existing dedup in _new_log_states CAN shadow the band — a
    state equal to the stored newest entry's rates is skipped before the
    band is consulted — and shadowing it is harmless: a level equal to a
    band's mean lies inside that band by construction (a mean lies between
    its extremes), so the band would have appended nothing either.

    This is the guard on the guard, and it is NOT the production shape: the
    state gets past the cutoff first, which needs the row priced at a
    LISTED level, where a collapsed row is priced at a mean and nothing is
    ever skipped.
    """
    row = [_entry(None, RATE_A),
           {"from": "2030-12-31T23:00:00Z", **RATE_B, "band": _band_of(RATE_A, RATE_B)}]
    assert row[-1]["fresh"] == RATE_B["fresh"], "a listed level, against shape"
    states = [("2030-12-31T22:00:00Z", RATE_A), ("2031-01-01T00:10:00Z", RATE_B)]

    rc, out, err, pricing_path, _ = _log_refresh(
        tmp_path, capsys, row=row, states=states, endpoint_rates=RATE_B)

    assert rc == 0 and not err
    assert "no rate moved" in out
    assert json.loads(pricing_path.read_text(encoding="utf-8"))[
        "providers"][MODEL][HOST] == row


def test_an_undated_banded_row_widens_without_repricing_or_losing_coverage(
        tmp_path, capsys):
    """The shape 38 of the 54 rows issue #640 collapses: one entry, no
    `from`, priced by the mean. A widening must not reprice it — computing
    the mean over an undated entry silently repriced the row from its mean
    to whichever level last escaped — and the row must keep covering records
    from before the escape, which it does because the undated entry stays
    and the widened one is APPENDED after it."""
    row = _banded_row(RATE_A, RATE_B, undated=True)
    states = [("2031-01-01T00:10:00Z", RATE_C)]

    rc, _, err, pricing_path, _ = _log_refresh(
        tmp_path, capsys, row=row, states=states, endpoint_rates=RATE_C)

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    history = saved["providers"][MODEL][HOST]
    assert len(history) == 2
    assert history[0] == row[0], "the undated entry is untouched"
    assert history[1]["from"] == STAMP
    assert history[1]["band"]["fresh"] == [0.2, 0.4], "the band still widens"
    assert {f: history[1][f] for f in pricing.RATE_FIELDS} == RATE_MID, \
        "the mean still prices: a widening does not reprice"
    loaded = pricing.load_tables(saved)
    assert (MODEL, HOST) not in loaded["PROVIDER_STARTS"], \
        "no start instant: the row is in force for records before the widening"


def test_the_band_does_not_touch_an_unbanded_row(tmp_path, monkeypatch, capsys):
    """Differential: the same row, the same log, three runs — plain, banded,
    and banded with the band never read. The band is the only difference, so
    the third must reproduce the first exactly, including what it appended."""
    banded = _banded_row(RATE_A, RATE_B)
    unbanded = [_entry(None, RATE_A),
                {"from": "2030-12-31T23:00:00Z", **RATE_MID}]
    states = [("2030-12-31T23:00:00Z", RATE_B),
              ("2031-01-01T00:10:00Z", RATE_C)]

    def run(directory, row):
        directory.mkdir()
        rc, out, err, pricing_path, constants_path = _log_refresh(
            directory, capsys, row=row, states=states)
        saved = json.loads(pricing_path.read_text(encoding="utf-8"))
        return (rc, err, constants_path.read_text(encoding="utf-8"),
                out, saved["providers"][MODEL][HOST])

    # The stored row itself differs (the band key), so the runs are compared
    # on everything the run PRODUCED: its exit code, its report, the version
    # it wrote and the entry it appended.
    def signature(result):
        return result[:4] + (result[4][-1],)

    plain = run(tmp_path / "plain", unbanded)
    assert plain[4][-1] == {"from": "2031-01-01T00:10:00Z", **RATE_C}, \
        "an unbanded row appends the logged state, as it always has"
    with_band = run(tmp_path / "banded", banded)
    assert signature(with_band) != signature(plain), \
        "the band changed what the run produced"
    monkeypatch.setattr(refresh.price_band, "entry_band", lambda entry: None)
    assert signature(run(tmp_path / "disabled", banded)) == signature(plain)


def test_a_widened_row_writes_a_file_both_rate_loaders_accept(tmp_path, capsys):
    row = _banded_row(RATE_A, RATE_B)
    states = [("2030-12-31T23:00:00Z", RATE_B),
              ("2031-01-01T00:10:00Z", RATE_C)]
    rc, _, err, pricing_path, _ = _log_refresh(
        tmp_path, capsys, row=row, states=states)

    assert rc == 0 and not err
    written = json.loads(pricing_path.read_text(encoding="utf-8"))
    history = written["providers"][MODEL][HOST]
    assert "band" in history[-1]
    assert pricing.load_tables(written)["PROVIDER_RATES"][(MODEL, HOST)] == RATE_MID
    if shutil.which("node"):
        browser_dir = tmp_path / "browser"
        browser_dir.mkdir()
        shutil.copy(pricing_path, browser_dir / "pricing.json")
        shutil.copy(ROOT / "src" / "pricing-loader.js",
                    browser_dir / "pricing-loader.js")
        shutil.copy(ROOT / "src" / "parser.js", browser_dir / "parser.js")
        proc = subprocess.run(
            ["node", "-e", "global.window = {}; "
             "require('./pricing-loader.js'); require('./parser.js'); "
             "console.log(JSON.stringify("
             "window.rateForModel('synthetic/model', '2031-01-01T00:20:00Z', "
             "'Wafer')));"],
            cwd=browser_dir, capture_output=True, text=True, timeout=60, check=False)
        assert proc.returncode == 0, proc.stderr
        priced = json.loads(proc.stdout)
        assert {name: priced[key] for key, name in (
            ("c5", "create_5m"), ("c1h", "create_1h"), ("out", "output"),
        )} | {"fresh": priced["fresh"], "read": priced["read"]} == RATE_MID
