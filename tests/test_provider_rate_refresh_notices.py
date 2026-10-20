"""The hourly refresh's commit message omits a notice line the last
committing run already carried , so a model still delisted — or
a tag still unrecognised — is not repeated in every commit.

The baseline is the committed ``pricing_notices.json`` beside the pricing
document, holding that run's FULL notice set: the lines it showed and the
lines it suppressed. Reading the baseline from the previous commit's message
instead would drop a suppressed line, which the next commit would re-emit —
alternating forever, which is the failure this file's first test pins.

Split from tests/test_provider_rate_refresh.py, which sits at the
test-module size ceiling; it owns the run harness and the fixture payloads.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from tests.test_provider_rate_refresh import (_catalog_without,
                                              _move_openinference, NOW, Run)
from tests.vendor_rate_refresh_helpers import vendor as vendor_refresh

_NOTICES_JSON = (Path(__file__).resolve().parents[1]
                 / "src" / "pricing_notices.json")

_DELISTED = "delisted"
_DELISTED_SUBJECT = "stealth/space-bunny-alpha (stealth/space-bunny-alpha)"
_DELISTED_LINE = (f"delisted from OpenRouter's catalog; row kept, not fetched: "
                  f"{_DELISTED_SUBJECT}")


def _delisted_run(tmp_path: Path) -> Run:
    """A run that moves OpenInference and delists stealth/space-bunny-alpha:
    it commits while carrying a delisting notice."""
    run = Run(tmp_path, pricing_version="73")
    _move_openinference(run)
    run.endpoints("stealth/space-bunny-alpha").clear()
    return run


def _commit_message(run: Run) -> str:
    return run.commit_msg.read_text(encoding="utf-8")


@pytest.mark.parametrize("baseline", [
    pytest.param(None, id="absent"),
    pytest.param("", id="empty"),
    pytest.param("\n", id="blank"),
    pytest.param("not json at all\n", id="not-json"),
    pytest.param('{"notices": "a string"}\n', id="wrong-shape"),
    pytest.param('{"other": []}\n', id="missing-key"),
    pytest.param("[]\n", id="top-level-array"),
    pytest.param("null\n", id="top-level-null"),
    pytest.param('{"notices": 5}\n', id="non-list"),
    pytest.param(json.dumps({"notices": {_DELISTED_LINE: 1}}) + "\n",
                 id="non-list-that-would-suppress"),
    pytest.param(json.dumps({"notices": [["nested"]]}) + "\n",
                 id="unhashable-entry"),
    pytest.param(b"\xff\xfe\x00not utf-8", id="undecodable"),
    pytest.param(json.dumps({"notices": [
        f"delisted from OpenRouter's catalog; row kept, not fetched: "
        f"{_DELISTED_SUBJECT}, other-model (other/model)"]}) + "\n",
        id="changed-delisting"),
])
def test_an_unusable_baseline_suppresses_nothing(tmp_path, capsys, baseline):
    run = _delisted_run(tmp_path)
    path = tmp_path / "pricing_notices.json"
    if baseline is not None:
        path.write_bytes(baseline if isinstance(baseline, bytes)
                         else baseline.encode("utf-8"))

    rc, out, _ = run(capsys,
                     catalog=_catalog_without(run, "stealth/space-bunny-alpha"))

    assert rc == 0
    assert _DELISTED in out
    assert _DELISTED in _commit_message(run)


def test_a_run_that_commits_records_the_carried_notice_set(tmp_path, capsys):
    """The baseline written by a committing run is its full rendered notice
    set — the line here, not the empty set a show-only record would hold."""
    run = _delisted_run(tmp_path)
    rc, out, _ = run(capsys,
                     catalog=_catalog_without(run, "stealth/space-bunny-alpha"))

    assert rc == 0 and _DELISTED in out
    recorded = (tmp_path / "pricing_notices.json").read_text(encoding="utf-8")
    assert _DELISTED_LINE in recorded


def test_a_dry_run_writes_no_baseline(tmp_path, capsys):
    """--dry-run writes nothing, the baseline included: it is developer-facing
    and must not move the state the next real run reads."""
    run = _delisted_run(tmp_path)

    rc, out, _ = run(capsys, "--dry-run",
                     catalog=_catalog_without(run, "stealth/space-bunny-alpha"))

    assert rc == 0 and _DELISTED in out
    assert not (tmp_path / "pricing_notices.json").exists()


_VENDOR_NOTICE = "openai/alpha: the vendor lists no first-party endpoint; skipped"
_VENDOR_LINE = "the vendor lists no first-party endpoint; skipped: openai/alpha"


def _vendor_pass(*_args):
    """A vendor pass carrying one notice, so the baseline's vendor half is
    exercised end to end."""
    return vendor_refresh.VendorOutcome(notices=[_VENDOR_NOTICE])


@pytest.mark.parametrize("vendor, needle", [
    (None, _DELISTED),
    (_vendor_pass, _VENDOR_LINE),
], ids=["provider", "vendor"])
def test_the_notice_stays_suppressed_across_consecutive_commits(tmp_path, capsys, vendor, needle):
    """The regression this design exists for: the baseline holds the lines
    the last committing run suppressed, so a permanently-delisted model's
    notice does not alternate back into every second commit .

    Three commits, because the defect shows on the run AFTER a suppressing
    one — a two-commit case passes against a baseline that drops what it
    suppressed."""
    run = _delisted_run(tmp_path)
    catalog = _catalog_without(run, "stealth/space-bunny-alpha")

    rc, first, _ = run(capsys, catalog=catalog, vendor=vendor)
    assert rc == 0
    assert needle in first
    assert needle in _commit_message(run), "the first commit shows it"

    for hours in (1, 2):
        # A fresh move at a later detection time, so the run commits too.
        _move_openinference(run)
        rc, out, _ = run(capsys, catalog=catalog, now=NOW + timedelta(hours=hours), vendor=vendor)
        assert rc == 0
        assert "no rate moved" not in out, "the run must commit"
        assert needle not in out, "the suppressed line must not come back"
        assert needle not in _commit_message(run)


def test_the_shipped_baseline_has_the_documents_shape():
    """The committed baseline is repository-managed data the refresh rewrites
    on every committing run, so only its SHAPE is pinned, never its content
    (SV-TEST-DATA).

    The ``sampled`` and ``vanished`` standing-state keys join
    the file at the first committing run on that code; the shipped baseline
    predates it, so they are shape-checked only while present."""
    doc = json.loads(_NOTICES_JSON.read_text(encoding="utf-8"))

    assert set(doc) <= {"notices", "sampled", "vanished"}
    assert isinstance(doc["notices"], list)
    assert all(isinstance(line, str) for line in doc["notices"])
    if "sampled" in doc:
        assert isinstance(doc["sampled"], dict)
        assert all(isinstance(classifications, dict)
                   for classifications in doc["sampled"].values())
    if "vanished" in doc:
        assert isinstance(doc["vanished"], dict)
        assert all(isinstance(hosts, list)
                   for hosts in doc["vanished"].values())
