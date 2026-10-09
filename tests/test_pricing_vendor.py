"""Task A of issue 851: vendor first-party rows live in the tracked table.

`openrouter.vendor.prefixes` configures the namespaces; a tracked entry
carrying `vendor_host` points at the (tracked key, host) provider row whose
history prices the vendor's bare first-party id. resolve() matches the BARE
form (the models table's own suffix rules), pricing exactly as the old
models-table rows did — the migration's cost-equality contract. A transcript
id with a tracked vendor prefix folds to its tracked bare form before the
provider and vendor rows are resolved.

SV-TEST-DATA: synthetic rows throughout; the one live-file test binds at a
fixed instant before every committed cutover, the shape SV-RATE-DATA allows.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from tests.refresh_fixture_builders import DEFAULT_ROW


from backend import long_context, pricing

ROOT = Path(__file__).resolve().parents[1]
LOADER_JS = ROOT / "src" / "pricing-loader.js"
VENDOR_TABLES_JS = ROOT / "src" / "vendor-tables.js"
HHMM_JS = ROOT / "src" / "hhmm-spelling.js"
RATES_JS = ROOT / "src" / "rates.js"
PARSER_JS = ROOT / "src" / "parser.js"
JS_FIELDS = {"fresh": "fresh", "c5": "create_5m", "c1h": "create_1h",
             "read": "read", "out": "output"}
RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")
UTC = timezone.utc
needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available")

R_OLD = {"fresh": 3.0, "create_5m": 3.75, "create_1h": 6.0,
         "read": 0.3, "output": 15.0}
R_NEW = {"fresh": 4.0, "create_5m": 5.0, "create_1h": 8.0,
         "read": 0.4, "output": 20.0}
# The shared default-row literal's vector (issue #858): the five
# rate fields only — the row wrappers add "from" themselves.
R_THIRD = {f: DEFAULT_ROW[f] for f in RATE_FIELDS}
CUT = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
STAMP = CUT.isoformat().replace("+00:00", "Z")
KEY = "claude-opus-9"          # bare tracked key: its own bare form
PKEY = "acme/glm-acme-9"       # prefixed tracked key: bare form glm-acme-9
BARE = "glm-acme-9"
HOST = "AcmeHost"
ROUTER_HOST = "RouterHost"
DATED_ROUTER_HOST = "DatedRouterHost"
PREFIXED_BARE = f"acme/{BARE}"
SEARCH_RATE = 0.0137
PREFIXES = ["anthropic", "openai", "moonshotai", "z-ai"]


def _entry(rates, frm=None, **extra):
    return {"from": frm, **{f: rates[f] for f in RATE_FIELDS}, **extra}


def _doc(*, vendor_host=True, prefixed=True, schedules=False, web_search=False,
         start=None, models=None, no_prefixes=False, prefixes=None):
    """A shape-valid synthetic document: tracked vendor rows (one bare
    claude-style key, one acme/-prefixed), one tracked non-vendor entry,
    and a models table keeping a key of its own."""
    if start is None:
        acme_history = [_entry(R_OLD), _entry(R_NEW, STAMP)]
    else:
        stamp = start.isoformat().replace("+00:00", "Z")
        acme_history = [_entry(R_OLD, stamp), _entry(R_NEW, STAMP)]
    if web_search:
        for entry in acme_history:
            entry["web_search"] = SEARCH_RATE
    if schedules:
        acme_history[0]["schedule"] = [
            {"days": ["saturday"], "rates": R_THIRD}]
    tracked = {
        KEY: {"id": "acme/claude-opus.9",
              **({"vendor_host": HOST} if vendor_host else {})},
        "acme/other-9": {"id": "acme/other.9"},
    }
    if prefixed:
        tracked[PKEY] = {"id": "acme/glm-acme.9", "vendor_host": HOST}
        providers = {KEY: {HOST: acme_history},
                     PKEY: {HOST: [_entry(R_THIRD)]}}
    else:
        providers = {KEY: {HOST: acme_history}}
    return {
        "long_context_models": [],
        "models": dict(models if models is not None
                       else {"claude-opus-4-7": [_entry(R_THIRD)]}),
        "openrouter": {"data_region": "global", "models": tracked,
                       "vendor": {"resolve": {},
                                  **({} if no_prefixes else
                                     {"prefixes": prefixes
                                      if prefixes is not None else ["acme"]})}},
        "provider_rates_fetched": "2030-01-01T00:00:00Z",
        "providers": providers,
    }


def _bare_id_doc():
    """A synthetic document whose GLM-shaped vendor model is tracked bare."""
    doc = _doc(prefixed=False)
    doc["openrouter"]["models"][BARE] = {
        "id": "acme/glm-acme.9", "vendor_host": HOST}
    doc["providers"][BARE] = {
        HOST: [_entry(R_OLD), _entry(R_NEW, STAMP)],
        ROUTER_HOST: [_entry(R_OLD)],
        DATED_ROUTER_HOST: [_entry(R_OLD), _entry(R_NEW, STAMP)],
    }
    return doc


def _clear_caches() -> None:
    pricing._MATCH_KEY_CACHE.clear()  # pylint: disable=protected-access
    pricing._VENDOR_MATCH_CACHE.clear()  # pylint: disable=protected-access


def _install(monkeypatch, doc):
    for name, value in pricing.load_tables(doc).items():
        monkeypatch.setattr(pricing, name, value)
    _clear_caches()
    return doc


def _node_raw(script: str):
    proc = subprocess.run(
        ["node", "-"], input=script, capture_output=True, text=True, timeout=60,
        check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _loader_error(tmp_path: Path, doc: dict) -> str | None:
    """The pricing-loader's load error for `doc`, from a tmp sandbox: null
    when the document loads."""
    (tmp_path / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
    shutil.copy(LOADER_JS, tmp_path / "pricing-loader.js")
    shutil.copy(VENDOR_TABLES_JS, tmp_path / "vendor-tables.js")
    shutil.copy(HHMM_JS, tmp_path / "hhmm-spelling.js")
    proc = subprocess.run(
        ["node", "-e",
         "global.window={};let e=null;try{require('./pricing-loader.js')}"
         "catch(x){e=x.message}console.log(JSON.stringify(e))"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _copy_browser(tmp_path):
    shutil.copy(LOADER_JS, tmp_path / "pricing-loader.js")
    shutil.copy(VENDOR_TABLES_JS, tmp_path / "vendor-tables.js")
    shutil.copy(HHMM_JS, tmp_path / "hhmm-spelling.js")
    shutil.copy(RATES_JS, tmp_path / "rates.js")
    shutil.copy(PARSER_JS, tmp_path / "parser.js")


def _js_rates(js: dict) -> dict:
    return {py: js[k] for k, py in JS_FIELDS.items()}


def _load_vendor():
    path = ROOT / "scripts" / "ci" / "refresh_vendor_rates.py"
    spec = importlib.util.spec_from_file_location("refresh_vendor_rates", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["refresh_vendor_rates"] = module
    spec.loader.exec_module(module)
    return module


# --- the loaders build and validate the vendor tables ------------------------


def test_the_loader_builds_the_vendor_tables(monkeypatch):
    doc = _install(monkeypatch, _doc())
    assert pricing.VENDOR_BARE == {KEY: KEY, BARE: PKEY}
    assert pricing.VENDOR_HOSTS == {KEY: HOST, PKEY: HOST}
    # A tracked entry without vendor_host is not vendor data.
    assert "acme/other-9" not in pricing.VENDOR_BARE
    assert doc  # the doc was loaded (shape-valid)


def test_a_bare_only_vendor_shape_loads_and_exports_fold_data():
    tables = pricing.load_tables(_bare_id_doc())
    assert tables["VENDOR_PREFIXES"] == ["acme"]
    assert tables["VENDOR_BARE"] == {KEY: KEY, BARE: BARE}
    assert tables["VENDOR_HOSTS"] == {KEY: HOST, BARE: HOST}


@needs_node
def test_the_browser_builds_the_same_vendor_tables(tmp_path):
    (tmp_path / "pricing.json").write_text(json.dumps(_doc()), encoding="utf-8")
    _copy_browser(tmp_path)
    got = _node_raw(f"""
      global.window = {{}};
      require({str(tmp_path / 'pricing-loader.js')!r});
      console.log(JSON.stringify({{
        bare: window.vendorBare, hosts: window.vendorHosts}}));
    """)
    assert got["bare"] == {KEY: KEY, BARE: PKEY}
    assert got["hosts"] == {KEY: HOST, PKEY: HOST}


@needs_node
def test_the_browser_loads_bare_only_vendor_data_and_exposes_fold_data(tmp_path):
    (tmp_path / "pricing.json").write_text(
        json.dumps(_bare_id_doc()), encoding="utf-8")
    _copy_browser(tmp_path)
    got = _node_raw(f"""
      global.window = {{}};
      require({str(tmp_path / 'pricing-loader.js')!r});
      console.log(JSON.stringify({{
        prefixes: window.vendorPrefixes,
        bareForms: window.vendorBareForms,
        bare: window.vendorBare,
      }}));
    """)
    assert got == {
        "prefixes": ["acme"],
        "bareForms": [KEY, BARE],
        "bare": {KEY: KEY, BARE: BARE},
    }


@needs_node
def test_both_vendor_spellings_are_refused_by_both_loaders(tmp_path):
    doc = _bare_id_doc()
    doc["openrouter"]["models"][PKEY] = {
        "id": "acme/glm-acme.9", "vendor_host": ROUTER_HOST}
    doc["providers"][PKEY] = {ROUTER_HOST: [_entry(R_THIRD)]}

    with pytest.raises(ValueError, match="both carry the bare form"):
        pricing.load_tables(doc)
    error = _loader_error(tmp_path, doc)
    assert error and "both carry the bare form" in error


def test_missing_prefixes_refuse_naming_the_key():
    with pytest.raises(ValueError, match="openrouter.vendor.prefixes"):
        pricing.load_tables(_doc(no_prefixes=True))


def test_malformed_prefixes_refuse_naming_the_key():
    for bad in (["Anthropic"], ["a", "a"], [""], ["x/y"], "anthropic",
                ["anthropic", 7], []):
        with pytest.raises(ValueError, match="openrouter.vendor.prefixes"):
            pricing.load_tables(_doc(prefixes=bad))


@needs_node
@pytest.mark.parametrize("bad", [["Anthropic"], [""], "anthropic", []])
def test_malformed_prefixes_refuse_in_the_browser(tmp_path, bad):
    (tmp_path / "pricing.json").write_text(
        json.dumps(_doc(prefixes=bad)), encoding="utf-8")
    _copy_browser(tmp_path)
    error = _node_raw(f"""
      global.window = {{}};
      let error = null;
      try {{ require({str(tmp_path / 'pricing-loader.js')!r}); }}
      catch (e) {{ error = e.message; }}
      console.log(JSON.stringify(error));
    """)
    assert error and "openrouter.vendor.prefixes" in error, error


def test_a_tracked_entry_without_id_refuses():
    doc = _doc()
    doc["openrouter"]["models"]["acme/idless-9"] = {"vendor_host": HOST}
    with pytest.raises(ValueError, match="acme/idless-9"):
        pricing.load_tables(doc)


def test_an_empty_vendor_host_refuses():
    doc = _doc()
    doc["openrouter"]["models"][KEY]["vendor_host"] = ""
    with pytest.raises(ValueError, match=KEY):
        pricing.load_tables(doc)


def test_two_tracked_entries_matching_one_bare_form_refuse():
    doc = _doc()
    doc["openrouter"]["models"]["glm-acme-9"] = {
        "id": "acme/glm-acme.9", "vendor_host": HOST}
    with pytest.raises(ValueError, match="bare form"):
        pricing.load_tables(doc)


def test_a_bare_form_colliding_with_a_models_key_refuses():
    doc = _doc(models={"other-9": [_entry(R_THIRD)]})
    doc["openrouter"]["models"]["acme/other-9"]["vendor_host"] = HOST
    with pytest.raises(ValueError, match="models-table"):
        pricing.load_tables(doc)


@needs_node
def test_a_bare_form_collision_refuses_in_the_browser(tmp_path):
    doc = _doc(models={"other-9": [_entry(R_THIRD)]})
    doc["openrouter"]["models"]["acme/other-9"]["vendor_host"] = HOST
    (tmp_path / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
    _copy_browser(tmp_path)
    error = _node_raw(f"""
      global.window = {{}};
      let error = null;
      try {{ require({str(tmp_path / 'pricing-loader.js')!r}); }}
      catch (e) {{ error = e.message; }}
      console.log(JSON.stringify(error));
    """)
    assert error and "models-table" in error, error


def test_a_long_context_member_may_name_a_tracked_key():
    doc = _doc()
    doc["long_context_models"] = [KEY]
    assert KEY in pricing.load_tables(doc)["LONG_CONTEXT_MODELS"]


# --- the bare-id vendor path in resolve() ------------------------------------


def test_a_bare_vendor_id_resolves_exact_at_the_windows(monkeypatch):
    _install(monkeypatch, _doc())
    before = pricing.resolve(KEY, CUT - timedelta(seconds=1))
    at = pricing.resolve(KEY, CUT)
    list_ = pricing.resolve(KEY)
    assert (before.kind, before.key) == (at.kind, at.key) == \
        (list_.kind, list_.key) == ("exact", KEY)
    assert before.rates == R_OLD and at.rates == R_NEW and list_.rates == R_NEW
    assert before.scheduled is False


def test_the_suffixed_bare_id_resolves_to_the_same_row(monkeypatch):
    _install(monkeypatch, _doc())
    for model in (f"{KEY}[1m]", f"{KEY}-20250514", f"{KEY}@x"):
        r = pricing.resolve(model)
        assert (r.kind, r.key) == ("exact", KEY), model
        assert r.rates == R_NEW


def test_the_prefixed_tracked_key_serves_its_bare_form(monkeypatch):
    _install(monkeypatch, _doc())
    r = pricing.resolve(BARE)
    assert (r.kind, r.key) == ("exact", PKEY)
    assert r.rates == R_THIRD


def test_a_prefixed_tracked_id_without_provider_uses_the_bare_vendor_row(
        monkeypatch):
    _install(monkeypatch, _doc())
    r = pricing.resolve(PKEY)
    assert (r.kind, r.key, r.rates) == ("exact", PKEY, R_THIRD)
    assert pricing.resolve("acme/untracked-9").kind == "default"


def test_a_prefixed_tracked_id_with_host_uses_the_bare_provider_row(
        monkeypatch):
    _install(monkeypatch, _bare_id_doc())
    r = pricing.resolve(PREFIXED_BARE, provider=ROUTER_HOST)
    assert (r.kind, r.key, r.rates) == ("exact", BARE, R_OLD)


def test_a_prefixed_tracked_id_without_a_host_row_uses_vendor_rates(
        monkeypatch):
    _install(monkeypatch, _bare_id_doc())
    r = pricing.resolve(PREFIXED_BARE, provider="MissingHost")
    assert (r.kind, r.key, r.rates) == ("exact", BARE, R_NEW)


def test_a_vendor_row_before_its_start_falls_through(monkeypatch):
    """Before the row begins the vendor path does not exist: the id prices
    exactly as an unrecognised id of its family does — the opus tier
    fallback here — and nothing vendor-priced before the start."""
    start = CUT - timedelta(days=30)
    _install(monkeypatch, _doc(start=start))
    monkeypatch.setattr(pricing, "_TIER_FALLBACKS", (  # pylint: disable=protected-access
        (re.compile(r"opus"), pricing._latest("opus")),))  # pylint: disable=protected-access
    r = pricing.resolve(KEY, start - timedelta(seconds=1))
    assert (r.kind, r.key, r.scheduled) == ("tier", None, False)
    assert r.rates is pricing._latest("opus")  # pylint: disable=protected-access
    assert pricing.resolve(KEY).rates == R_NEW, "list still answers"


def test_the_bare_path_keeps_search_rate_but_not_host_schedule(monkeypatch):
    """Search is a provider-row rate for the tracked vendor model. The
    bare path uses its dated rate and ignores only the host's schedule."""
    _install(monkeypatch, _doc(web_search=True, schedules=True))
    bare = pricing.resolve(KEY, CUT - timedelta(seconds=1))
    via_host = pricing.resolve(KEY, CUT - timedelta(seconds=1), HOST)
    assert bare.scheduled is False
    assert bare.rates["web_search"] == SEARCH_RATE
    assert {key: bare.rates[key] for key in RATE_FIELDS} == R_OLD
    assert via_host.rates["web_search"] == SEARCH_RATE
    assert via_host.scheduled is True, "the host's schedule applies"


def test_a_family_fallback_is_fed_from_the_vendor_keys(monkeypatch):
    """The claude families now live only in the tracked table, so the tier
    fallback scans it: the fallback rates are the highest-versioned vendor
    key's list price."""
    _install(monkeypatch, _doc())
    monkeypatch.setattr(pricing, "_TIER_FALLBACKS", (  # pylint: disable=protected-access
        (re.compile(r"opus"), pricing._latest("opus")),))  # pylint: disable=protected-access
    r = pricing.resolve("claude-opus-99")
    assert r.kind == "tier"
    assert r.rates is pricing._latest("opus")  # pylint: disable=protected-access
    assert r.rates is pricing.PROVIDER_RATES[(KEY, HOST)]


def test_default_rates_read_the_tracked_claude_row():
    """The default estimate is the merged view's claude-opus-4-7 row — a
    tracked vendor row since the migration — and the row's first entry
    still carries the rates the pre-migration models row held (frozen:
    an append-only history never rewrites it)."""
    assert pricing.DEFAULT_RATES is pricing._list_rates("claude-opus-4-7")  # pylint: disable=protected-access
    frozen = {"fresh": 5.0, "create_5m": 6.25, "create_1h": 10.0,
              "read": 0.5, "output": 25.0}
    assert pricing.rate_for("claude-opus-4-7", datetime(2020, 1, 1, tzinfo=UTC)) == frozen  # sv-test-data: allow (closed-window pin before the row's first cutover)


@needs_node
def test_the_browser_resolves_the_bare_path_identically(tmp_path):
    doc = _doc(web_search=True, schedules=True)
    (tmp_path / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
    _copy_browser(tmp_path)
    before = (CUT - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    got = _node_raw(f"""
      global.window = {{}};
      require({str(tmp_path / 'pricing-loader.js')!r});
      require({str(tmp_path / 'rates.js')!r});
      const K = {{fresh: 'fresh', c5: 'create_5m', c1h: 'create_1h',
                 read: 'read', out: 'output'}};
      const map = (r) => Object.fromEntries(
        Object.entries(K).map(([a, b]) => [b, r.rates[a]]));
      console.log(JSON.stringify({{
        before: window.resolveModelRate({json.dumps(KEY)}, {json.dumps(before)}, null),
        listRes: window.resolveModelRate({json.dumps(KEY)}, null, null),
        suffix: window.resolveModelRate({json.dumps(KEY + '[1m]')}, null, null),
        atSuffix: window.resolveModelRate({json.dumps(KEY + '@x')}, null, null),
        snapshot: window.resolveModelRate({json.dumps(KEY + '-20250514')}, null, null),
        prefixedSpell: window.resolveModelRate({json.dumps(PKEY)}, null, null),
        barePrefixed: window.resolveModelRate({json.dumps(BARE)}, null, null),
        hostSpelled: window.resolveModelRate({json.dumps(KEY)}, {json.dumps(before)}, {json.dumps(HOST)}),
        hostSaturday: window.resolveModelRate({json.dumps(KEY)},
          {json.dumps("2026-10-31T12:00:00Z")}, {json.dumps(HOST)}),
        bareSaturday: window.resolveModelRate({json.dumps(KEY)},
          {json.dumps("2026-10-31T12:00:00Z")}, null),
      }}));
    """)
    assert _js_rates(got["before"]["rates"]) == R_OLD
    assert (got["before"]["kind"], got["before"]["key"]) == ("exact", KEY)
    assert got["before"]["rates"]["search"] == SEARCH_RATE
    assert _js_rates(got["listRes"]["rates"]) == R_NEW
    assert (got["suffix"]["kind"], got["suffix"]["key"]) == ("exact", KEY)
    assert (got["atSuffix"]["kind"], got["atSuffix"]["key"]) == ("exact", KEY)
    assert (got["snapshot"]["kind"], got["snapshot"]["key"]) == ("exact", KEY)
    assert (got["prefixedSpell"]["kind"], got["prefixedSpell"]["key"]) == \
        ("exact", PKEY)
    assert _js_rates(got["prefixedSpell"]["rates"]) == R_THIRD
    assert (got["barePrefixed"]["kind"], got["barePrefixed"]["key"]) == \
        ("exact", PKEY)
    got_host = got["hostSpelled"]
    assert got_host["rates"]["search"] == SEARCH_RATE
    # 2026-10-31 is a Saturday: the host's schedule window answers through
    # the host and never on the bare path.
    assert _js_rates(got["hostSaturday"]["rates"]) == R_THIRD
    assert got["hostSaturday"]["rates"]["search"] == SEARCH_RATE
    assert _js_rates(got["bareSaturday"]["rates"]) == R_OLD
    assert got["bareSaturday"]["rates"]["search"] == SEARCH_RATE


@needs_node
def test_the_browser_prefixed_id_uses_the_bare_provider_and_vendor_rows(
        tmp_path):
    (tmp_path / "pricing.json").write_text(
        json.dumps(_bare_id_doc()), encoding="utf-8")
    _copy_browser(tmp_path)
    got = _node_raw(f"""
      global.window = {{}};
      require({str(tmp_path / 'pricing-loader.js')!r});
      require({str(tmp_path / 'rates.js')!r});
      const fold = {json.dumps(PREFIXED_BARE)};
      console.log(JSON.stringify({{
        host: window.resolveModelRate(fold, null, {json.dumps(ROUTER_HOST)}),
        vendor: window.resolveModelRate(fold, null, 'MissingHost'),
        untracked: window.resolveModelRate('acme/untracked-9', null, null),
      }}));
    """)
    assert (got["host"]["kind"], got["host"]["key"]) == ("exact", BARE)
    assert _js_rates(got["host"]["rates"]) == R_OLD
    assert (got["vendor"]["kind"], got["vendor"]["key"]) == \
        ("exact", BARE)
    assert _js_rates(got["vendor"]["rates"]) == R_NEW
    assert got["untracked"]["kind"] == "default"


# --- rate_boundaries: the vendor branch ---------------------------------------


def test_vendor_boundaries_for_a_bare_pair(monkeypatch):
    _install(monkeypatch, _doc())
    from backend.rate_boundaries import rate_boundaries  # pylint: disable=import-outside-toplevel
    assert rate_boundaries(KEY, None) == [CUT]
    assert rate_boundaries(KEY, "") == [CUT], "an empty provider is bare"


def test_vendor_boundaries_for_a_prefixed_bare_form(monkeypatch):
    _install(monkeypatch, _bare_id_doc())
    from backend.rate_boundaries import rate_boundaries  # pylint: disable=import-outside-toplevel
    assert rate_boundaries(PREFIXED_BARE, None) == [CUT]


def test_vendor_boundaries_for_a_prefixed_provider_pair(monkeypatch):
    _install(monkeypatch, _bare_id_doc())
    from backend.rate_boundaries import rate_boundaries  # pylint: disable=import-outside-toplevel
    prefixed = rate_boundaries(PREFIXED_BARE, DATED_ROUTER_HOST)
    assert prefixed == [CUT]
    assert prefixed == rate_boundaries(BARE, DATED_ROUTER_HOST)


def test_vendor_boundaries_honor_a_start(monkeypatch):
    _install(monkeypatch, _doc(start=CUT - timedelta(days=30)))
    from backend.rate_boundaries import rate_boundaries  # pylint: disable=import-outside-toplevel
    assert rate_boundaries(KEY, None) == [CUT - timedelta(days=30), CUT]


def test_vendor_boundaries_absent_without_a_row(monkeypatch):
    """A tracked vendor entry with no providers row prices nothing: no
    boundaries either (the resolve path falls through the same way)."""
    doc = _doc()
    del doc["providers"][KEY]
    _install(monkeypatch, doc)
    from backend.rate_boundaries import rate_boundaries  # pylint: disable=import-outside-toplevel
    assert rate_boundaries(KEY, None) == []


def test_the_pre_start_fold_uses_the_vendor_windows(monkeypatch):
    """A provider row that begins at a time: before it, a record prices by
    the model alone — which for a vendor key is the vendor row's windows,
    so the fold's boundaries carry them."""
    vendor_start = CUT - timedelta(days=30)
    doc = _doc(start=vendor_start)
    provider_start = datetime(2027, 1, 1, tzinfo=UTC)
    doc["providers"][KEY]["OtherHost"] = [
        {"from": provider_start.isoformat().replace("+00:00", "Z"),
         **{f: R_THIRD[f] for f in RATE_FIELDS}}]
    _install(monkeypatch, doc)
    from backend.rate_boundaries import rate_boundaries  # pylint: disable=import-outside-toplevel
    assert rate_boundaries(KEY, "OtherHost") == [
        vendor_start, CUT, provider_start]


# --- the fingerprint covers the vendor tables ---------------------------------


def test_a_vendor_table_edit_moves_the_fingerprint(monkeypatch):
    from backend import rate_fingerprint  # pylint: disable=import-outside-toplevel
    _install(monkeypatch, _doc())
    rate_fingerprint.clear_fingerprint_cache()
    before = rate_fingerprint.pair_fingerprint(KEY, None)
    doc = _doc()
    doc["providers"][KEY][HOST][0]["fresh"] = 9.5
    _install(monkeypatch, doc)
    rate_fingerprint.clear_fingerprint_cache()
    after = rate_fingerprint.pair_fingerprint(KEY, None)
    assert after != before


def test_a_bare_table_edit_moves_the_fingerprint(monkeypatch):
    """Removing the vendor entry from the tracked table un-prices its bare
    id, and the fingerprint must say so: the pair's bare-path inputs
    changed even though no rate row moved."""
    from backend import rate_fingerprint  # pylint: disable=import-outside-toplevel
    _install(monkeypatch, _doc())
    rate_fingerprint.clear_fingerprint_cache()
    before = rate_fingerprint.pair_fingerprint(KEY, None)
    doc = _doc()
    del doc["openrouter"]["models"][KEY]
    _install(monkeypatch, doc)
    rate_fingerprint.clear_fingerprint_cache()
    after = rate_fingerprint.pair_fingerprint(KEY, None)
    assert after != before


_PIN_INSTANT = datetime(2020, 1, 1, tzinfo=UTC)
_PIN_KEYS = (
    "claude-opus-4-7", "claude-opus-5-5", "claude-sonnet-5-5",
    "claude-haiku-4-5", "gpt-5-5", "gpt-5-6-sol", "glm-5-3-flash",
    "glm-5-3", "kimi-k3", "o3", "claude-3-opus-",
)


def test_the_live_migrated_rows_price_their_frozen_first_entries():
    """Bind the committed file to the migration at a fixed instant before
    every committed cutover: each migrated row's first entry still prices
    what the pre-migration models row held, whatever the refresh appends."""
    doc = json.loads(
        (ROOT / "src" / "pricing.json").read_text(encoding="utf-8"))
    assert len(doc["models"]) == 1 and "bonsai-2-27b" in doc["models"]
    for key in _PIN_KEYS:
        entry = doc["openrouter"]["models"][key]
        host = entry["vendor_host"]
        history = doc["providers"][key][host]
        rates = {f: history[0][f] for f in RATE_FIELDS}
        if "web_search" in history[0]:
            rates["web_search"] = history[0]["web_search"]
        assert pricing.rate_for(key, _PIN_INSTANT) == rates, key
        # The suffix tolerance and the tracked-key identity hold live too.
        assert pricing.resolve(f"{key}[1m]").key == key, key


def test_every_migrated_row_keeps_the_models_shape_rules():
    """Live file: every vendor_host-carrying tracked entry carries its id
    and host. (The provider-table-keys-are-tracked half of this invariant
    is pinned by test_provider_rate_refresh's
    test_every_provider_table_model_names_its_openrouter_id; the pickup
    delay — an entry ahead of its row for one run — is the design,
    SV-VENDOR-RATES.)"""
    doc = json.loads((ROOT / "src" / "pricing.json").read_text(encoding="utf-8"))
    tracked = doc["openrouter"]["models"]
    for key, entry in tracked.items():
        if "vendor_host" not in entry:
            continue
        assert entry["id"], key
        assert isinstance(entry["vendor_host"], str) and entry["vendor_host"], key


# --- the vendor pass on a tracked key (no re-add; the fold still runs) ---------


def test_the_vendor_pass_never_re_adds_a_tracked_key():
    """The tracked table's business: the pass selects a tracked key's
    first-party listing — the band below folds the meter membership, so the
    listing was read — but re-adds or rewrites nothing, and the models
    table is never written."""
    doc = _doc()
    before = json.loads(json.dumps(doc))
    catalog = {"data": [{"id": "acme/claude-opus.9", "architecture": {
        "output_modalities": ["text"]}}]}
    payload = {"data": {"endpoints": [
        {"provider_name": HOST, "tag": "acme", "quantization": "fp8",
         "status": 0, "context_length": 131072,
         "pricing": {"prompt": "0.000003", "completion": "0.000015",
                     "overrides": [{"min_prompt_tokens":
                                    long_context.LONG_CONTEXT_THRESHOLD,
                                    "prompt": "0.000006",
                                    "completion": "0.0000225"}]}}]}}
    outcome = _load_vendor().vendor_pass(
        doc, lambda: catalog, lambda _mid: payload)
    assert outcome.refusals == []
    assert outcome.moves, "the tracked key's listing was not selected"
    assert doc["long_context_models"] == [KEY]
    assert doc["models"] == before["models"], "the models table was written"
    assert (doc["openrouter"]["models"][KEY]
            == before["openrouter"]["models"][KEY]), "the entry was rewritten"


# --- the family fallback and default on the REAL file -------------------------


@pytest.mark.parametrize("probe,family", [
    ("claude-haiku-99", "haiku"), ("claude-fable-99", "fable"),
    ("claude-mythos-99", "fable"), ("claude-opus-99", "opus"),
    ("claude-sonnet-99", "sonnet")])
def test_real_family_fallbacks_feed_from_the_tracked_table(probe, family):
    """The claude families live in the tracked table now: an unrecognised
    member's tier fallback rates are that family's tracked list price."""
    r = pricing.resolve(probe)
    assert r.kind == "tier", probe
    assert r.rates is pricing._latest(  # pylint: disable=protected-access
        *(("fable", "mythos") if family == "fable" else (family,)))


# --- browser lockstep on the fallback tables ----------------------------------


@needs_node
def test_the_browser_falls_back_from_the_tracked_tables_too(tmp_path):
    (tmp_path / "pricing.json").write_text(json.dumps(_doc()), encoding="utf-8")
    _copy_browser(tmp_path)
    got = _node_raw(f"""
      global.window = {{}};
      require({str(tmp_path / 'pricing-loader.js')!r});
      require({str(tmp_path / 'rates.js')!r});
      console.log(JSON.stringify({{
        tier: window.resolveModelRate('claude-opus-99', null, null),
        def: window.resolveModelRate('zz-unknown-9', null, null),
      }}));
    """)
    assert got["tier"]["kind"] == "tier"
    assert _js_rates(got["tier"]["rates"]) == R_NEW
    assert _js_rates(got["def"]["rates"]) == R_THIRD, "default = the models row"
