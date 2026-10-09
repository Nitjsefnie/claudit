"""SV-RATE-REFRESH treats web search as a per-search rate, separately
from the five token prices carried by OpenRouter's price log.

Driven the same way as test_provider_rate_refresh.py, whose fixture
builders these share: fixture payloads over the seeded view, never the
network.
"""
from __future__ import annotations

import copy
from datetime import timedelta
from decimal import Decimal

from backend.pricing_load import _history
from tests.refresh_fixture_builders import (
    RATES_A, RATES_B, _endpoint, _per_token,
)
from tests.test_provider_rate_refresh import (
    GLM, NOW, RATE_FIELDS, Run, refresh, refresh_prices)
from tests.test_refresh_pricelog import _series

# USD per search, deliberately distinct from per-token rate fields.
# Synthetic per-search rate, deliberately unlike the deployed listing.
WEB_SEARCH = "0.0137"
STAMP = "2031-01-01T00:00:00Z"

# anthropic/claude-opus-5.5: every endpoint lists the 1h cache write at 8
# per million beside the 5m tier's 5, and charges $0.01 a web search.
CLAUDE = "claude-opus-5-5"
CLAUDE_HOST = "Claude Platform on AWS"
CLAUDE_RATES = {"fresh": 4.0, "create_5m": 5.0, "create_1h": 8.0,
                "read": 0.2, "output": 20.0}


def _rates(run: Run, model: str, host: str) -> dict:
    return run.doc()["providers"][model][host][-1]


def _move(run: Run, model: str, host: str) -> dict:
    """Move one host's output, so a run appends an entry whose note is
    readable. Returns the five rate fields — a row's newest entry may also
    carry a band (issue #640), which an appended entry never copies."""
    current = _rates(run, model, host)
    moved = {**current, "output": current["output"] * 2}
    pricing = run.endpoint(model, host)["pricing"]
    pricing["completion"] = _per_token(moved["output"])
    return {field: moved[field] for field in RATE_FIELDS}


def _search_rate(run: Run, model: str, host: str,
                 value: str = WEB_SEARCH) -> None:
    run.endpoint(model, host)["pricing"]["web_search"] = value


# --- web search is a separate rate -----------------------------------------


def test_a_listed_search_price_is_an_explicit_rate(tmp_path, capsys):
    run = Run(tmp_path)
    _search_rate(run, GLM, "OpenInference")
    moved = _move(run, GLM, "OpenInference")
    rc, _, err = run(capsys)
    assert rc == 0, err
    entry = run.doc()["providers"][GLM]["OpenInference"][-1]
    assert entry["web_search"] == 0.0137
    assert "note" not in entry
    assert {f: entry[f] for f in moved} == moved


def test_search_price_and_discount_keep_only_the_discount_note(tmp_path, capsys):
    run = Run(tmp_path)
    _search_rate(run, GLM, "OpenInference")
    run.endpoint(GLM, "OpenInference")["pricing"]["discount"] = 0.5
    _move(run, GLM, "OpenInference")
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][GLM]["OpenInference"][-1]
    assert entry["note"] == "50% off"
    assert entry["web_search"] == 0.0137


def test_absent_search_price_defaults_to_zero():
    assert "web_search" in refresh_prices.PRICED
    assert not hasattr(refresh_prices, "RECORDED_FEES")
    rates = refresh_prices.rates_of(
        {"prompt": "0.000004", "completion": "0.00002"}, "host")
    assert rates.get("web_search", 0) == 0


def test_numeric_search_price_is_usd_per_search():
    rates = refresh_prices.rates_of({
        "prompt": "0.000004", "completion": "0.00002",
        "web_search": 0.0137,
    }, "host")
    assert rates["web_search"] == 0.0137


def test_zero_search_price_is_not_written_as_a_note(tmp_path, capsys):
    run = Run(tmp_path)
    _search_rate(run, GLM, "OpenInference", "0")
    _move(run, GLM, "OpenInference")
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][GLM]["OpenInference"][-1]
    assert "note" not in entry
    assert entry.get("web_search", 0) == 0


def test_another_unmodelled_price_at_a_nonzero_price_still_refuses(tmp_path, capsys):
    run = Run(tmp_path)
    run.endpoint(GLM, "OpenInference")["pricing"].update(
        {"request": "0.001", "web_search": WEB_SEARCH})
    before = run.doc()["providers"][GLM]["OpenInference"]
    _move(run, GLM, "OpenInference")
    rc, _, err = run(capsys)
    assert rc != 0
    assert f"{GLM} via OpenInference" in err and "pricing request '0.001'" in err
    # The recorded fee did not buy the host a pass.
    assert run.doc()["providers"][GLM]["OpenInference"] == before


def test_a_search_rate_that_is_not_a_number_refuses_its_host(tmp_path, capsys):
    run = Run(tmp_path)
    _search_rate(run, GLM, "OpenInference", "free")
    before = run.doc()["providers"][GLM]["OpenInference"]
    rc, _, err = run(capsys)
    assert rc != 0 and "web_search" in err
    assert run.doc()["providers"][GLM]["OpenInference"] == before


def test_search_move_keeps_logged_token_history_and_samples_only_search(
        tmp_path, capsys):
    run = Run(tmp_path)
    old_search_rate = "0.002"

    def set_old_rate(doc):
        entry = doc["providers"][GLM]["OpenInference"][0]
        entry.pop("note", None)
        entry["web_search"] = float(old_search_rate)

    run.edit(set_old_rate)
    _search_rate(run, GLM, "OpenInference")
    _move(run, GLM, "OpenInference")
    listed = run.endpoint(GLM, "OpenInference")["pricing"]
    rates = refresh_prices.rates_of(listed, "OpenInference")
    token_rates = {field: rates[field] for field in RATE_FIELDS}
    log_stamp = "2026-10-01T00:00:00Z"
    rc = refresh.main(
        ["--commit-msg", str(run.commit_msg)],
        fetch=lambda model_id: copy.deepcopy(run.payloads[model_id]),
        fetch_models=lambda: {"data": [
            {"id": source["id"], "canonical_slug": source["id"]}
            for source in run.doc()["openrouter"]["models"].values()]},
        fetch_log=lambda _slug: {"data": {"series": [
            _series(slug="openinference", host="OpenInference",
                    rates=token_rates, at=log_stamp)]}},
        now=NOW, pricing_path=run.pricing, constants_path=run.constants,
        vendor=None)
    out, err = capsys.readouterr()
    assert rc == 0, err
    history = run.doc()["providers"][GLM]["OpenInference"]
    logged, sampled_search = history[-2:]
    assert logged["from"] == log_stamp
    assert logged["web_search"] == float(old_search_rate)
    assert sampled_search["from"] == STAMP
    assert sampled_search["web_search"] == 0.0137
    assert {field: sampled_search[field] for field in RATE_FIELDS} == {
        field: logged[field] for field in RATE_FIELDS}


def test_search_only_move_on_log_backed_host_appends_unchanged_token_rates(
        tmp_path, capsys):
    run = Run(tmp_path)
    old_search_rate = 0.002

    def set_old_rate(doc):
        entry = doc["providers"][GLM]["OpenInference"][0]
        entry.pop("note", None)
        entry["web_search"] = old_search_rate

    run.edit(set_old_rate)
    previous = run.doc()["providers"][GLM]["OpenInference"][-1]
    _search_rate(run, GLM, "OpenInference")
    token_rates = {field: previous[field] for field in RATE_FIELDS}
    log_stamp = "2026-10-01T00:00:00Z"
    rc = refresh.main(
        ["--commit-msg", str(run.commit_msg)],
        fetch=lambda model_id: copy.deepcopy(run.payloads[model_id]),
        fetch_models=lambda: {"data": [
            {"id": source["id"], "canonical_slug": source["id"]}
            for source in run.doc()["openrouter"]["models"].values()]},
        fetch_log=lambda _slug: {"data": {"series": [
            _series(slug="openinference", host="OpenInference",
                    rates=token_rates, at=log_stamp)]}},
        now=NOW, pricing_path=run.pricing, constants_path=run.constants,
        vendor=None)
    out, err = capsys.readouterr()
    assert rc == 0, err

    entry = run.doc()["providers"][GLM]["OpenInference"][-1]
    assert entry["from"] == STAMP
    assert entry["web_search"] == 0.0137
    assert {field: entry[field] for field in RATE_FIELDS} == token_rates
    assert "web_search 0.002 → 0.0137, sampled at detection" in out


def _direct_listing(rates: dict) -> refresh.Listing:
    return refresh.Listing("fixture", {}, rates, None, Decimal(0))


def _days_before_detection(days: int) -> str:
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _render_move(move) -> str:
    result = refresh.Result({}, [move], [], [], [], {})
    return refresh.report(STAMP, result, {move.model: {"id": move.model}})


def test_simultaneous_token_and_search_move_keeps_loadable_history():
    """A token point at detection and the sampled search change share one
    dated entry so pricing_load's strict timestamp order remains valid."""
    token_rates = {**RATES_B, "output": 12.0}
    hosts = {"SearchHost": [{
        "from": None, **RATES_B, "web_search": 0.002,
    }]}

    refresh._append_logged(
        "acme/search-9", hosts, "SearchHost",
        _direct_listing({**token_rates, "web_search": 0.0137}),
        [{"from": STAMP, **token_rates}], NOW)

    history = hosts["SearchHost"]
    _history(history, "acme/search-9 via SearchHost", may_begin=True)
    at_detection = [entry for entry in history if entry.get("from") == STAMP]
    assert len(at_detection) == 1
    assert {field: at_detection[0][field] for field in RATE_FIELDS} == token_rates
    assert at_detection[0]["web_search"] == 0.0137


def test_band_formation_and_search_move_share_detection_timestamp():
    hosts = {"SearchHost": [{
        "from": None, **RATES_A, "web_search": 0.002,
    }]}
    states = [RATES_B, RATES_A, RATES_B, RATES_A]
    entries = [
        {"from": _days_before_detection(days), **rates}
        for days, rates in zip((6, 5, 4, 3), states)
    ]

    move = refresh._append_logged(
        "acme/search-9", hosts, "SearchHost",
        _direct_listing({**RATES_A, "web_search": 0.0137}),
        entries, NOW)

    history = hosts["SearchHost"]
    _history(history, "acme/search-9 via SearchHost", may_begin=True)
    at_detection = [entry for entry in history if entry.get("from") == STAMP]
    assert len(at_detection) == 1
    assert "band" in at_detection[0]
    assert at_detection[0]["web_search"] == 0.0137
    move_report = _render_move(move)
    assert "with a band" in move_report
    assert "web_search 0.002 → 0.0137, sampled at detection" in move_report


def test_search_only_epoch_cannot_turn_a_token_step_into_a_band():
    token_a = {**RATES_A, "output": 4.0}
    token_b = {**token_a, "output": 5.0}
    search_stamp = _days_before_detection(2)
    token_stamp = _days_before_detection(1)
    baseline = {"from": None, **token_a, "web_search": 0.002}
    with_search = {"SearchHost": [
        copy.deepcopy(baseline),
        {"from": search_stamp, **token_a, "web_search": 0.0137},
    ]}
    without_search = {"SearchHost": [copy.deepcopy(baseline)]}
    listing = _direct_listing({**token_b, "web_search": 0.0137})
    entries = [{"from": token_stamp, **token_b}]

    for hosts in (with_search, without_search):
        refresh._append_logged("acme/search-9", hosts, "SearchHost",
                               listing, entries, NOW)

    searched_history = with_search["SearchHost"]
    plain_history = without_search["SearchHost"]
    searched_rates, *_ = _history(
        searched_history, "acme/search-9 via SearchHost", may_begin=True)
    plain_rates, *_ = _history(
        plain_history, "acme/search-9 via SearchHost", may_begin=True)
    assert not any("band" in entry for entry in searched_history)
    assert not any("band" in entry for entry in plain_history)
    assert searched_rates["output"] == plain_rates["output"] == 5.0
    assert searched_history[1]["from"] == search_stamp
    assert searched_history[1]["web_search"] == 0.0137


def test_search_only_repeated_level_does_not_reform_a_band():
    token_a = {**RATES_A, "output": 4.0}
    token_b = {**token_a, "output": 5.0}
    token_c = {**token_a, "output": 6.0}
    band = {field: [min(token_a[field], token_b[field]),
                    max(token_a[field], token_b[field])]
            for field in RATE_FIELDS}
    hosts = {"SearchHost": [
        {"from": None, **token_a, "web_search": 0.002},
        {"from": _days_before_detection(3), **token_a,
         "web_search": 0.0137},
        {"from": _days_before_detection(2), **token_b,
         "web_search": 0.0137, "band": band},
    ]}
    entries = [{"from": _days_before_detection(1), **token_c}]

    move = refresh._append_logged(
        "acme/search-9", hosts, "SearchHost",
        _direct_listing({**token_c, "web_search": 0.0137}), entries, NOW)

    history = hosts["SearchHost"]
    rates, *_ = _history(history, "acme/search-9 via SearchHost",
                         may_begin=True)
    assert move is not None and move.source == "log"
    assert "band" not in history[-1]
    assert rates["output"] == 6.0
    assert history[1]["from"] == _days_before_detection(3)
    assert history[1]["web_search"] == 0.0137


def test_mixed_log_search_move_report_identifies_both_changes():
    token_a = {**RATES_B, "output": 4.0}
    token_b = {**token_a, "output": 5.0}
    hosts = {"SearchHost": [{
        "from": None, **token_a, "web_search": 0.002,
    }]}

    move = refresh._append_logged(
        "acme/search-9", hosts, "SearchHost",
        _direct_listing({**token_b, "web_search": 0.0137}),
        [{"from": _days_before_detection(1), **token_b}], NOW)
    rendered = _render_move(move)

    assert move.entries_appended == 1
    assert "1 log entry" in rendered
    assert "output 4.0 → 5.0" in rendered
    assert "web_search 0.002 → 0.0137, sampled at detection" in rendered
    assert "2 log entries" not in rendered


def test_search_only_move_preserves_token_band():
    band = {field: [value / 2, value * 2]
            for field, value in RATES_B.items()}
    hosts = {"SearchHost": [{
        "from": None, **RATES_B, "web_search": 0.002, "band": band,
    }]}

    refresh._append_logged(
        "acme/search-9", hosts, "SearchHost",
        _direct_listing({**RATES_B, "web_search": 0.0137}), [], NOW)

    history = hosts["SearchHost"]
    _history(history, "acme/search-9 via SearchHost", may_begin=True)
    assert history[-1]["web_search"] == 0.0137
    assert history[-1]["band"] == band


def test_search_change_defers_when_detection_epoch_is_already_committed():
    hosts = {"SearchHost": [{
        "from": STAMP, **RATES_B, "web_search": 0.002,
    }]}
    before = copy.deepcopy(hosts)
    listing = _direct_listing({**RATES_B, "web_search": 0.0137})
    notices = []

    move = refresh._append_logged(
        "acme/search-9", hosts, "SearchHost", listing, [], NOW,
        deferred_notices=notices)

    assert move is None
    assert hosts == before
    assert len(notices) == 1
    assert "web_search change deferred" in notices[0]
    assert "next append" in notices[0]

    later = NOW + timedelta(hours=1)
    move = refresh._append_logged(
        "acme/search-9", hosts, "SearchHost", listing, [], later,
        deferred_notices=notices)
    _history(hosts["SearchHost"], "acme/search-9 via SearchHost",
             may_begin=True)
    assert move is not None and move.source == "search"
    assert hosts["SearchHost"][0] == before["SearchHost"][0]
    assert hosts["SearchHost"][-1]["from"] == "2031-01-01T01:00:00Z"
    assert hosts["SearchHost"][-1]["web_search"] == 0.0137


def test_search_change_lands_on_the_next_token_append_after_conflict():
    hosts = {"SearchHost": [{
        "from": STAMP, **RATES_B, "web_search": 0.002,
    }]}
    committed = copy.deepcopy(hosts["SearchHost"][0])
    later = NOW + timedelta(hours=1)
    later_stamp = later.strftime("%Y-%m-%dT%H:%M:%SZ")
    notices = []

    move = refresh._append_logged(
        "acme/search-9", hosts, "SearchHost",
        _direct_listing({**RATES_A, "web_search": 0.0137}),
        [{"from": later_stamp, **RATES_A}], NOW,
        deferred_notices=notices)

    _history(hosts["SearchHost"], "acme/search-9 via SearchHost",
             may_begin=True)
    assert move is not None and move.source == "log"
    assert hosts["SearchHost"][0] == committed
    assert hosts["SearchHost"][1]["from"] == later_stamp
    assert {field: hosts["SearchHost"][1][field]
            for field in RATE_FIELDS} == RATES_A
    assert hosts["SearchHost"][1]["web_search"] == 0.0137
    assert notices and later_stamp in notices[0]


def test_refresh_reports_deferred_search_change_without_rewriting_history(
        tmp_path, capsys):
    run = Run(tmp_path)

    def set_committed_search_epoch(doc):
        entry = doc["providers"][GLM]["OpenInference"][0]
        entry["from"] = STAMP
        entry["web_search"] = 0.002
        entry.pop("note", None)

    run.edit(set_committed_search_epoch)
    previous = copy.deepcopy(run.doc()["providers"][GLM]["OpenInference"])
    _search_rate(run, GLM, "OpenInference")
    token_rates = {field: previous[-1][field] for field in RATE_FIELDS}
    rc = refresh.main(
        ["--commit-msg", str(run.commit_msg)],
        fetch=lambda model_id: copy.deepcopy(run.payloads[model_id]),
        fetch_models=lambda: {"data": [
            {"id": source["id"], "canonical_slug": source["id"]}
            for source in run.doc()["openrouter"]["models"].values()]},
        fetch_log=lambda _slug: {"data": {"series": [
            _series(slug="openinference", host="OpenInference",
                    rates=token_rates, at=STAMP)]}},
        now=NOW, pricing_path=run.pricing, constants_path=run.constants,
        vendor=None)
    out, err = capsys.readouterr()

    assert rc == 0, err
    assert f"web_search change deferred from {STAMP} until the next append" in out
    assert run.doc()["providers"][GLM]["OpenInference"] == previous


def test_prior_search_epochs_do_not_disable_band_reform():
    band = {field: [value / 2, value * 2]
            for field, value in RATES_B.items()}
    hosts = {"SearchHost": [
        {"from": None, **RATES_B, "web_search": 0.002},
        {"from": _days_before_detection(6), **RATES_B,
         "web_search": 0.003, "band": band},
    ]}
    entries = [{"from": _days_before_detection(5), **RATES_A}]

    move = refresh._append_logged(
        "acme/search-9", hosts, "SearchHost",
        _direct_listing({**RATES_A, "web_search": 0.003}), entries, NOW)

    assert move is None
    assert len(hosts["SearchHost"]) == 2


def test_prior_search_epochs_do_not_disable_band_formation():
    hosts = {"SearchHost": [
        {"from": None, **RATES_A, "web_search": 0.002},
        {"from": _days_before_detection(8), **RATES_A,
         "web_search": 0.003},
    ]}
    states = [RATES_B, RATES_A, RATES_B, RATES_A]
    entries = [
        {"from": _days_before_detection(days), **rates}
        for days, rates in zip((6, 5, 4, 3), states)
    ]

    refresh._append_logged(
        "acme/search-9", hosts, "SearchHost",
        _direct_listing({**RATES_A, "web_search": 0.003}), entries, NOW)

    formed = [entry for entry in hosts["SearchHost"]
              if entry.get("from") == STAMP]
    assert len(formed) == 1
    assert "band" in formed[0]
    assert formed[0]["web_search"] == 0.003


# --- a listed 1h cache write is the create_1h rate --------------------------


def test_a_listed_1h_write_price_becomes_the_create_1h_rate(tmp_path, capsys):
    run = Run(tmp_path)
    listing = run.endpoint(CLAUDE, CLAUDE_HOST)["pricing"]
    assert listing["input_cache_write_1h"] == "0.000008"
    listing["input_cache_write_1h"] = "0.000009"
    _move(run, CLAUDE, CLAUDE_HOST)
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][CLAUDE][CLAUDE_HOST][-1]
    assert entry["create_1h"] == 9.0, "the 1h tier moved with the listing"
    assert entry["create_5m"] == 5.0, "the 5m tier is not the 1h one"


def test_a_listing_that_reproduces_the_row_moves_nothing(tmp_path, capsys):
    run = Run(tmp_path)
    before = run.snapshot()
    assert run(capsys)[0] == 0
    assert run.snapshot() == before, "the row's own listing reproduces it, 1h tier included"


def test_a_listing_without_a_1h_write_price_keeps_the_tiers_equal():
    rates = refresh_prices.rates_of(
        {"prompt": "0.000004", "completion": "0.00002",
         "input_cache_write": "0.000005"}, "h")
    assert rates["create_5m"] == 5.0 and rates["create_1h"] == 5.0


def test_a_1h_tier_round_trips_through_the_listing_inverse():
    listed = refresh_prices.as_listed(CLAUDE_RATES)
    assert listed["input_cache_write"] == "0.000005"
    assert listed["input_cache_write_1h"] == "0.000008"
    round_trip = refresh_prices.rates_of(listed, "h")
    assert {field: round_trip[field] for field in RATE_FIELDS} == CLAUDE_RATES
    assert round_trip.get("web_search", 0) == 0


# --- a different-region tag drops under the global filter --------------------


def test_global_suffix_is_recognized_as_the_configured_region():
    assert refresh_prices.tag_region("google-vertex/europe") == "europe"
    assert refresh_prices.tag_region("google-vertex/global") == "global"


def test_a_regional_twin_beside_the_global_endpoint_is_not_an_ambiguous_host(
        tmp_path, capsys):
    """An out-of-region twin at another price is not two prices to choose
    between: the account is never billed by it, so the host lists one."""
    run = Run(tmp_path)
    dearer = {field: _rates(run, CLAUDE, CLAUDE_HOST)[field] * 1.1
              for field in CLAUDE_RATES}
    twin = _endpoint(CLAUDE_HOST, dearer, tag="claude-on-aws/europe")
    run.endpoint(CLAUDE, CLAUDE_HOST)["tag"] = "claude-on-aws"
    run.endpoints(CLAUDE).append(twin)
    before = run.doc()["providers"][CLAUDE][CLAUDE_HOST]
    rc, _, err = run(capsys)
    assert rc == 0, err
    assert run.doc()["providers"][CLAUDE][CLAUDE_HOST] == before
