"""The hourly refresh's commit message omits a notice line the last
committing run already carried (issue #888), so a model still delisted — or
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
from types import SimpleNamespace

import pytest

from tests.test_provider_rate_refresh import (_catalog_without,
                                              _move_openinference, NOW, Run)

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


def test_the_notice_stays_suppressed_across_consecutive_commits(tmp_path, capsys):
    """The regression this design exists for: the baseline holds the lines
    the last committing run suppressed, so a permanently-delisted model's
    notice does not alternate back into every second commit (issue #888).

    Three commits, because the defect shows on the run AFTER a suppressing
    one — a two-commit case passes against a baseline that drops what it
    suppressed."""
    run = _delisted_run(tmp_path)
    catalog = _catalog_without(run, "stealth/space-bunny-alpha")

    rc, first, _ = run(capsys, catalog=catalog)
    assert rc == 0
    assert _DELISTED in first
    assert _DELISTED in _commit_message(run), "the first commit shows it"

    for hours in (1, 2):
        # A fresh move at a later detection time, so the run commits too.
        _move_openinference(run)
        rc, out, _ = run(capsys, catalog=catalog, now=NOW + timedelta(hours=hours))
        assert rc == 0
        assert "no rate moved" not in out, "the run must commit"
        assert _DELISTED not in out, "the suppressed line must not come back"
        assert _DELISTED not in _commit_message(run)


def test_a_changed_delisting_notice_still_appears(tmp_path, capsys):
    """A grouped line that no longer matches the carried one — a model
    dropped from it — is not suppressed, so a changed notice is never
    hidden (issue #888)."""
    run = _delisted_run(tmp_path)
    (tmp_path / "pricing_notices.json").write_text(
        '{\n  "notices": [\n'
        f'    "delisted from OpenRouter\'s catalog; row kept, not fetched: '
        f'{_DELISTED_SUBJECT}, other-model (other/model)"\n'
        '  ]\n}\n', encoding="utf-8")

    rc, out, _ = run(capsys,
                     catalog=_catalog_without(run, "stealth/space-bunny-alpha"))

    assert rc == 0
    assert _DELISTED in out
    assert _DELISTED in _commit_message(run)


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
])
def test_an_unusable_baseline_suppresses_nothing(tmp_path, capsys, baseline):
    """Every unusable baseline shape under-suppresses: absent, empty, blank,
    malformed, wrong-shaped, key-less, a top-level array or null, a non-list
    ``notices`` (including one whose keys WOULD suppress), and a non-string
    entry. The feature can never hide a notice behind a bad read, nor crash
    the run on one (issue #888)."""
    run = _delisted_run(tmp_path)
    path = tmp_path / "pricing_notices.json"
    if baseline is not None:
        path.write_text(baseline, encoding="utf-8")

    rc, out, _ = run(capsys,
                     catalog=_catalog_without(run, "stealth/space-bunny-alpha"))

    assert rc == 0
    assert _DELISTED in out
    assert _DELISTED in _commit_message(run)


def test_an_undecodable_baseline_suppresses_nothing(tmp_path, capsys):
    """A non-UTF-8 baseline is a read failure like any other, not a crash:
    UnicodeDecodeError is a ValueError, not an OSError."""
    run = _delisted_run(tmp_path)
    (tmp_path / "pricing_notices.json").write_bytes(b"\xff\xfe\x00not utf-8")

    rc, out, _ = run(capsys,
                     catalog=_catalog_without(run, "stealth/space-bunny-alpha"))

    assert rc == 0
    assert _DELISTED in out


def test_a_run_that_commits_records_the_carried_notice_set(tmp_path, capsys):
    """The baseline written by a committing run is its full rendered notice
    set — the line here, not the empty set a show-only record would hold."""
    run = _delisted_run(tmp_path)
    rc, out, _ = run(capsys,
                     catalog=_catalog_without(run, "stealth/space-bunny-alpha"))

    assert rc == 0 and _DELISTED in out
    recorded = (tmp_path / "pricing_notices.json").read_text(encoding="utf-8")
    assert _DELISTED_LINE in recorded


def test_a_quiet_run_leaves_the_baseline_untouched(tmp_path, capsys):
    """A run that appends nothing publishes no commit, so it must not move
    the baseline either (issue #888)."""
    run = Run(tmp_path)
    path = tmp_path / "pricing_notices.json"

    rc, out, _ = run(capsys)

    assert rc == 0 and "no rate moved" in out
    assert not path.exists()


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
    return SimpleNamespace(moves=[], refusals=[], notices=[_VENDOR_NOTICE])


def test_a_vendor_notice_also_stays_suppressed_across_commits(tmp_path, capsys):
    """The baseline carries the vendor pass's notices too: a regression
    dropping that half would oscillate a vendor notice exactly as the
    provider one did (issue #888).

    Three commits, for the same reason the provider case runs three: a
    two-commit case passes against a baseline that records only what it
    showed."""
    run = _delisted_run(tmp_path)
    catalog = _catalog_without(run, "stealth/space-bunny-alpha")

    rc, first, _ = run(capsys, catalog=catalog, vendor=_vendor_pass)
    assert rc == 0 and _VENDOR_LINE in first

    for hours in (1, 2):
        _move_openinference(run)
        rc, out, _ = run(capsys, catalog=catalog, vendor=_vendor_pass,
                         now=NOW + timedelta(hours=hours))
        assert rc == 0 and "no rate moved" not in out
        assert _VENDOR_LINE not in out
        assert _VENDOR_LINE not in _commit_message(run)


def test_the_shipped_baseline_has_the_documents_shape():
    """The committed baseline is repository-managed data the refresh rewrites
    on every committing run, so only its SHAPE is pinned, never its content
    (SV-TEST-DATA).

    The ``sampled`` and ``vanished`` standing-state keys (issue #933) join
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
