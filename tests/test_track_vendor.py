"""Dispatch namespace validation uses synthetic catalogs and pricing files."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from backend.pricing_document import expand_pricing_doc, serialize_pricing_doc
from tests.refresh_fixture_builders import seed_doc
from tests.vendor_rate_refresh_helpers import NOW, _load


@pytest.fixture(name="pricing_path")
def _pricing_path(tmp_path: Path) -> Path:
    path = tmp_path / "pricing.json"
    path.write_text(serialize_pricing_doc(seed_doc()), encoding="utf-8", newline="\n")
    return path


@pytest.mark.parametrize("suffix", ["", ":free", ":batch"])
def test_namespace_addition_changes_only_the_prefix_line(pricing_path, suffix):
    before = pricing_path.read_bytes()
    raw = json.loads(before)
    assert serialize_pricing_doc(expand_pricing_doc(raw)).encode() == before
    tracker = _load("track_vendor")
    rc = tracker.main(
        ["--namespace", "acme-2"], pricing_path=pricing_path, now=NOW,
        fetch_models=lambda: {"data": [{"id": f"acme-2/model-test{suffix}"}]})
    assert rc == 0
    assert pricing_path.read_bytes() == before.replace(
        b'"z-ai"\n', b'"z-ai",\n        "acme-2"\n')
    assert set(path.name for path in pricing_path.parent.iterdir()) == {"pricing.json"}


@pytest.mark.parametrize("namespace", ["", "Acme", "-Acme", "acme_ai", "acme/test",
                                       "acme ", "acme\n", "acm\u00e9"])
def test_malformed_namespace_refuses_before_fetch(pricing_path, capsys, namespace):
    before = pricing_path.read_bytes()
    rc = _load("track_vendor").main(
        [f"--namespace={namespace}"], pricing_path=pricing_path, now=NOW,
        fetch_models=pytest.fail)
    assert rc == 1
    err = capsys.readouterr().err
    assert repr(namespace) in err and "malformed" in err
    assert pricing_path.read_bytes() == before


def test_already_tracked_refuses_before_fetch(pricing_path, capsys):
    before = pricing_path.read_bytes()
    rc = _load("track_vendor").main(
        ["--namespace", "openai"], pricing_path=pricing_path, now=NOW,
        fetch_models=pytest.fail)
    assert rc == 1
    err = capsys.readouterr().err
    assert "openai" in err and "already tracked" in err
    assert pricing_path.read_bytes() == before


def test_absent_namespace_requires_an_exact_namespace(pricing_path, capsys):
    before = pricing_path.read_bytes()
    rc = _load("track_vendor").main(
        ["--namespace", "acme"], pricing_path=pricing_path, now=NOW,
        fetch_models=lambda: {"data": [{"id": "acme-extra/test"},
                                       {"id": "other/acme"}]})
    assert rc == 1
    err = capsys.readouterr().err
    assert "acme" in err and "absent" in err
    assert pricing_path.read_bytes() == before


@pytest.mark.parametrize("payload", [None, {}, {"data": {}},
                                     {"data": [{"id": 3}]}])
def test_unreadable_catalog_refuses(pricing_path, capsys, payload):
    before = pricing_path.read_bytes()
    rc = _load("track_vendor").main(
        ["--namespace", "acme"], pricing_path=pricing_path, now=NOW,
        fetch_models=lambda: payload)
    assert rc == 1
    err = capsys.readouterr().err
    assert "acme" in err and "catalog is unreadable" in err
    assert ": " in err.partition("catalog is unreadable")[2]
    assert pricing_path.read_bytes() == before


def test_catalog_fetch_failure_names_namespace_and_reason(pricing_path, capsys):
    before = pricing_path.read_bytes()

    def fetch():
        raise OSError("catalog connection failed")

    rc = _load("track_vendor").main(
        ["--namespace", "acme"], pricing_path=pricing_path, now=NOW,
        fetch_models=fetch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "acme" in err and "catalog connection failed" in err
    assert pricing_path.read_bytes() == before


def test_would_be_document_is_loader_checked(pricing_path, capsys):
    raw = json.loads(pricing_path.read_bytes())
    raw["openrouter"]["vendor"]["prefixes"].append("INVALID")
    pricing_path.write_text(json.dumps(raw), encoding="utf-8", newline="\n")
    before = pricing_path.read_bytes()
    rc = _load("track_vendor").main(
        ["--namespace", "acme"], pricing_path=pricing_path, now=NOW,
        fetch_models=lambda: {"data": [{"id": "acme/test"}]})
    assert rc == 1
    err = capsys.readouterr().err
    assert "acme" in err and "would not load" in err
    assert pricing_path.read_bytes() == before


def test_commit_message_survives_a_prefix_only_refresh(pricing_path, tmp_path):
    message = tmp_path / "commit-msg.txt"
    rc = _load("track_vendor").main(
        ["--namespace", "acme", "--pricing-path", str(pricing_path),
         "--commit-msg", str(message)], now=NOW,
        fetch_models=lambda: {"data": [{"id": "acme/test:free"}]})
    assert rc == 0
    subject, body, captured = message.read_text(encoding="utf-8").strip().split("\n\n")
    assert "acme" in subject
    assert not re.search(r"#\d+|issue", subject, re.IGNORECASE)
    assert "acme" in body
    assert captured == "Captured by .github/workflows/refresh-pricing.yml."
    constants = tmp_path / "constants.py"
    constants.write_text('PRICING_VERSION = "100"\n', encoding="utf-8", newline="\n")
    before = message.read_bytes()
    refresh = _load("refresh_provider_rates")
    assert refresh.main(
        ["--commit-msg", str(message)], now=NOW, pricing_path=pricing_path,
        constants_path=constants, fetch=pytest.fail,
        fetch_models=lambda: {"data": [{"id": "acme/test:free"}]},
        fetch_log=pytest.fail) == 0
    assert message.read_bytes() == before
