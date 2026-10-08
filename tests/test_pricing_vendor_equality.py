"""The migration's cost-equality pin (backend and browser) and the
default-row/empty-bare-form refusals, split from test_pricing_vendor.py
to keep that module under its size ceiling — relocation only, no
assertion changed."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta
import pytest

from backend import pricing

from tests.test_vendor_rate_refresh import (
    GPT_ID,
    GPT_KEY,
    TRACKED,
    _band,
    _catalog,
    _doc as _vrf_doc,
    _endpoint,
    _payload,
    _per_token,
    _price,
    _run,
)

from tests.test_pricing_vendor import (
    CUT,
    HOST,
    LOADER_JS,
    PARSER_JS,
    PREFIXES,
    R_NEW,
    R_OLD,
    R_THIRD,
    RATES_JS,
    STAMP,
    UTC,
    VENDOR_TABLES_JS,
    HHMM_JS,
    _clear_caches,
    _doc,
    _entry,
    _install,
    _loader_error,
    needs_node,
    FEE_NOTE,
)

# --- the migration's cost-equality pin ----------------------------------------
# The committed proof: pricing synthetic records through the OLD tables
# (the pre-migration location of the moved rows, hand-built here) and the
# NEW tables (the same rows relocated) must price every record identically.
# The old rows are frozen literals; the live-file binding below pins the
# migrated rows to them at a fixed instant before every cutover, so a later
# refresh append cannot rot either half.


def _old_doc() -> dict:
    """The pre-migration shapes of the moved rows (frozen literals).
    claude-old-window-9 carries a real dated window: the equality claim
    covers pricing ACROSS a cutover, not only windowless list prices."""
    return {
        "long_context_models": [],
        "models": {
            "claude-opus-4-7": [_entry(R_THIRD)],
            "claude-old-opus-9": [_entry(R_OLD)],
            "claude-old-opus-9-1": [_entry(R_THIRD)],
            "claude-old-window-9": [_entry(R_OLD), _entry(R_NEW, STAMP)],
            "claude-old-fold-9": [
                _entry(R_OLD, note="Cache reads at 0.05x base input.")],
        },
        "openrouter": {"data_region": "global",
                       "models": {"claude-old-fold-9": {"id": "acme/claude-old-fold.9"}},
                       "vendor": {"prefixes": PREFIXES, "resolve": {}}},
        "provider_rates_fetched": "2030-01-01T00:00:00Z",
        "providers": {
            "claude-old-fold-9": {"OldHost": [_entry(R_OLD, note=FEE_NOTE)]},
        },
    }


def _new_doc() -> dict:
    """The same rows in their post-migration locations."""
    return {
        "long_context_models": [],
        "models": {"claude-opus-4-7": [_entry(R_THIRD)]},
        "openrouter": {
            "data_region": "global",
            "models": {
                "claude-old-opus-9": {"id": "acme/claude-old-opus.9",
                                      "vendor_host": "NewHost"},
                "claude-old-opus-9-1": {"id": "acme/claude-old-opus-1.9",
                                        "vendor_host": "NewHost"},
                "claude-old-window-9": {"id": "acme/claude-old-window.9",
                                        "vendor_host": "NewHost"},
                "claude-old-fold-9": {"id": "acme/claude-old-fold.9",
                                      "vendor_host": "OldHost"},
            },
            "vendor": {"prefixes": PREFIXES, "resolve": {}},
        },
        "provider_rates_fetched": "2030-01-01T00:00:00Z",
        "providers": {
            "claude-old-opus-9": {"NewHost": [_entry(R_OLD)]},
            "claude-old-opus-9-1": {"NewHost": [_entry(R_THIRD)]},
            "claude-old-window-9": {"NewHost": [_entry(R_OLD),
                                                _entry(R_NEW, STAMP)]},
            "claude-old-fold-9": {"OldHost": [_entry(R_OLD, note=FEE_NOTE)]},
        },
    }


def _probe_records():
    """(model, ts or None, provider or None, tokens) over both documents:
    the dated windows — before, at and after the cutover — the list price,
    ts=None, a [1m] suffix, and the fold's fee-bearing host row."""
    instants = [None, CUT - timedelta(seconds=1), CUT,
                datetime(2031, 6, 1, tzinfo=UTC)]
    tokens = {"fresh": 100_000, "output": 40_000, "eph5": 5_000,
              "eph1h": 2_000, "unsplit_create": 1_000, "read": 90_000}
    cases = []
    for model in ("claude-old-opus-9", "claude-old-opus-9[1m]",
                  "claude-old-opus-9-1", "claude-old-window-9",
                  "claude-old-fold-9"):
        for ts in instants:
            for provider in (None, "OldHost"):
                cases.append((model, ts, provider, tokens))
    return cases


def _price_all(doc, cases):
    tables = pricing.load_tables(doc)
    saved = {name: getattr(pricing, name) for name in tables}
    try:
        for name, value in tables.items():
            setattr(pricing, name, value)
        _clear_caches()
        out = []
        for model, ts, provider, tokens in cases:
            res = pricing.resolve(model, ts, provider)
            cost = pricing.compute_cost(model, ts=ts, res=res, **tokens)
            out.append((res.kind, res.key, res.request_fee, res.scheduled,
                        res.rates, cost))
        return out
    finally:
        for name, value in saved.items():
            setattr(pricing, name, value)
        _clear_caches()


def test_migration_prices_every_record_identically():
    cases = _probe_records()
    old, new = _price_all(_old_doc(), cases), _price_all(_new_doc(), cases)
    for case, a, b in zip(cases, old, new, strict=True):
        assert a == b, case


@needs_node
def test_migration_prices_every_record_identically_in_the_browser(tmp_path):
    """The committed browser half of the equality pin: the same hand-built
    old and migrated documents through window.resolveModelRate and
    computeSessionStats under node — kind, key, fee, rates and cost equal
    for every probe record, dated windows included."""
    cases = _probe_records()
    tokens = cases[0][3]
    payloads = []
    for name, doc in (("old", _old_doc()), ("new", _new_doc())):
        d = tmp_path / name
        d.mkdir()
        (d / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
        shutil.copy(LOADER_JS, d / "pricing-loader.js")
        shutil.copy(VENDOR_TABLES_JS, d / "vendor-tables.js")
        shutil.copy(HHMM_JS, d / "hhmm-spelling.js")
        shutil.copy(RATES_JS, d / "rates.js")
        shutil.copy(PARSER_JS, d / "parser.js")
        script = f"""
          global.window = {{}};
          require('./pricing-loader.js');
          require('./rates.js');
          require('./parser.js');
          const CASES = {json.dumps([[m, ts and ts.isoformat(), p]
                                     for m, ts, p, _ in cases])};
          const T = {json.dumps(tokens)};
          const JF = {{fresh: 'fresh', c5: 'create_5m', c1h: 'create_1h',
                      read: 'read', out: 'output'}};
          const out = CASES.map(([model, ts, provider], i) => {{
            const r = window.resolveModelRate(model, ts, provider);
            const msg = {{ type: 'assistant_usage', line: i, ts, model,
                          usage: {{ input_tokens: T.fresh,
                                    cache_creation_input_tokens: 0,
                                    cache_read_input_tokens: 0,
                                    output_tokens: 0 }} }};
            if (provider !== null) msg.provider = provider;
            return {{ kind: r.kind, key: r.key, fee: r.fee,
                     rates: Object.fromEntries(Object.entries(JF)
                       .map(([a, b]) => [b, r.rates[a]])),
                     cost: window.computeSessionStats([], [msg]).cost }};
          }});
          console.log(JSON.stringify(out));
        """
        proc = subprocess.run(["node", "-e", script], cwd=d, capture_output=True,
                              text=True, timeout=60, check=False)
        assert proc.returncode == 0, proc.stderr
        payloads.append(json.loads(proc.stdout))
    for case, a, b in zip(cases, payloads[0], payloads[1], strict=True):
        assert a == b, case


def test_the_folded_row_prices_the_host_fee_only_via_the_host():
    """The fold case: the models row died, so the bare id prices the
    surviving host row's RATES fee-free, while a record through the host
    pays the host's own per-request fee — on both sides of the migration."""
    tokens = {"fresh": 1_000_000, "output": 0, "eph5": 0, "eph1h": 0,
              "unsplit_create": 0, "read": 0}
    cases = [("claude-old-fold-9", None, None, tokens),
             ("claude-old-fold-9", CUT, "OldHost", tokens)]
    old, new = _price_all(_old_doc(), cases), _price_all(_new_doc(), cases)
    assert old == new
    assert old[0][2] == 0.0, "bare: the host's fee never applies"
    assert old[1][2] == 0.01, "through the host: the fee applies"


# The instant every committed history predates: pricing each migrated row's
# first entry, which an append-only history never rewrites.

# --- the default estimate's row, tracked but rowless ---------------------------


def _rowless_default_doc() -> dict:
    """A document whose claude-opus-4-7 is a tracked entry with no provider
    row yet — the auto-add's pickup-delay shape — and no models-table row."""
    doc = _doc(models={})
    doc["openrouter"]["models"]["claude-opus-4-7"] = {
        "id": "acme/claude-opus-4.7", "vendor_host": HOST}
    return doc


def test_notice_leaves_a_tracked_entry_and_member_untouched():
    """The not-tracked contract's second half: a model with a tracked entry
    and meter membership whose source turns untracked keeps both, byte for
    byte."""
    banded = _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0,
                    overrides=[_band(1.0, 5.0, read=0.1, write=1.25,
                                     write_1h=2.0)])
    scheduled = _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0,
                       overrides=[{"utc_days": ["monday"], "utc_start": 0,
                                   "utc_end": 100, "prompt": _per_token(1.0),
                                   "completion": _per_token(5.0)}])
    before, _ = _run(
        _vrf_doc(tracked={GPT_KEY: dict(TRACKED)}),
        _catalog(GPT_ID), {GPT_ID: _payload(_endpoint("openai", banded))})
    assert GPT_KEY in before["long_context_models"]
    after, out = _run(
        before,
        _catalog(GPT_ID), {GPT_ID: _payload(_endpoint("openai", scheduled))})
    assert out.refusals == [] and len(out.notices) == 1
    assert "not tracked" in out.notices[0]
    assert after["openrouter"]["models"][GPT_KEY] == TRACKED
    assert GPT_KEY in after["long_context_models"]
    assert out.moves == []


def test_a_tracked_but_rowless_default_row_refuses_the_backend():
    """Discriminated from the plain missing row: claude-opus-4-7 IS a
    tracked key here, but the row its bare path would read does not exist,
    so the default estimate would price a KeyError — the document refuses,
    naming the row."""
    with pytest.raises(ValueError,
                       match="no claude-opus-4-7 row prices the default estimate"):
        pricing.load_tables(_rowless_default_doc())


@needs_node
def test_the_browser_tier_fallback_skips_a_rowless_tracked_key(tmp_path):
    """The browser twin of the rowless skip: a tracked claude-family key
    with no provider row yet never wins the fallback race — the tier
    prices from the newest key that HAS rates, never undefined, and the
    rowless bare id falls through to the same tier."""
    doc = _doc(models={})
    doc["providers"] = {}
    doc["openrouter"]["models"]["claude-opus-9"] = {
        "id": "acme/claude-opus.9", "vendor_host": HOST}
    doc["models"]["claude-opus-4-7"] = [_entry(R_THIRD)]
    (tmp_path / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
    shutil.copy(LOADER_JS, tmp_path / "pricing-loader.js")
    shutil.copy(VENDOR_TABLES_JS, tmp_path / "vendor-tables.js")
    shutil.copy(HHMM_JS, tmp_path / "hhmm-spelling.js")
    shutil.copy(RATES_JS, tmp_path / "rates.js")
    proc = subprocess.run(
        ["node", "-e", """
          global.window = {};
          require('./pricing-loader.js');
          require('./rates.js');
          const JF = {fresh: 'fresh', c5: 'create_5m', c1h: 'create_1h',
                      read: 'read', out: 'output'};
          const map = (r) => Object.fromEntries(
            Object.entries(JF).map(([a, b]) => [b, r.rates[a]]));
          const tier = window.resolveModelRate('claude-opus-99');
          const bare = window.resolveModelRate('claude-opus-9');
          console.log(JSON.stringify(
            {tier: {kind: tier.kind, rates: map(tier)},
             bare: {kind: bare.kind, rates: map(bare)}}));
        """],
        cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got["tier"] == {"kind": "tier", "rates": dict(R_THIRD)}
    assert got["bare"] == {"kind": "tier", "rates": dict(R_THIRD)}


@needs_node
def test_a_tracked_but_rowless_default_row_refuses_the_browser(tmp_path):
    """The browser twin, the same refusal substance: the loader refuses at
    load with the same message — never a raw TypeError at resolve time."""
    assert _loader_error(tmp_path, _rowless_default_doc()) == (
        "pricing.json: no claude-opus-4-7 row prices the default estimate")


def test_a_rowless_tracked_family_key_is_skipped_by_the_tier_fallback(
        monkeypatch):
    """The pickup delay must not jam import: a tracked claude-family key
    with no provider row yet names no rates, so _latest skips it — the
    fallback prices from the newest key that HAS rates and resolve() falls
    through — instead of raising the KeyError that would make
    `import backend.pricing` fail and strand the hourly bot's commit."""
    doc = {
        "long_context_models": [],
        "models": {"claude-opus-4-7": [_entry(R_THIRD)]},
        "openrouter": {"data_region": "global",
                       "models": {"claude-opus-9": {"id": "acme/claude-opus.9",
                                                    "vendor_host": HOST}},
                       "vendor": {"prefixes": PREFIXES, "resolve": {}}},
        "provider_rates_fetched": "2030-01-01T00:00:00Z",
        "providers": {},
    }
    _install(monkeypatch, doc)
    # pylint: disable-next=protected-access
    latest = pricing._latest("opus")
    assert latest == R_THIRD
    monkeypatch.setattr(pricing, "_TIER_FALLBACKS", (  # pylint: disable=protected-access
        (re.compile(r"opus"),
         pricing._latest("opus")),))  # pylint: disable=protected-access
    r = pricing.resolve("claude-opus-99")
    assert (r.kind, r.rates) == ("tier", R_THIRD)
    bare = pricing.resolve("claude-opus-9")
    assert bare.kind == "tier", "the rowless bare id falls through"


def test_a_tracked_key_that_is_its_prefix_refuses_the_backend():
    """A tracked key exactly `<prefix>/` derives an empty bare form: it
    would match every id under the namespace, so the loader refuses it."""
    doc = _doc()
    doc["openrouter"]["models"]["acme/"] = {"id": "acme/", "vendor_host": HOST}
    with pytest.raises(ValueError, match="bare form is empty"):
        pricing.load_tables(doc)


@needs_node
def test_a_tracked_key_that_is_its_prefix_refuses_the_browser(tmp_path):
    """The browser twin of the empty-bare-form refusal."""
    doc = _doc()
    doc["openrouter"]["models"]["acme/"] = {"id": "acme/", "vendor_host": HOST}
    error = _loader_error(tmp_path, doc)
    assert error is not None and "bare form is empty" in error
