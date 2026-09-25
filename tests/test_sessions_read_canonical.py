"""/api/sessions* must filter is_canonical, not re-sort at read time.

SV-CANONICAL-FLAG resolved cross-file uuid dedup at INGEST into
records.is_canonical so read endpoints could filter a boolean instead of
sorting the table per request. /api/sessions never got that memo: it
still deduplicates with a per-request read-time sort, and
/api/sessions/{id} has no dedup at all, so a session whose main file
carries a loser row counts it anyway.

Two guards, one per failure mode (source-level guards are repo
precedent -- tests/test_panel_wiring.py -- for wrongness a green suite
cannot see):

1. A source pin: backend/api_sessions.py must not reintroduce the
   per-request read-time dedup sort. Measured on a 50k-record corpus:
   ~2.0 s per /api/sessions request, against 0.28-0.5 s for the
   rollup-served endpoints.
2. A behavioral net over the mini fixture's cross-file duplicate
   (shared-uuid-1 sits in sess-C.jsonl, agent-aaaa.jsonl AND
   sess-D.jsonl; the lexicographically-first file_key -- the sidecar
   agent-aaaa.jsonl -- is the winner, per the ingest-time rule): the
   uuid counts exactly once, attributed to the winner's session, and
   the detail endpoint drops the loser rows.
"""
from __future__ import annotations

import re
from pathlib import Path

# Importing the fixture function registers it in this module under its
# @pytest.fixture(name="app_with_data") name (test_api_token_types.py
# precedent). These tests are read-only, so they can share the
# module-scoped client.
from test_api import _app_with_data_fixture  # noqa: F401  pylint: disable=unused-import

ROOT = Path(__file__).resolve().parents[1]
SESSIONS = ROOT / "backend" / "api_sessions.py"

# session_id -> (request_count, input_tokens, output_tokens). The
# duplicate uuid counts ONCE, attributed to the winner's session
# (sess-C, via the agent-aaaa.jsonl sidecar); the loser copies
# (sess-C.jsonl's and sess-D.jsonl's) count nowhere.
_EXPECTED_SESSIONS = {
    "sess-A": (1, 100, 200),
    "sess-B": (1, 0, 0),
    "sess-C": (1, 1000, 500),
    "sess-D": (1, 50, 25),
}


def _strip_python_line_comments(src: str) -> str:
    """Drop `#` line comments so prose ABOUT the ban is not read as the
    ban. The lookbehind spares `#` opening a string literal (the only
    `#` this module's string literals could legitimately carry)."""
    return re.sub(r"(?<![:'\"\w])#.*$", "", src, flags=re.M)


def test_sessions_module_has_no_read_time_distinct_on():
    """The read path must filter the ingest-time flag, not sort per request."""
    src = _strip_python_line_comments(SESSIONS.read_text(encoding="utf-8"))
    assert "FROM records" in src, "guard would pass vacuously on an emptied module"
    hits = re.findall(r"DISTINCT\s+ON", src, re.I)
    assert not hits, (
        "backend/api_sessions.py reintroduces read-time dedup "
        f"(SV-CANONICAL-FLAG): {hits}")


def test_list_counts_the_shared_uuid_once(app_with_data):
    """Every session's list row reflects the dedup: the winner's session
    carries the shared uuid once, the loser sessions once excluding it."""
    body = app_with_data.get("/api/sessions?limit=500").json()
    items = {i["session_id"]: i for i in body["items"]}
    assert set(items) == set(_EXPECTED_SESSIONS), sorted(items)
    for sid, (req, inp, out) in _EXPECTED_SESSIONS.items():
        assert items[sid]["request_count"] == req, sid
        assert items[sid]["input_tokens"] == inp, sid
        assert items[sid]["output_tokens"] == out, sid


def test_detail_drops_the_non_canonical_loser_rows(app_with_data):
    """sess-D's main file carries the shared-uuid-1 loser plus its own
    canonical row; only the canonical one may count. (Before the fix the
    detail endpoint aggregated with no canonical filter and returned
    request_count=2, 1050/525.)"""
    body = app_with_data.get("/api/sessions/sess-D").json()
    assert body["request_count"] == 1
    assert body["input_tokens"] == 50
    assert body["output_tokens"] == 25
    assert body["models"] == {"claude-opus-4-7": 1}
    assert body["r2_key"].endswith("sess-D.jsonl")


def test_detail_keeps_fully_canonical_sessions_unchanged(app_with_data):
    """A session whose main file is all-canonical keeps its exact
    pre-filter numbers: the canonical condition must not disturb the
    normal aggregation path. (That a main file with NO canonical
    records still keeps its detail row is pinned separately by
    test_detail_returns_zeroed_row_for_non_canonical_main_file.)"""
    body = app_with_data.get("/api/sessions/sess-A").json()
    assert body["request_count"] == 1
    assert body["input_tokens"] == 100
    assert body["output_tokens"] == 200


def test_detail_returns_zeroed_row_for_non_canonical_main_file(app_with_data):
    """sess-C's main file holds only the non-canonical copy of
    shared-uuid-1 (the canonical winner lives in the agent-aaaa.jsonl
    sidecar, which the detail endpoint -- main-file-only -- does not
    read). This pins the deliberate placement of the canonical condition
    in the LEFT JOIN's ON clause: the file row survives with zeroed
    aggregates -- request_count included (issue #146: COUNT(*) over the
    LEFT JOIN counted the one NULL-extended row the empty canonical
    population still produces). WHERE-clause placement would drop the
    row instead and answer 404 for an existing session."""
    resp = app_with_data.get("/api/sessions/sess-C")
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_count"] == 0
    assert body["input_tokens"] == 0
    assert body["output_tokens"] == 0
    assert body["cache_create_5m_tokens"] == 0
    assert body["cache_create_1h_tokens"] == 0
    assert body["cache_read_tokens"] == 0
    assert body["models"] == {}
