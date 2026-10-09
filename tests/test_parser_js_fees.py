"""Browser pricing charges an explicit web-search rate per search."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.refresh_fixture_builders import RATES_B, seed_doc

ROOT = Path(__file__).resolve().parents[1]
LOADER_JS = ROOT / "src" / "pricing-loader.js"
VENDOR_TABLES_JS = ROOT / "src" / "vendor-tables.js"
HHMM_JS = ROOT / "src" / "hhmm-spelling.js"
PARSER_USAGE_JS = ROOT / "src" / "parser-usage.js"
PARSER_JS = ROOT / "src" / "parser.js"
RATES_JS = ROOT / "src" / "rates.js"
RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")

# Synthetic per-search rates, deliberately unlike the deployed listing.
SEARCH_RATE = 0.0137
SEARCH_RATE_2 = 0.045
CUTOVER = "2026-08-01T00:00:00Z"
OLD_FEE_NOTE = ("web_search $0.0137/request not modelled: per-request, "
                "unpriceable from token counts")


def _entry(rates: dict, **extra) -> dict:
    return {"from": None, **{field: rates[field] for field in RATE_FIELDS},
            **extra}


def _doc(*, note: str | None = None) -> dict:
    first = _entry(RATES_B, web_search=SEARCH_RATE)
    second = {**_entry(RATES_B, web_search=SEARCH_RATE_2),
              "from": CUTOVER}
    if note is not None:
        first = _entry(RATES_B, note=note)
        second = {**_entry(RATES_B, note=note), "from": CUTOVER}
    return seed_doc(
        models={"acme/acme-9": [_entry(RATES_B)]},
        providers={"acme/acme-9": {"HostCo": [first, second]}},
    )


@pytest.fixture(name="sandbox")
def _sandbox(tmp_path, monkeypatch):
    for source, name in (
        (LOADER_JS, "pricing-loader.js"),
        (VENDOR_TABLES_JS, "vendor-tables.js"),
        (HHMM_JS, "hhmm-spelling.js"),
        (PARSER_USAGE_JS, "parser-usage.js"),
        (PARSER_JS, "parser.js"),
        (RATES_JS, "rates.js"),
    ):
        shutil.copy(source, tmp_path / name)
    (tmp_path / "pricing.json").write_text(
        json.dumps(_doc(), indent=2, sort_keys=True), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _node(sandbox, script: str) -> dict:
    bootstrap = f"""
      global.window = {{}};
      require({str(sandbox / 'pricing-loader.js')!r});
      require({str(sandbox / 'rates.js')!r});
      require({str(sandbox / 'parser.js')!r});
    """
    proc = subprocess.run(
        ["node", "-e", bootstrap + script],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_browser_resolves_search_rate_at_the_records_timestamp(sandbox):
    got = _node(sandbox, """
      const rate = (ts) => window.resolveModelRate(
        'acme/acme-9', ts, 'HostCo').rates.search;
      console.log(JSON.stringify({
        before: rate('2026-07-01T00:00:00Z'),
        after: rate('2026-08-02T00:00:00Z'),
        list: rate(null),
      }));
    """)
    assert got == {"before": SEARCH_RATE, "after": SEARCH_RATE_2,
                   "list": SEARCH_RATE_2}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_browser_cost_charges_each_search_and_reports_the_count(sandbox):
    got = _node(sandbox, """
      const usage = {
        type: 'assistant_usage', line: 1,
        ts: '2026-08-02T00:00:00Z', model: 'acme/acme-9',
        provider: 'HostCo', web_search_requests: 3,
        usage: { input_tokens: 1000000, output_tokens: 0,
                 cache_creation_input_tokens: 0,
                 cache_read_input_tokens: 0 },
      };
      const stats = window.computeSessionStats([], [usage]);
      console.log(JSON.stringify({ cost: stats.cost,
                                   searches: stats.webSearchRequests }));
    """)
    assert got == {"cost": pytest.approx(2.0 + 3 * SEARCH_RATE_2),
                   "searches": 3}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_old_fee_shaped_note_is_not_a_rate_or_per_record_charge(sandbox):
    (sandbox / "pricing.json").write_text(
        json.dumps(_doc(note=OLD_FEE_NOTE), indent=2, sort_keys=True),
        encoding="utf-8")
    got = _node(sandbox, """
      const resolved = window.resolveModelRate(
        'acme/acme-9', '2026-07-01T00:00:00Z', 'HostCo');
      const usage = {
        type: 'assistant_usage', line: 1,
        ts: '2026-07-01T00:00:00Z', model: 'acme/acme-9',
        provider: 'HostCo', web_search_requests: null,
        usage: { input_tokens: 1000000, output_tokens: 0,
                 cache_creation_input_tokens: 0,
                 cache_read_input_tokens: 0 },
      };
      console.log(JSON.stringify({
        searchRate: resolved.rates.search ?? 0,
        hasFee: Object.prototype.hasOwnProperty.call(resolved, 'fee'),
        cost: window.computeSessionStats([], [usage]).cost,
      }));
    """)
    assert got == {"searchRate": 0, "hasFee": False, "cost": 2.0}
