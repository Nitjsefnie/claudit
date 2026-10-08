"""SV-RATE-REFRESH: a price band — the range an oscillating host moves
inside, recorded once (issues #640, #663, #664, #665). Six boundaries, over
synthetic rows:

1. the classifier's four verdicts (BAND / TOGGLE / STEP / STABLE);
2. an in-band listing appends NOTHING — no move, no write, no bump;
3. an out-of-band listing appends ONE entry: the band re-forms (window
   range, window mean), or the price in force is followed when the
   window no longer oscillates;
4. the hourly log path forms a band itself for an unbanded row that
   oscillates — its new states plus ONE band entry, dated at the
   detection instant;
5. the one-time collapse reads the WINDOW that classified the row — an
   old level far from the oscillation neither widens the band nor pulls
   the mean;
6. STEP and STABLE rows byte-identical to today's behaviour.

Plus the band's own shape: a loader accepts it and refuses a non-finite
bound, in both loaders, because an accepted `inf` makes `x <= inf` true of
everything, so the row goes permanently silent and swallows a real repricing.
"""
from __future__ import annotations

import importlib.util
import json
import math
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


from backend import pricing

from tests.refresh_fixture_builders import DEFAULT_ROW
from tests.test_provider_rate_log_refresh import (
    HOST, MODEL, NOW, RATE_A, RATE_B, _doc, _run)

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / "scripts" / "ci"
sys.path.insert(0, str(CI))
needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available")


def _load(name: str):
    path = CI / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


price_band = _load("price_band")
collapse = _load("collapse_oscillating_rates")

AT = datetime(2031, 1, 8, tzinfo=timezone.utc)
STAMP = "2031-01-08T00:00:00Z"
DAYS = 7
FIELDS = pricing.RATE_FIELDS
# The loader half names a synthetic host, not the refresh harness's, so its
# own messages stay readable.
BAND_HOST = "HostCo"
RATE_C = {"fresh": 0.40, "create_5m": 0.40, "create_1h": 0.40,
          "read": 0.030, "output": 0.900}
RATE_HIGH = {"fresh": 0.9, "create_5m": 0.9, "create_1h": 0.9,
             "read": 0.09, "output": 2.9}
RATE_LOW = {"fresh": 0.05, "create_5m": 0.05, "create_1h": 0.05,
            "read": 0.005, "output": 0.25}
# A banded row is priced by the MEAN of the levels its window moved between,
# which equals no listed level. A newest entry priced at a LISTED level
# instead is swallowed by the pre-existing `rates == previous` dedup in
# _new_log_states, which returns before the band is consulted — so boundary 2
# would still pass with the branch that stops the churn deleted. The collapse
# keeps an all-undated row's entries as its window, so an undated entry has
# its own window and the row re-forms from it.
MEAN = {"fresh": 0.25, "create_5m": 0.25, "create_1h": 0.25,
        "read": 0.015, "output": 0.75}


def _entry(at: str | None, rates: dict, **extra) -> dict:
    return {"from": at, **rates, **extra}


def _day(n: int) -> str:
    """n days before AT."""
    return (AT - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(hours: float) -> str:
    """The stamp `hours` before NOW, the refresh tests' detection instant."""
    return (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _band_of(*levels: dict) -> dict:
    return {field: [min(x[field] for x in levels), max(x[field] for x in levels)]
            for field in FIELDS}


def _mean_of(*pairs: tuple[dict, float]) -> dict:
    """The time-weighted mean over (rates, seconds) pairs — the arithmetic
    `time_weighted` runs, computed inline for the expectation."""
    total = sum(d for _, d in pairs)
    return {field: round(sum(rates[field] * d for rates, d in pairs) / total, 10)
            for field in FIELDS}


def _banded_row(*levels: dict) -> list[dict]:
    """The row a collapse left for a history that began without a `from`."""
    return [{"from": None, **MEAN, "band": _band_of(*levels)}]


# --- 1. the shape classifier --------------------------------------------------

_SHAPES = [
    pytest.param([RATE_A, RATE_B], "STABLE", id="stable"),
    pytest.param([RATE_A, RATE_B, RATE_C], "STEP", id="step-never-returns"),
    # Four moves through five levels: never returns to one, but moves under
    # once a day, so a slow repricing rather than an oscillation.
    pytest.param([{**RATE_A, "fresh": 0.20 + 0.05 * i} for i in range(5)],
                 "STEP", id="step-under-one-a-day"),
    pytest.param([RATE_A, RATE_B] * 3, "TOGGLE", id="toggle"),
    pytest.param([{**RATE_A, "fresh": 0.20 + 0.01 * i} for i in range(9)],
                 "BAND", id="band"),
]


@pytest.mark.parametrize("levels,shape", _SHAPES)
def test_the_classifier_names_each_shape(levels, shape):
    """Over a trailing window measured against `at`, never wall clock: one
    dated entry per level, a day apart, all inside it."""
    history = [_entry(_day(DAYS - 1 - i), level)
               for i, level in enumerate(levels)]
    assert price_band.classify(history, AT, DAYS)["shape"] == shape


def test_the_classifier_counts_an_undated_baseline_like_a_dated_one():
    """Issue #836: the row's undated first entry is the level in force
    before the window, so an undated row and its dated equivalent classify
    the same — B then A inside the window is a TOGGLE (two changes, one
    return against the baseline), not a one-change STABLE that needs one
    more move before the row bands."""
    inside = [_entry(_ago(30), RATE_B), _entry(_ago(20), RATE_A)]
    undated = [_entry(None, RATE_A)] + inside
    dated = [_entry(_ago(400), RATE_A)] + inside
    assert price_band.classify(undated, NOW, 7.0)["shape"] == "TOGGLE"
    assert price_band.classify(dated, NOW, 7.0)["shape"] == "TOGGLE"


# --- 4 & 5. the one-time collapse, and the rows it must not touch -------------

# Four days at A then four at B: a return, and a mean of exactly MEAN.
TOGGLE_ROW = [_entry(_day(8), RATE_A), _entry(_day(4), RATE_B), _entry(_day(0), RATE_A)]
STEP_ROW = [_entry(_day(8), RATE_A), _entry(_day(5), RATE_B), _entry(_day(2), RATE_C)]
STABLE_ROW = [_entry(_day(8), RATE_A), _entry(_day(5), RATE_B)]


def test_the_collapse_rewrites_only_the_oscillating_row(tmp_path, capsys):
    """The oscillating row becomes ONE banded entry keeping its own start and
    priced by its mean; every other row is byte-identical. The report names
    the hosts it collapsed, because a mean is only a safe price while no
    record has been priced through that host — the merge runs the records
    query over exactly those names."""
    pricing_path = tmp_path / "pricing.json"
    constants_path = tmp_path / "constants.py"
    doc = {"models": {MODEL: [_entry(None, RATE_A)],
                      "claude-opus-4-7": [dict(DEFAULT_ROW)]},
           "providers": {MODEL: {"BandCo": TOGGLE_ROW, "StepCo": STEP_ROW,
                                 "StableCo": STABLE_ROW}},
           "provider_rates_fetched": STAMP, "long_context_models": [],
           "openrouter": {"data_region": "global",
                          "models": {MODEL: {"id": "synthetic/model-id"}},
                          "vendor": {"prefixes": ["anthropic", "openai",
                                                  "moonshotai", "z-ai"]}}}
    pricing_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
    constants_path.write_text('PRICING_VERSION = "9"\n', encoding="utf-8")
    rc = collapse.main([], pricing_path=pricing_path, constants_path=constants_path)
    out, err = capsys.readouterr()

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    (entry,) = saved["providers"][MODEL]["BandCo"]
    assert entry["from"] == _day(8), "the row keeps its own start"
    assert entry["band"] == _band_of(RATE_A, RATE_B)
    # The baseline level held at the window's open (day 7), so it weighs
    # only from there — 3 days, not the 4 since its own `from` (issue #663).
    window_mean = _mean_of((RATE_A, 3 * 86400), (RATE_B, 4 * 86400))
    assert {field: entry[field] for field in FIELDS} == window_mean
    assert saved["providers"][MODEL]["StepCo"] == STEP_ROW, "a step is untouched"
    assert saved["providers"][MODEL]["StableCo"] == STABLE_ROW
    assert "BandCo: 3 → 1 entries" in out
    named = out.split("(the records-safety query takes exactly these):")[1]
    assert named.split() == ["BandCo"], "and only the collapsed host is named"
    assert 'PRICING_VERSION = "10"' in constants_path.read_text(encoding="utf-8")
    assert pricing.load_tables(saved)["PROVIDER_RATES"][
        MODEL, "BandCo"] == window_mean


def test_the_collapse_band_and_mean_come_from_the_window(tmp_path, capsys):
    """Issue #663: the band spans the WINDOW's levels — the last entry dated
    before it plus every entry in it — not the whole history. A level far
    from the oscillation neither widens the band nor pulls the mean."""
    row = [_entry(_day(30), RATE_HIGH), _entry(_day(8), RATE_A),
           _entry(_day(4), RATE_B), _entry(_day(0), RATE_A)]
    doc = {"models": {MODEL: [_entry(None, RATE_A)]},
           "providers": {MODEL: {"BandCo": row}},
           "provider_rates_fetched": STAMP, "long_context_models": [],
           "openrouter": {"data_region": "global",
                          "models": {MODEL: {"id": "synthetic/model-id"}}}}
    pricing_path = tmp_path / "pricing.json"
    constants_path = tmp_path / "constants.py"
    pricing_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
    constants_path.write_text('PRICING_VERSION = "9"\n', encoding="utf-8")
    rc = collapse.main([], pricing_path=pricing_path, constants_path=constants_path)
    out, err = capsys.readouterr()

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    (entry,) = saved["providers"][MODEL]["BandCo"]
    assert entry["from"] == _day(30), "the row keeps its own start"
    assert entry["band"] == _band_of(RATE_A, RATE_B), \
        "the far old level is outside the window and outside the band"
    # The baseline level held at the window's open (day 7), so it weighs
    # only from there — 3 days, not the 4 since its own `from` (issue #663).
    window_mean = _mean_of((RATE_A, 3 * 86400), (RATE_B, 4 * 86400))
    assert {field: entry[field] for field in FIELDS} == window_mean, \
        "the mean weighs the window's levels, not 22 days of the old level"
    assert "BandCo: 4 → 1 entries" in out
    assert pricing.load_tables(saved)["PROVIDER_RATES"][
        MODEL, "BandCo"] == window_mean


def test_the_collapse_refuses_a_scheduled_row_and_a_fee_note(tmp_path, capsys):
    """A scheduled row and a per-request fee anywhere in the row are kept
    exactly as they are, and the run names them and stays red."""
    fee = ("web_search $0.001/request not modelled: per-request, "
           "unpriceable from token counts")
    fee_row = [_entry(None, RATE_A, note=fee),
               _entry(_day(4), RATE_B), _entry(_day(2), RATE_A),
               _entry(_day(1), RATE_B)]
    schedule = [{"days": ["monday"], "rates": RATE_B}]
    scheduled_row = [_entry(None, RATE_A, schedule=schedule),
                     _entry(_day(4), RATE_B, schedule=schedule),
                     _entry(_day(2), RATE_A, schedule=schedule),
                     _entry(_day(1), RATE_B, schedule=schedule)]
    doc = {"models": {MODEL: [_entry(None, RATE_A)]},
           "providers": {MODEL: {"FeeCo": fee_row, "SchedCo": scheduled_row}},
           "provider_rates_fetched": STAMP, "long_context_models": [],
           "openrouter": {"data_region": "global",
                          "models": {MODEL: {"id": "synthetic/model-id"}}}}
    pricing_path = tmp_path / "pricing.json"
    constants_path = tmp_path / "constants.py"
    pricing_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
    constants_path.write_text('PRICING_VERSION = "9"\n', encoding="utf-8")
    rc = collapse.main([], pricing_path=pricing_path, constants_path=constants_path)
    out, _ = capsys.readouterr()

    assert rc == 1
    assert out.count(" kept ") == 2
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    assert saved["providers"][MODEL]["FeeCo"] == fee_row
    assert saved["providers"][MODEL]["SchedCo"] == scheduled_row


# --- 2 & 3. the hourly rule on a banded log row --------------------------------

def _refresh(tmp_path, capsys, row, states):
    return _run(tmp_path, capsys, history=states, hosts={HOST: row},
                endpoint_rates=states[-1][1], version=71)


def test_an_in_band_listing_appends_nothing_and_commits_nothing(tmp_path, capsys):
    row = _banded_row(RATE_A, RATE_B)
    unchanged = json.dumps(_doc({HOST: row}), indent=2, sort_keys=True) + "\n"
    rc, out, err, pricing_path, constants_path = _refresh(
        tmp_path, capsys, row, [("2031-01-01T00:10:00Z", RATE_B)])

    assert rc == 0 and not err and "no rate moved" in out
    assert pricing_path.read_text(encoding="utf-8") == unchanged, "no write at all"
    assert 'PRICING_VERSION = "71"' in constants_path.read_text(encoding="utf-8")


def test_an_unbanded_row_that_oscillates_gets_its_moves_plus_one_band_entry(
        tmp_path, capsys):
    """Issue #664: the hourly log path forms the band itself — an unbanded
    row whose window classifies TOGGLE or BAND appends its new states and
    ONE band entry dated at the detection instant, so a host that starts
    oscillating after any collapse stops committing every in-range move
    without a rerun of the collapse script."""
    row = [_entry(_ago(5.5), RATE_A), _entry(_ago(5.0), RATE_B)]
    states = [(row[0]["from"], RATE_A), (row[1]["from"], RATE_B),
              (_ago(10.0 / 60.0), RATE_A)]
    rc, out, err, pricing_path, constants_path = _refresh(
        tmp_path, capsys, row, states)

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    history = saved["providers"][MODEL][HOST]
    mean = _mean_of((RATE_A, 1800), (RATE_B, 17400), (RATE_A, 600))
    assert history == row + [
        _entry(_ago(10.0 / 60.0), RATE_A),
        _entry("2031-01-01T00:30:00Z", mean, band=_band_of(RATE_A, RATE_B))]
    assert "with a band" in out, "the report names the band formation"
    assert (f"  changed   {HOST}: 2 entries with a band, newest prices "
            f"fresh {RATE_B['fresh']!r} → {mean['fresh']!r}") in out, \
        "the band line prints the appended entry's window mean, not the listing"
    assert 'PRICING_VERSION = "72"' in constants_path.read_text(encoding="utf-8")


def test_a_new_host_whose_log_oscillates_gets_the_log_plus_one_band_entry(
        tmp_path, capsys):
    """Issue #664, a newly listed host: the whole log is stored, then the
    same formation runs over it — the log's own trailing window oscillates,
    so the run appends ONE band entry dated at the detection instant."""
    states = [(_ago(25.5), RATE_A), (_ago(20.0), RATE_B), (_ago(10.0 / 60.0), RATE_A)]
    rc, out, err, pricing_path, _ = _run(tmp_path, capsys, history=states, hosts={})

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    history = saved["providers"][MODEL][HOST]
    mean = _mean_of((RATE_A, 19800), (RATE_B, 71400), (RATE_A, 600))
    assert history == [_entry(at, rates) for at, rates in states] + [
        _entry("2031-01-01T00:30:00Z", mean, band=_band_of(RATE_A, RATE_B))]
    assert "with a band" in out


def test_escapes_followed_by_an_in_band_state_append_the_price_in_force(
        tmp_path, capsys):
    """Issue #665: two escapes then an in-band state — the window no
    longer oscillates (STEP: three levels, none returning), so the row
    follows the price in force: ONE plain entry, the state now in force,
    dated at its own change point."""
    row = _banded_row(RATE_A, RATE_B)
    states = [("2031-01-01T00:05:00Z", RATE_HIGH),
              ("2031-01-01T00:10:00Z", RATE_LOW),
              ("2031-01-01T00:15:00Z", RATE_B)]
    rc, _, err, pricing_path, constants_path = _refresh(tmp_path, capsys, row, states)

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    history = saved["providers"][MODEL][HOST]
    assert history == [row[0], _entry("2031-01-01T00:15:00Z", RATE_B)]
    assert "band" not in history[-1], "a step carries no band"
    assert (MODEL, HOST) not in pricing.load_tables(saved)["PROVIDER_STARTS"], \
        "so the row still covers records from before the escape"
    assert 'PRICING_VERSION = "72"' in constants_path.read_text(encoding="utf-8")


def test_an_escape_that_keeps_oscillating_re_forms_the_band(tmp_path, capsys):
    """Issue #665: escapes that RETURN within the window keep the row
    oscillating, so ONE entry re-forms the band — the range the window's
    levels span, priced by their time-weighted mean, dated at the last
    escape's change point. The banded entry's note carries over. The
    banded entry and an older level both sit OUTSIDE the window (400h and
    200h ago, window 168h), and the old level sits OUTSIDE the surviving
    band's span, so re-forming from the whole history instead of the
    window's levels widens the band and fails — the reform-side pin of
    issue #663's window semantics."""
    old = {**RATE_B, "fresh": 0.1, "create_5m": 0.1, "create_1h": 0.1,
           "read": 0.004}
    row = [_entry(_ago(400), old),
           {"from": _ago(200), **MEAN, "band": _band_of(RATE_A, RATE_B),
            "note": "10% off"}]
    states = [(_ago(18.5), RATE_HIGH), (_ago(12.5), RATE_LOW),
              (_ago(6.5), RATE_HIGH)]
    rc, out, err, pricing_path, constants_path = _refresh(tmp_path, capsys, row, states)

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    history = saved["providers"][MODEL][HOST]
    # The banded baseline entry predates the window (200h ago, window
    # 168h), so it weighs only from the window's open: 149.5h, not the
    # 181.5h since its own `from` (issue #663). The old level is excluded
    # from the window's levels entirely: the band pins that, the mean pins
    # the clipping.
    mean = _mean_of((MEAN, 538200), (RATE_HIGH, 21600), (RATE_LOW, 21600),
                    (RATE_HIGH, 23400))
    assert history == [row[0], row[1],
                       _entry(_ago(6.5), mean,
                              band=_band_of(MEAN, RATE_HIGH, RATE_LOW),
                              note="10% off")]
    assert "with a band" in out
    assert (f"  changed   {HOST}: 1 entries with a band, newest prices "
            f"fresh {MEAN['fresh']!r} → {mean['fresh']!r}") in out, \
        "the re-formed band line prints the new window mean, not the listing"
    assert 'PRICING_VERSION = "72"' in constants_path.read_text(encoding="utf-8")


def test_formation_forms_nothing_for_a_newest_entry_not_before_the_instant():
    """The guard pins the loaders' strictly-after rule: a newest entry
    dated at or after `at` forms nothing. The window still oscillates
    (A, B, A), so only the guard can return None."""
    at = NOW
    history = [_entry(_ago(72), RATE_A), _entry(_ago(48), RATE_B),
               _entry(at.strftime("%Y-%m-%dT%H:%M:%SZ"), RATE_A)]
    assert price_band.formation(history, at, 7.0) is None


def test_a_forming_window_weighs_its_pre_window_baseline_from_the_open(
        tmp_path, capsys):
    """The formation's window baseline may predate the window: it held at
    the window's open, so it weighs only from there — not the 197.5h since
    its own `from`."""
    row = [_entry(_ago(200), RATE_C), _entry(_ago(5.5), RATE_A),
           _entry(_ago(5.0), RATE_B)]
    states = [(row[0]["from"], RATE_C), (row[1]["from"], RATE_A),
              (row[2]["from"], RATE_B), (_ago(10.0 / 60.0), RATE_A)]
    rc, _, err, pricing_path, _ = _refresh(
        tmp_path, capsys, row, states)

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    history = saved["providers"][MODEL][HOST]
    mean = _mean_of((RATE_C, 585000), (RATE_A, 1800), (RATE_B, 17400),
                    (RATE_A, 600))
    assert history == row + [
        _entry(_ago(10.0 / 60.0), RATE_A),
        _entry("2031-01-01T00:30:00Z", mean, band=_band_of(RATE_A, RATE_B, RATE_C))]


def test_an_undated_banded_entry_re_forms_like_its_dated_equivalent(
        tmp_path, capsys):
    """The level in force at the window's open may be an UNDATED entry
    (42 of the 66 live band entries carry from: null). It is the window's
    baseline: the band and mean weigh it from the window's open — the same
    mean the dated equivalent produces — instead of counting for nothing.
    Red on the code that dropped it: the mean collapsed to the escapes."""
    row = [{"from": None, **MEAN, "band": _band_of(RATE_A, RATE_B),
            "note": "10% off"}]
    states = [(_ago(18.5), RATE_HIGH), (_ago(12.5), RATE_LOW),
              (_ago(6.5), RATE_HIGH)]
    rc, _, err, pricing_path, constants_path = _refresh(tmp_path, capsys, row, states)

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    history = saved["providers"][MODEL][HOST]
    mean = _mean_of((MEAN, 538200), (RATE_HIGH, 21600), (RATE_LOW, 21600),
                    (RATE_HIGH, 23400))
    assert history == [row[0], _entry(_ago(6.5), mean,
                                      band=_band_of(MEAN, RATE_HIGH, RATE_LOW),
                                      note="10% off")]
    assert 'PRICING_VERSION = "72"' in constants_path.read_text(encoding="utf-8")


def test_a_genuine_step_from_a_banded_row_appends_the_price_in_force(
        tmp_path, capsys):
    """Issue #665: a single escape — the window no longer oscillates
    (STABLE: one change), so the row follows the price: ONE plain entry at
    the escape level, dated at its own change point. (Master appends a
    widened entry that keeps the old mean — the defect the issue pins.)"""
    row = [{"from": _ago(154), **MEAN, "band": _band_of(RATE_A, RATE_B)}]
    states = [(_ago(1.5), RATE_HIGH)]
    rc, _, err, pricing_path, constants_path = _refresh(tmp_path, capsys, row, states)

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    history = saved["providers"][MODEL][HOST]
    assert history == [row[0], _entry(_ago(1.5), RATE_HIGH)], \
        "the step is followed, not swallowed by a widened band"
    assert "band" not in history[-1]
    assert 'PRICING_VERSION = "72"' in constants_path.read_text(encoding="utf-8")


# --- the band's own shape, in both loaders ------------------------------------

def _banded_doc(band: object) -> dict:
    return {"models": {MODEL: [_entry(None, MEAN)],
                       "claude-opus-4-7": [dict(DEFAULT_ROW)]},
            "providers": {MODEL: {BAND_HOST: [
                _entry("2026-05-01T00:00:00Z", MEAN, band=band)]}},
            "provider_rates_fetched": STAMP, "long_context_models": [],
            "openrouter": {"data_region": "global", "models": {},
                           "vendor": {"prefixes": ["anthropic", "openai",
                                                   "moonshotai", "z-ai"]}}}


def _node_load(tmp_path: Path, text: str) -> str | None:
    """Require the real pricing-loader.js beside `text`; the load error."""
    (tmp_path / "pricing.json").write_text(text, encoding="utf-8")
    loader = tmp_path / "pricing-loader.js"
    shutil.copy(ROOT / "src" / "pricing-loader.js", loader)
    shutil.copy(ROOT / "src" / "hhmm-spelling.js",
                tmp_path / "hhmm-spelling.js")
    shutil.copy(ROOT / "src" / "vendor-tables.js",
                tmp_path / "vendor-tables.js")
    program = (f"global.window={{}};let e=null;try{{require({str(loader)!r})}}"
               "catch(x){e=x.message}console.log(JSON.stringify(e))")
    proc = subprocess.run(["node", "-e", program], capture_output=True, text=True,
                          timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_banded_row_prices_by_its_five_rate_fields():
    band = _band_of(RATE_A, RATE_B)
    assert pricing.load_tables(_banded_doc(band))[
        "PROVIDER_RATES"][MODEL, BAND_HOST] == MEAN


# JSON has no NaN or Infinity literal, so the only non-finite bound the
# BROWSER can be handed is a number too large to represent.
@pytest.mark.parametrize("band", [
    pytest.param({"fresh": [0.1, math.inf]}, id="infinite"),
    pytest.param({"fresh": [math.nan, 0.4]}, id="nan"),
    pytest.param("0.2..0.4", id="not-a-mapping"),
])
def test_a_malformed_band_is_refused_naming_the_row(band):
    with pytest.raises(ValueError, match=r"synthetic/model via HostCo\[0\]"):
        pricing.load_tables(_banded_doc(band))


@needs_node
def test_an_overflowed_band_bound_is_refused_naming_the_row_in_the_browser(tmp_path):
    text = json.dumps(_banded_doc({"fresh": [0.1, "1e999"]}))
    error = _node_load(tmp_path, text.replace('"1e999"', "1e999"))
    assert error and "synthetic/model via HostCo[0].band[fresh]" in error, error
