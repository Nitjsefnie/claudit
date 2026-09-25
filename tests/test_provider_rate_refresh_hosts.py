"""SV-RATE-REFRESH, per host: weekly schedules (pricing.overrides), what is
not modelled, one host's failure staying its own, the price-order twin
notice, tag suffixes and the file edges.

Driven the same way as test_provider_rate_refresh.py, whose fixture
builders these share: fixture payloads over the seeded view, never the
network.
"""
from __future__ import annotations

import copy
import urllib.error
from decimal import Decimal

import pytest

from tests.test_provider_rate_refresh import (
    GLM, NOW, RATE_FIELDS, STAMP, V41, Run, _baseten_twins, _per_token, _refused,
    refresh)

# --- weekly schedules (pricing.overrides) ------------------------------------
# DeepSeek and Alibaba list time-of-day prices; the seeded rows carry them
# as each entry's schedule.


def _deepseek(run: Run) -> dict:
    return run.endpoint(V41, "DeepSeek")


def test_a_host_s_overrides_are_its_entry_schedule(tmp_path, capsys):
    """The payload reproduces the seeded schedule, so nothing moves."""
    run = Run(tmp_path)
    assert run.doc()["providers"][V41]["DeepSeek"][-1]["schedule"]
    before = run.snapshot()
    assert run(capsys)[0] == 0
    assert run.snapshot() == before


def test_a_moved_window_appends_the_whole_schedule(tmp_path, capsys):
    run = Run(tmp_path)
    stored = run.doc()["providers"][V41]["DeepSeek"][-1]
    window = _deepseek(run)["pricing"]["overrides"][0]
    window["prompt"] = _per_token(stored["schedule"][0]["rates"]["fresh"] * 2)
    rc, out, _ = run(capsys)
    assert rc == 0
    entry = run.doc()["providers"][V41]["DeepSeek"][-1]
    assert entry["from"] == STAMP
    assert {f: entry[f] for f in RATE_FIELDS} == {f: stored[f] for f in RATE_FIELDS}
    assert entry["schedule"][0]["rates"]["fresh"] == stored["schedule"][0]["rates"]["fresh"] * 2
    assert entry["schedule"][1:] == stored["schedule"][1:]
    assert "DeepSeek: schedule of" in out


def test_dropped_overrides_append_an_entry_without_a_schedule(tmp_path, capsys):
    run = Run(tmp_path)
    del _deepseek(run)["pricing"]["overrides"]
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][V41]["DeepSeek"][-1]
    assert entry["from"] == STAMP and "schedule" not in entry


def test_an_override_inherits_the_prices_it_does_not_name(tmp_path, capsys):
    run = Run(tmp_path)
    stored = run.doc()["providers"][V41]["DeepSeek"][-1]
    del _deepseek(run)["pricing"]["overrides"][0]["completion"]
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][V41]["DeepSeek"][-1]
    assert entry["schedule"][0]["rates"]["output"] == stored["output"]


def _move_glm(run: Run) -> None:
    """A move elsewhere, so a refused host's run still has one to commit."""
    run.endpoint(GLM, "OpenInference")["pricing"]["completion"] = _per_token(
        run.doc()["providers"][GLM]["OpenInference"][-1]["output"] * 2)


@pytest.mark.parametrize("damage", [
    pytest.param(lambda p: p["overrides"][0].update({"min_prompt_tokens": 200000}),
                 id="long-context-tier"),
    pytest.param(lambda p: p["overrides"][0].update({"utc_days": ["funday"]}),
                 id="unknown-day"),
    pytest.param(lambda p: p["overrides"][1].update({"utc_start": 2400}),
                 id="hour-24"),
    pytest.param(lambda p: p.update({"overrides": {"utc_days": ["monday"]}}),
                 id="not-a-list"),
    pytest.param(lambda p: p.update({"request": "0.001"}), id="per-request-fee"),
    pytest.param(lambda p: p.update({"prompt": "Infinity"}), id="infinite-price"),
    pytest.param(lambda p: p.update({"prompt": "-1"}), id="variable-price"),
    pytest.param(lambda p: p.update({"discount": 1}), id="discount-of-one"),
])
def test_what_is_not_modelled_refuses_only_its_host(tmp_path, capsys, damage):
    run = Run(tmp_path)
    before = run.doc()
    damage(_deepseek(run)["pricing"])
    _move_glm(run)
    run.endpoint(V41, "Novita")["pricing"]["completion"] = _per_token(
        before["providers"][V41]["Novita"][-1]["output"] * 2)
    rc, _, err = run(capsys)
    assert rc != 0 and f"{V41} via DeepSeek" in err
    after = run.doc()
    assert after["providers"][V41]["DeepSeek"] == before["providers"][V41]["DeepSeek"]
    assert after["providers"][V41]["Novita"][-1]["from"] == STAMP, "same model, other host"
    assert after["providers"][GLM]["OpenInference"][-1]["from"] == STAMP


def test_a_zero_price_of_a_kind_not_modelled_is_ignored(tmp_path, capsys):
    run = Run(tmp_path)
    _deepseek(run)["pricing"].update({"request": "0", "image": 0, "web_search": "0.0"})
    before = run.snapshot()
    assert run(capsys)[0] == 0
    assert run.snapshot() == before


# --- one host's failure is its own -------------------------------------------


def test_a_failing_fetch_refuses_only_its_model(tmp_path, capsys):
    run = Run(tmp_path)
    _move_glm(run)
    failing = run.doc()["openrouter"]["models"][V41]["id"]

    def fetch(model_id: str) -> object:
        if model_id == failing:
            raise urllib.error.URLError("connection reset")
        return copy.deepcopy(run.payloads[model_id])

    rc = refresh.main(["--commit-msg", str(run.commit_msg)], fetch=fetch, now=NOW,
                      pricing_path=run.pricing, constants_path=run.constants)
    err = capsys.readouterr().err
    assert rc != 0 and f"{V41}: fetching {failing} failed" in err
    assert run.doc()["providers"][GLM]["OpenInference"][-1]["from"] == STAMP


# --- the blind spot of price order is reported -------------------------------


def test_a_price_order_row_that_rises_is_reported_not_refused(tmp_path, capsys):
    """The tracked twin moving past the other looks exactly like the other
    becoming the row's price, and that always shows as a rise; the run says
    so, and appends the rise."""
    run = Run(tmp_path)
    cheaper, dearer = _baseten_twins(run)
    cheaper["pricing"]["input_cache_read"] = format(
        Decimal(dearer["pricing"]["input_cache_read"]) * 2, "f")
    rc, out, _ = run(capsys)
    assert rc == 0
    assert "possible twin switch" in out and f"{V41} via BaseTen" in out
    assert run.doc()["providers"][V41]["BaseTen"][-1]["read"] == 0.03


def test_a_price_order_row_that_falls_is_not_reported(tmp_path, capsys):
    run = Run(tmp_path)
    cheaper, _ = _baseten_twins(run)
    cheaper["pricing"]["input_cache_read"] = format(
        Decimal(cheaper["pricing"]["input_cache_read"]) * Decimal("0.5"), "f")
    rc, out, _ = run(capsys)
    assert rc == 0 and "possible twin switch" not in out
    assert run.doc()["providers"][V41]["BaseTen"][-1]["from"] == STAMP


# --- tags, and edges ------------------------------------------------------------


def test_a_tag_suffix_neither_region_nor_quantization_is_logged(tmp_path, capsys):
    run = Run(tmp_path)
    run.endpoint(GLM, "Wafer")["tag"] = "wafer/fast"
    rc, out, _ = run(capsys)
    assert rc == 0 and "'wafer/fast' names neither a known region nor a quantization" in out


def test_twins_differing_in_max_prompt_tokens_are_not_identical(tmp_path, capsys):
    run = Run(tmp_path)
    _, dearer = _baseten_twins(run)
    dearer["max_prompt_tokens"] = 4096
    _refused(run, capsys, f"{V41} via BaseTen", "identical")


def test_a_file_with_two_parser_versions_is_refused(tmp_path, capsys):
    run = Run(tmp_path)
    _move_glm(run)
    run.constants.write_text(run.constants.read_text(encoding="utf-8")
                             + '\nPARSER_VERSION = "1"\n', encoding="utf-8")
    _refused(run, capsys, "PARSER_VERSION")


def test_the_subject_counts_vanished_hosts(tmp_path, capsys):
    run = Run(tmp_path)
    _move_glm(run)
    run.endpoints(GLM)[:] = [e for e in run.endpoints(GLM)
                             if e["provider_name"] != "Cloudflare"]
    assert run(capsys)[0] == 0
    assert run.commit_msg.read_text(encoding="utf-8").startswith(
        "Refresh OpenRouter provider rates: 1 changed, 1 vanished")
