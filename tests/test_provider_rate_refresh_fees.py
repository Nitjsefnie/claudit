"""SV-RATE-REFRESH, the two price kinds the table could not read until
anthropic/claude-opus-5.5 was tracked: a listed 1h cache-write price, which
is `create_1h`, and a listed per-request fee, which no token count can
price and is therefore recorded in the row's note instead of refusing the
host.

Every other unmodelled key at a nonzero price still refuses: the carve-out
is the recorded fee, not leniency, and that is what keeps an unmodelled
cost from being dropped in silence.

Driven the same way as test_provider_rate_refresh.py, whose fixture
builders these share: fixture payloads over the seeded view, never the
network.
"""
from __future__ import annotations

import copy

from tests.refresh_fixture_builders import _endpoint, _per_token
from tests.test_provider_rate_refresh import (
    GLM, NOW, RATE_FIELDS, Run, refresh, refresh_prices)
from tests.test_refresh_pricelog import _series

# The fee as OpenRouter lists it: USD per request, not per token.
WEB_SEARCH = "0.01"
FEE_NOTE = ("web_search $0.01/request not modelled: per-request, "
            "unpriceable from token counts")

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


def _fee(run: Run, model: str, host: str, value: str = WEB_SEARCH) -> None:
    run.endpoint(model, host)["pricing"]["web_search"] = value


# --- a per-request fee is recorded, never priced and never dropped -----------


def test_a_listed_per_request_fee_is_recorded_in_the_note_not_refused(tmp_path, capsys):
    run = Run(tmp_path)
    _fee(run, GLM, "OpenInference")
    moved = _move(run, GLM, "OpenInference")
    rc, _, err = run(capsys)
    assert rc == 0, err
    entry = run.doc()["providers"][GLM]["OpenInference"][-1]
    assert entry["note"] == FEE_NOTE
    # The fee enters no rate: the row prices the tokens it can see, and its
    # note says a real cost sits outside them.
    assert {f: entry[f] for f in moved} == moved


def test_a_recorded_fee_rides_beside_a_discount_in_one_note(tmp_path, capsys):
    run = Run(tmp_path)
    _fee(run, GLM, "OpenInference")
    run.endpoint(GLM, "OpenInference")["pricing"]["discount"] = 0.5
    _move(run, GLM, "OpenInference")
    assert run(capsys)[0] == 0
    assert run.doc()["providers"][GLM]["OpenInference"][-1]["note"] == (
        f"50% off; {FEE_NOTE}")


def test_a_free_fee_notes_nothing(tmp_path, capsys):
    run = Run(tmp_path)
    _fee(run, GLM, "OpenInference", "0")
    _move(run, GLM, "OpenInference")
    assert run(capsys)[0] == 0
    assert "note" not in run.doc()["providers"][GLM]["OpenInference"][-1]


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


def test_a_fee_that_is_not_a_number_refuses_its_host(tmp_path, capsys):
    run = Run(tmp_path)
    _fee(run, GLM, "OpenInference", "free")
    before = run.doc()["providers"][GLM]["OpenInference"]
    rc, _, err = run(capsys)
    assert rc != 0 and "fee web_search 'free'" in err
    assert run.doc()["providers"][GLM]["OpenInference"] == before


def test_a_fee_host_is_sampled_even_when_the_log_could_back_it(tmp_path, capsys):
    """The other append path, end to end. A fee-carrying host the log can
    identify is still sampled, so the entry the run writes carries the
    note — log-backed appends carry OpenRouter's own history, which has no
    field for the fee and would leave the cost unrecorded."""
    run = Run(tmp_path)
    _fee(run, GLM, "OpenInference")
    _move(run, GLM, "OpenInference")
    listed = run.endpoint(GLM, "OpenInference")["pricing"]
    rates = refresh_prices.rates_of(listed, "OpenInference")
    rc = refresh.main(
        ["--commit-msg", str(run.commit_msg)],
        fetch=lambda model_id: copy.deepcopy(run.payloads[model_id]),
        fetch_models=lambda: {"data": [
            {"id": source["id"], "canonical_slug": source["id"]}
            for source in run.doc()["openrouter"]["models"].values()]},
        fetch_log=lambda _slug: {"data": {"series": [
            _series(slug="openinference", host="OpenInference", rates=rates)]}},
        now=NOW, pricing_path=run.pricing, constants_path=run.constants,
        vendor=None)
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert "per-request fee the price log cannot carry" in out
    assert run.doc()["providers"][GLM]["OpenInference"][-1]["note"] == FEE_NOTE


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
    assert refresh_prices.rates_of(listed, "h") == CLAUDE_RATES


# --- a regional tag drops under the global filter ----------------------------


def test_a_tag_named_europe_is_a_region_and_not_a_global_endpoint():
    assert refresh_prices.tag_region("google-vertex/europe") == "europe"
    assert refresh_prices.tag_region("google-vertex/global") is None


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
