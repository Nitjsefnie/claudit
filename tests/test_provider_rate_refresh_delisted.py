"""Known delistings across catalog outages and subsequent refreshes."""
from __future__ import annotations

import copy
import json
import urllib.error
from datetime import timedelta
from email.message import Message

import pytest

from tests.test_provider_rate_refresh import (
    GLM, NOW, Run, V41, _catalog_without, _move_openinference, refresh)

RETIRED = "stealth/space-bunny-alpha"
UNREADABLE = {"unexpected": "shape"}


def _run(run, capsys, catalog, *, missing_endpoint=None, now=NOW):
    fetched = []

    def fetch(model_id):
        fetched.append(model_id)
        if model_id == missing_endpoint:
            raise urllib.error.HTTPError("https://example.invalid", 404, "not found", Message(), None)
        return copy.deepcopy(run.payloads[model_id])

    rc = refresh.main([], fetch=fetch, fetch_models=lambda: catalog,
                      fetch_log=lambda _slug: {"data": {"series": []}},
                      now=now, pricing_path=run.pricing, constants_path=run.constants,
                      vendor=refresh.refresh_vendor_rates.vendor_pass)
    out, err = capsys.readouterr()
    return rc, out, err, fetched


def _state(run, value):
    run.pricing.with_name("pricing_notices.json").write_text(
        json.dumps(value), encoding="utf-8")


@pytest.mark.parametrize("missing_endpoint", [False, True], ids=["empty", "404"])
def test_known_delisted_is_not_fetched_during_catalog_outage(
        tmp_path, capsys, missing_endpoint):
    run = Run(tmp_path)
    model_id = run.doc()["openrouter"]["models"][RETIRED]["id"]
    other_id = run.doc()["openrouter"]["models"][GLM]["id"]
    run.endpoints(RETIRED).clear()
    _state(run, {"delisted": [None, {}, model_id]})
    before = run.snapshot()

    rc, out, err, fetched = _run(
        run, capsys, UNREADABLE, missing_endpoint=model_id if missing_endpoint else None)

    assert rc == 0 and not err
    assert model_id not in fetched and other_id in fetched
    assert "catalog unreadable" in out and "last recorded as delisted" in out
    assert RETIRED in out
    assert run.snapshot() == before


@pytest.mark.parametrize("missing_endpoint", [False, True], ids=["empty", "404"])
def test_unknown_delisting_still_refuses_during_catalog_outage(
        tmp_path, capsys, missing_endpoint):
    run = Run(tmp_path)
    model_id = run.doc()["openrouter"]["models"][RETIRED]["id"]
    run.endpoints(RETIRED).clear()
    _state(run, {"delisted": ["other/model"]})
    before = run.snapshot()

    rc, _, err, fetched = _run(
        run, capsys, UNREADABLE, missing_endpoint=model_id if missing_endpoint else None)

    assert rc == 1 and model_id in fetched
    assert RETIRED in err
    assert ("404" if missing_endpoint else "no endpoints listed") in err
    assert run.snapshot() == before


def test_readable_catalog_relisting_overrides_the_record(tmp_path, capsys):
    run = Run(tmp_path)
    model_id = run.doc()["openrouter"]["models"][RETIRED]["id"]
    run.endpoints(RETIRED).clear()
    _state(run, {"delisted": [model_id]})

    rc, _, err, fetched = _run(run, capsys, _catalog_without(run))

    assert rc == 1 and model_id in fetched
    assert RETIRED in err and "no endpoints listed" in err


def test_committing_refresh_records_and_carries_only_tracked_delistings(tmp_path, capsys):
    run = Run(tmp_path)
    tracked = run.doc()["openrouter"]["models"]
    retired_ids = sorted([tracked[RETIRED]["id"], tracked[V41]["id"]])
    original_payloads = {model_id: copy.deepcopy(run.payloads[model_id])
                         for model_id in retired_ids}
    run.endpoints(RETIRED).clear()
    run.endpoints(V41).clear()
    _move_openinference(run)

    rc, _, err, fetched = _run(run, capsys, _catalog_without(run, *retired_ids))
    assert rc == 0 and not err and not set(retired_ids) & set(fetched)
    state_path = run.pricing.with_name("pricing_notices.json")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["delisted"] == retired_ids

    state["delisted"].append("untracked/model")
    _state(run, state)
    _move_openinference(run, discount=0.1)
    rc, _, err, fetched = _run(run, capsys, UNREADABLE, now=NOW + timedelta(hours=1))
    assert rc == 0 and not err and not set(retired_ids) & set(fetched)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["delisted"] == retired_ids

    run.payloads.update(original_payloads)
    _move_openinference(run, discount=0.2)
    rc, _, err, fetched = _run(
        run, capsys, _catalog_without(run), now=NOW + timedelta(hours=2))
    assert rc == 0 and not err and set(retired_ids) <= set(fetched)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["delisted"] == []

    run.endpoints(RETIRED).clear()
    rc, _, err, fetched = _run(run, capsys, UNREADABLE, now=NOW + timedelta(hours=3))
    assert rc == 1 and tracked[RETIRED]["id"] in fetched
    assert "no endpoints listed" in err


@pytest.mark.parametrize("contents", [
    None, "directory", b"", b"not json", b"\xff", b"[]", b"null", b"{}",
    json.dumps({"delisted": [None, {}, []]}).encode(),
    json.dumps({"delisted": RETIRED}).encode(),
    json.dumps({"delisted": {RETIRED: True}}).encode(),
    json.dumps({"delisted": 1}).encode(),
], ids=["absent", "unreadable", "empty", "malformed", "undecodable", "array", "null",
        "missing-key", "non-string-items", "string", "mapping", "number"])
def test_unusable_record_does_not_hide_a_refusal(tmp_path, capsys, contents):
    run = Run(tmp_path)
    model_id = run.doc()["openrouter"]["models"][RETIRED]["id"]
    run.endpoints(RETIRED).clear()
    if contents == "directory":
        run.pricing.with_name("pricing_notices.json").mkdir()
    elif contents is not None:
        run.pricing.with_name("pricing_notices.json").write_bytes(contents)

    rc, _, err, fetched = _run(run, capsys, UNREADABLE)

    assert rc == 1 and model_id in fetched
    assert "no endpoints listed" in err
