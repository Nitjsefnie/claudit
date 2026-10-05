"""The one-time collapse of every oscillating provider row into its band.

A row whose price moved inside a range and returned to levels it had held
before is recorded once, as that range: the entry keeps the row's own
start, its five rate fields become the time-weighted mean over its
history and a band spans every level it held. STEP and STABLE rows are
left exactly as they are.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend import pricing

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / "scripts" / "ci"
sys.path.insert(0, str(CI))


def _load():
    """Import scripts/ci/collapse_oscillating_rates.py by path."""
    path = CI / "collapse_oscillating_rates.py"
    spec = importlib.util.spec_from_file_location("collapse_oscillating_rates", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["collapse_oscillating_rates"] = module
    spec.loader.exec_module(module)
    return module


collapse_script = _load()

MODEL = "synthetic/model"
AS_OF = "2031-01-08T00:00:00Z"
FETCHED = AS_OF  # the file already carries the instant --as-of defaults to
VERSION = 205

RATE_A = {"fresh": 0.30, "create_5m": 0.30, "create_1h": 0.30,
          "read": 0.010, "output": 0.800}
RATE_B = {"fresh": 0.20, "create_5m": 0.20, "create_1h": 0.20,
          "read": 0.020, "output": 0.700}
RATE_C = {"fresh": 0.40, "create_5m": 0.40, "create_1h": 0.40,
          "read": 0.030, "output": 0.900}


def _entry(at: str | None, rates: dict, **extra) -> dict:
    return {"from": at, **rates, **extra}


def _day(n: int) -> str:
    """n days before AS_OF."""
    when = datetime(2031, 1, 8, tzinfo=timezone.utc) - timedelta(days=n)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _levels(count: int, rates: list[dict]) -> list[tuple[str, dict]]:
    """`count` dated entries, one per rate level, all inside the window."""
    return [(_day(6 - i), rates[i % len(rates)]) for i in range(count)]


def _oscillating() -> list[dict]:
    """A row that flips between two levels four times: a TOGGLE."""
    return [_entry(_day(8), RATE_A),
            *[_entry(at, rates) for at, rates in _levels(5, [RATE_B, RATE_A])]]


def _stepping() -> list[dict]:
    """Three levels, never returning: a STEP, which keeps every entry."""
    return [_entry(_day(8), RATE_A), _entry(_day(6), RATE_B),
            _entry(_day(3), RATE_C)]


def _doc(hosts: dict[str, list[dict]]) -> dict:
    return {
        "models": {MODEL: [_entry(None, RATE_A)]},
        "providers": {MODEL: copy.deepcopy(hosts)},
        "provider_rates_fetched": FETCHED,
        "long_context_models": [],
        "openrouter": {"data_region": "global",
                       "models": {MODEL: {"id": "synthetic/model-id"}}},
    }


def _run(tmp_path: Path, capsys, hosts, argv=None, version=VERSION):
    pricing_path = tmp_path / "pricing.json"
    constants_path = tmp_path / "constants.py"
    pricing_path.write_text(json.dumps(_doc(hosts), indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
    constants_path.write_text(f'PRICING_VERSION = "{version}"\n', encoding="utf-8")
    rc = collapse_script.main(argv if argv is not None else [],
                              pricing_path=pricing_path,
                              constants_path=constants_path)
    out, err = capsys.readouterr()
    return rc, out, err, pricing_path, constants_path


def _saved(pricing_path: Path) -> dict:
    return json.loads(pricing_path.read_text(encoding="utf-8"))


# --- the collapse -------------------------------------------------------------


def test_an_oscillating_row_becomes_one_banded_entry(tmp_path, capsys):
    hosts = {"HostCo": _oscillating(), "StepCo": _stepping()}

    rc, out, err, pricing_path, constants_path = _run(tmp_path, capsys, hosts)

    assert rc == 0 and not err
    saved = _saved(pricing_path)
    history = saved["providers"][MODEL]["HostCo"]
    assert len(history) == 1
    assert history[0]["from"] == _day(8), "the row keeps its own start"
    assert history[0]["band"]["fresh"] == [0.2, 0.3]
    assert history[0]["band"]["read"] == [0.01, 0.02]
    assert history[0]["band"]["output"] == [0.7, 0.8]
    assert saved["providers"][MODEL]["StepCo"] == hosts["StepCo"], \
        "a step keeps every entry"
    assert 'PRICING_VERSION = "206"' in constants_path.read_text(encoding="utf-8")
    assert "HostCo: 6 → 1 entries" in out
    assert "StepCo" not in out, "an untouched row is not reported"
    assert pricing.load_tables(saved), "the file must load"


def test_the_mean_is_the_time_weighted_one_over_the_whole_history(tmp_path, capsys):
    """The row holds A for four of its eight days and B for the other four,
    so the entry prices at their mean — neither level."""
    rc, _, err, pricing_path, _ = _run(tmp_path, capsys, {"HostCo": _oscillating()})

    assert rc == 0 and not err
    entry = _saved(pricing_path)["providers"][MODEL]["HostCo"][0]
    assert entry["fresh"] == 0.25
    assert entry["read"] == 0.015
    assert entry["output"] == 0.75


def test_the_window_is_what_the_caller_passes(tmp_path, capsys):
    """The same moves, read through a window they all fall outside of, leave
    the row exactly as it was: --days is what classifies, not the history."""
    hosts = {"HostCo": _oscillating()}

    rc, _, err, pricing_path, _ = _run(
        tmp_path, capsys, hosts, argv=["--days", "1"])

    assert rc == 0 and not err
    assert _saved(pricing_path)["providers"][MODEL]["HostCo"] == hosts["HostCo"]


def test_a_dry_run_reports_without_writing_either_file(tmp_path, capsys):
    hosts = {"HostCo": _oscillating()}
    before = json.dumps(_doc(hosts), indent=2, sort_keys=True) + "\n"

    rc, out, err, pricing_path, constants_path = _run(
        tmp_path, capsys, hosts, argv=["--dry-run"])  # noqa

    assert rc == 0 and not err
    assert "HostCo: 6 → 1 entries" in out
    assert pricing_path.read_text(encoding="utf-8") == before
    assert f'PRICING_VERSION = "{VERSION}"' in constants_path.read_text(encoding="utf-8")


def test_nothing_to_collapse_leaves_the_version_alone(tmp_path, capsys):
    rc, _, err, pricing_path, constants_path = _run(
        tmp_path, capsys, {"StepCo": _stepping()})

    assert rc == 0 and not err
    assert f'PRICING_VERSION = "{VERSION}"' in constants_path.read_text(encoding="utf-8")
    assert _saved(pricing_path)["providers"][MODEL]["StepCo"] == _stepping()


def test_as_of_moves_the_fetched_stamp_when_it_collapsed(tmp_path, capsys):
    rc, _, err, pricing_path, _ = _run(
        tmp_path, capsys, {"HostCo": _oscillating()},
        argv=["--as-of", "2031-01-08T12:00:00Z"])

    assert rc == 0 and not err
    assert _saved(pricing_path)["provider_rates_fetched"] == "2031-01-08T12:00:00Z"


def test_as_of_refuses_a_malformed_instant(tmp_path, capsys):
    """The one spelling both loaders accept, refused before anything is read."""
    with pytest.raises(SystemExit):
        _run(tmp_path, capsys, {"HostCo": _oscillating()},
             argv=["--as-of", "2031-02-01"])
    assert "YYYY-MM-DDTHH:MM:SSZ" in capsys.readouterr().err


# --- the report a human has to act on ----------------------------------------


def test_the_report_names_every_collapsed_host_for_the_records_query(tmp_path, capsys):
    """No record may have been priced through a host whose prices the mean
    now stands for; the distinct host names are what the safety query takes."""
    hosts = {"StreamLake": _oscillating(), "Morph": _oscillating(),
             "StepCo": _stepping()}

    rc, out, _, _, _ = _run(tmp_path, capsys, hosts)

    assert rc == 0
    assert "collapsed hosts (the records-safety query takes exactly these)" in out
    names = out.split("collapsed hosts (the records-safety query takes exactly these)")[1]
    assert "Morph" in names and "StreamLake" in names
    assert "StepCo" not in names
    assert names.count("StreamLake") == 1, "distinct host names, not rows"


def test_a_scheduled_row_is_reported_and_refused(tmp_path, capsys):
    scheduled = _oscillating()
    scheduled[-1]["schedule"] = [{"days": ["sunday"], "rates": RATE_C}]

    rc, out, _, pricing_path, _ = _run(tmp_path, capsys, {"HostCo": scheduled})

    assert rc != 0, "a human has to look at a row the run would not collapse"
    assert "HostCo" in out and "schedule" in out
    assert _saved(pricing_path)["providers"][MODEL]["HostCo"] == scheduled


def test_a_fee_recording_row_is_reported_and_refused(tmp_path, capsys):
    fee = ("web_search $0.005/request not modelled: per-request, "
           "unpriceable from token counts")
    row = _oscillating()
    row[-1]["note"] = fee

    rc, out, _, _, _ = _run(tmp_path, capsys, {"HostCo": row})

    assert rc != 0
    assert "HostCo" in out and "fee" in out
