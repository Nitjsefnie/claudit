"""Every application classic script in public/index.html runs in one shared realm.

A <script src> without type="text/babel" is a classic script: the browser
instantiates its top level into the page's one shared global scope. A
top-level const/let/class there is a one-way door: the next classic
script that declares the same name - or declares it over the
non-configurable global property another file's top-level function
declaration created - dies with a SyntaxError before its first statement
(parser-codex.js's top-level ``const {... } = window``
against parser-lanes.js's same-named function declarations silenced the
entire Codex lane parser; window.parseLaneCodex was never defined).

Node's require()-based tests cannot see this - each required file gets
its own module scope - so this drives the REAL scripts through node's
vm in index.html tag order: vm.runInContext gives each script the
browser's classic-script instantiation over one shared context, the same
semantics the page has, so the same collisions throw here as there.
The tag list is parsed from index.html at test time, at load order, so a
newly added classic script is guarded by construction. /vendor/ and CDN
scripts are skipped: this checks our globals, not third-party browser runtimes.

The seams' typeof alone cannot catch a miswired seam: a rename in
parser-lanes.js leaves every seam a function bound to undefined helpers,
and typeof cannot see inside a closure. So the guard also runs the real
Codex fixture fixtures/parser/codex_min.jsonl and both Kimi wires'
fixtures (kimi_code_min.jsonl, kimi_legacy_min.jsonl) through
window.parseTranscriptLanes after the load and requires usage records
out of each - the seams must not merely exist, they must work.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import pytest

from backend.js_min import minify_js
from tests.node_run import run_node

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "public" / "index.html"
BOOT_LOADER = ROOT / "src" / "babel-loader.js"
CODEX_PROBE_FIXTURE = ROOT / "fixtures" / "parser" / "codex_min.jsonl"
KIMI_CODE_PROBE_FIXTURE = ROOT / "fixtures" / "parser" / "kimi_code_min.jsonl"
KIMI_LEGACY_PROBE_FIXTURE = (
    ROOT / "fixtures" / "parser" / "kimi_legacy_min.jsonl"
)

# The seams later scripts and the panels consume; all must survive the
# full classic-script load.
_SEAMS = (
    "decodePricingDoc",
    "parseLaneCodex",
    "parseLaneKimiCode",
    "parseLaneLegacy",
    "parseTranscriptLanes",
    "sniffTranscriptFormat",
    "parseTranscript",
)

# Reads the (name, source) list and the named probe fixtures as one JSON
# object on stdin, runs every script in order over ONE shared vm context
# (classic-script semantics), and reports the typeof of each seam after
# the full load plus each probe's assistant_usage count. The first
# failing script is reported on stderr and exits nonzero.
_RUNNER = """
(async () => {
const vm = require('vm');
const { scripts, probes, babelScripts } = JSON.parse(require('fs').readFileSync(0, 'utf8'));
// pricing-loader.js takes its node path in the sandbox (no document
// here): it reads the rate document beside __dirname, so hand the
// context the require/__dirname pair its own docstring names as the
// test mode.
let boot = null;
const ctx = vm.createContext({
  require: (name) => require(name),
  __dirname: process.env.CLAUDIT_SRC_DIR,
  Babel: {disableScriptTags() {}},
  addEventListener(type, fn) { if (type === 'DOMContentLoaded') boot = fn; },
});
vm.runInContext('var window = globalThis;', ctx, { filename: '<prelude>' });
for (const [name, code] of scripts) {
  try {
    vm.runInContext(code, ctx, { filename: name });
  } catch (e) {
    console.error(name + ': ' + e);
    process.exit(1);
  }
}
// The text/babel scripts run through the real src/babel-loader.js, so they
// compile under the loader's own options; only the browser around it is stubbed.
if (babelScripts.length) {
  const Babel = require(process.env.CLAUDIT_BABEL_FILE);
  const sources = Object.fromEntries(babelScripts);
  const tasks = [];
  ctx.Babel.transform = (code, options) => Babel.transform(code, options);
  ctx.pricingReady = Promise.resolve();
  ctx.fetch = async (url) => ({status: 200, text: async () => sources[url]});
  ctx.MessageChannel = class {
    constructor() {
      this.port1 = {};
      this.port2 = {postMessage: () => tasks.push(() => this.port1.onmessage({}))};
    }
  };
  ctx.React = {createElement() {}, useState() {}, useEffect() {}, useMemo() {},
    useRef() {}, useLayoutEffect() {}, useId() {}};
  ctx.ReactDOM = {createRoot() { return {render() {}}; }};
  ctx.getComputedStyle = () => ({getPropertyValue: () => ''});
  ctx.document = {
    documentElement: {},
    getElementById() { return {}; },
    querySelectorAll: () => babelScripts.map(([name, code]) => (
      name === 'Inline Babel script' ? {src: '', innerHTML: code} : {src: name, innerHTML: ''})),
    createElement: () => ({}),
    head: {appendChild(script) { vm.runInContext(script.text, ctx); }},
  };
  boot();
  const settle = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
  await settle();
  while (tasks.length) {
    try {
      tasks.shift()();
    } catch (e) {
      console.error(String(e));
      process.exit(1);
    }
    await settle();
  }
}
const seams = %SEAMS%;
const seam = (n) => vm.runInContext(`typeof window.${n}`, ctx);
ctx.__probes = probes;
const probeOut = vm.runInContext(`
  (() => Object.fromEntries(Object.entries(window.__probes).map(
    ([name, text]) => {
      const r = window.parseTranscriptLanes(text);
      return [name,
              r.meta.filter((m) => m.type === 'assistant_usage').length];
    })))()
`, ctx);
console.log(JSON.stringify({
  ran: scripts.length,
  seams: Object.fromEntries(seams.map((n) => [n, seam(n)])),
  probe: probeOut,
}));
})();
"""


class _ScriptTagCollector(HTMLParser):
    """Collect <script> tags in document order as (attrs, body) pairs.

    html.parser lowercases tag and attribute names and reads a script
    body as CDATA, so tags match case-insensitively by construction and
    the body arrives verbatim - a regexp over HTML has neither property
    (CodeQL: a filtering regexp that misses upper-case <SCRIPT> tags).
    """

    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[tuple[list[tuple[str, str | None]], str]] = []
        self._open: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._open = len(self.scripts)
            self.scripts.append((attrs, ""))

    def handle_data(self, data: str) -> None:
        if self._open is not None:
            idx = self._open
            self.scripts[idx] = (self.scripts[idx][0], self.scripts[idx][1] + data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._open = None


def _classic_scripts() -> list[tuple[str, str]]:
    """Return our classic scripts as (name, source) pairs in tag order.

    Skip Babel-compiled tags and vendor/CDN browser runtimes; test our globals.
    """
    collector = _ScriptTagCollector()
    collector.feed(INDEX_HTML.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for attrs, body in collector.scripts:
        types = [v for k, v in attrs if k == "type" and v is not None]
        if "text/babel" in types:
            continue
        srcs = [v for k, v in attrs if k == "src" and v is not None]
        if srcs:
            url = srcs[0]
            if not url.startswith("/") or url.startswith("/vendor/"):
                continue
            path = ROOT / url.lstrip("/")
            out.append((url, path.read_bytes().decode("utf-8")))
        elif body.strip():
            out.append(("<inline>", body))
    return out


def _realm_summary(scripts: list[tuple[str, str]],
                   babel_scripts: list[tuple[str, str]] | None = None) -> dict:
    """Load one ordered script set and probe its globals and parser seams."""
    runner = _RUNNER.replace("%SEAMS%", repr(list(_SEAMS)))
    probes = {
        "codex_min.jsonl": CODEX_PROBE_FIXTURE,
        "kimi_code_min.jsonl": KIMI_CODE_PROBE_FIXTURE,
        "kimi_legacy_min.jsonl": KIMI_LEGACY_PROBE_FIXTURE,
    }
    proc = run_node(
        ["node", "-e", runner],
        input=json.dumps({
            "scripts": scripts,
            "babelScripts": babel_scripts or [],
            "probes": {name: path.read_text(encoding="utf-8")
                       for name, path in probes.items()},
        }).encode(),
        capture_output=True,
        timeout=60,
        env={**os.environ, "CLAUDIT_SRC_DIR": str(ROOT / "src"),
             "CLAUDIT_BABEL_FILE": str(ROOT / "public" / "vendor" / "babel.min.js")},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    return json.loads(proc.stdout.decode())


def _babel_scripts() -> list[tuple[str, str]]:
    collector = _ScriptTagCollector()
    collector.feed(INDEX_HTML.read_text(encoding="utf-8"))
    out = []
    for attrs, body in collector.scripts:
        tag = dict(attrs)
        if tag.get("type") != "text/babel":
            continue
        url = tag.get("src")
        out.append((url, (ROOT / url.lstrip("/")).read_text(encoding="utf-8"))
                   if url else ("Inline Babel script", body))
    return out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_classic_scripts_load_in_one_realm() -> None:
    """Sources and served copies expose the same working globals in one realm."""
    scripts = _classic_scripts()
    assert scripts, "index.html lost its classic scripts; guard ran on nothing"
    summary = _realm_summary(scripts)
    # Keep transformed coverage URLs outside the repository's src/**/*.js set.
    with tempfile.TemporaryDirectory(prefix="claudit-classic-") as directory:
        assert not Path(directory).resolve().is_relative_to(ROOT)
        minified = []
        changed = False
        for name, source in scripts:
            if name == "<inline>":
                minified.append((name, source))
                continue
            path = Path(directory) / name.lstrip("/")
            path.parent.mkdir(parents=True, exist_ok=True)
            transformed = minify_js(source)
            changed |= transformed != source
            path.write_bytes(transformed.encode("utf-8"))
            minified.append((str(path), path.read_bytes().decode("utf-8")))
        assert changed, "minified realm must exercise transformed source bytes"
        assert _realm_summary(minified) == summary
    for name in _SEAMS:
        assert summary["seams"][name] == "function", (
            f"window.{name} is {summary['seams'][name]!r} after the full "
            "classic-script load"
        )
    assert summary["probe"]["codex_min.jsonl"] >= 1, (
        "the codex probe fixture yielded no assistant_usage through "
        "window.parseTranscriptLanes after the full load - a seam is "
        "miswired (e.g. a renamed helper left a seam a function bound "
        "to undefined)"
    )
    for name in ("kimi_code_min.jsonl", "kimi_legacy_min.jsonl"):
        assert summary["probe"][name] >= 1, (
            f"the {name} probe fixture yielded no assistant_usage through "
            "window.parseTranscriptLanes after the full classic-script "
            "load - the parser-kimi.js seam is miswired (e.g. its tag "
            "dropped or reordered in index.html, or its helpers bound to "
            "undefined)"
        )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_babel_scripts_share_the_classic_realm() -> None:
    scripts = _classic_scripts()
    babel_scripts = _babel_scripts()
    assert babel_scripts and babel_scripts[-1][0] == "Inline Babel script"
    _realm_summary(scripts, babel_scripts)
    first, *rest = babel_scripts
    duplicate = first[0], first[1] + "\nconst __claudit_scope_probe = 1;\n"
    second = rest[0][0], rest[0][1] + "\nconst __claudit_scope_probe = 2;\n"
    with pytest.raises(AssertionError, match="__claudit_scope_probe"):
        _realm_summary(scripts, [duplicate, second, *rest[1:]])
    with pytest.raises(AssertionError, match="__claudit_scope_probe"):
        _realm_summary([*scripts, ("probe.js", "const __claudit_scope_probe = 1;")],
                       [duplicate, *rest])


# Learns the names pricing-loader.js writes from a node-path load, then loads
# every classic script again on the browser path with the pricing fetch held
# open: each such name is an accessor that records a read until the loader
# assigns it, so a script reading a table at its own evaluation is named.
_EARLY_READ_RUNNER = r"""
const vm = require('vm');
const { scripts } = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const scenario = process.argv[1];
const LOADER = '/src/pricing-loader.js';
const base = () => ({require, __dirname: process.env.CLAUDIT_SRC_DIR,
  Babel: {disableScriptTags() {}}, addEventListener() {}});
function load(sandbox, around) {
  const ctx = vm.createContext(sandbox);
  vm.runInContext('var window = globalThis;', ctx);
  for (const [name, code] of scripts) {
    if (name === LOADER) around(true);
    vm.runInContext(code, ctx, {filename: name});
    if (name === LOADER) around(false);
  }
  return ctx;
}
const nodePath = base();
let before;
load(nodePath, (start) => { before = start ? new Set(Object.keys(nodePath)) : before; });
const names = Object.keys(nodePath).filter((name) => !before.has(name));
let boot;
const browser = {...base(), MessageChannel, URL,
  addEventListener(type, fn) { if (type === 'DOMContentLoaded') boot = fn; }};
const reads = new Set();
for (const name of names) {
  Object.defineProperty(browser, name, {configurable: true, enumerable: true,
    get() { reads.add(name); return undefined; },
    set(value) { Object.defineProperty(browser, name, {value, writable: true, enumerable: true, configurable: true}); }});
}
let respond;
browser.fetch = (url) => new Promise((resolve) => {
  respond = () => resolve(scenario === 'refused' ? {status: 404, url, text: async () => ''}
    : {status: 200, url, text: async () => require('fs').readFileSync(
      require('path').join(process.env.CLAUDIT_SRC_DIR, new URL(url).pathname.split('/').pop()), 'utf8')});
});
const ctx = load(browser, (start) => {
  if (start) browser.document = {currentScript: {src: 'https://claudit.test/src/pricing-loader.js', dataset: {}}};
  else delete browser.document;
});
const early = [...reads];
// The real src/babel-loader.js boots over one app script standing in for
// every text/babel script; it records whether the tables existed when it ran.
const root = {};
browser.Babel.transform = (code) => ({code});
browser.document = {
  querySelectorAll: () => [{src: '', innerHTML: 'window.appSaw = window.longContextModels !== undefined;'}],
  createElement: () => ({}),
  getElementById: (id) => (id === 'root' ? root : null),
  head: {appendChild(script) { vm.runInContext(script.text, ctx); }},
};
const unhandled = [];
process.on('unhandledRejection', (e) => unhandled.push(String(e)));
const turns = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
(async () => {
  boot();
  await turns();
  const heldRun = 'appSaw' in browser;
  respond();
  await Promise.resolve(browser.pricingReady).catch(() => {});
  await turns();
  console.log(JSON.stringify({names, early, heldRun, appSaw: browser.appSaw ?? null,
    rootText: root.textContent ?? null, unhandled,
    installed: names.filter((name) => Object.getOwnPropertyDescriptor(browser, name).get === undefined),
    tier: scenario === 'refused' ? null
      : vm.runInContext("window.resolveModelRate('claude-opus-99-unlisted', null, null).kind", ctx)}));
  process.exit(0);  // the loader's message port would keep node alive
})();
"""


_BOOT_RUNNER = r"""
const fs = require('fs');
const vm = require('vm');
const scenario = process.argv[1];
const path = process.argv[2];
const urls = ['https://dashboard.test/a.jsx?v=1', 'https://dashboard.test/b.jsx?v=2'];
const tags = [
  {src: urls[0], nonce: 'nonce-a', innerHTML: ''},
  {src: urls[1], nonce: 'nonce-b', innerHTML: ''},
  {src: '', nonce: 'nonce-inline',
    innerHTML: "events.push((function () { return this === undefined; })() ? 'inline-strict' : 'inline-sloppy')"},
];
const requests = {}, tasks = [], errors = [], calls = [], events = [];
let ready, held, disabled = false, tick = 0, kind = null;
const context = vm.createContext({
  events,
  Babel: {
    disableScriptTags() { disabled = true; },
    transform(source, options) {
      calls.push({source, options, tick, kind});
      if (source === 'bad syntax') throw new SyntaxError('compile failed');
      return {code: source};
    },
  },
  fetch(url, options) {
    events.push('fetch:' + url);
    return new Promise((resolve, reject) => { requests[url] = {resolve, reject, options}; });
  },
  setTimeout(fn) { tasks.push(['timer', fn]); },
  MessageChannel: class {
    constructor() {
      this.port1 = {};
      this.port2 = {postMessage: () => tasks.push(['message', () => this.port1.onmessage({})])};
    }
  },
});
context.window = context;
let settlePricing;
if (!scenario.startsWith('pricing-')) context.pricingReady = Promise.resolve();
else if (scenario !== 'pricing-absent') {
  context.pricingReady = new Promise((resolve, reject) => { settlePricing = {resolve, reject}; });
}
const root = {};
process.on('unhandledRejection', (e) => errors.push('unhandled: ' + String(e)));
context.addEventListener = (type, fn) => { if (type === 'DOMContentLoaded') ready = fn; };
context.document = {
  querySelectorAll(selector) {
    if (selector !== 'script[type="text/babel"]') throw Error('unexpected selector: ' + selector);
    return tags;
  },
  createElement(type) {
    if (type !== 'script') throw Error('unexpected element: ' + type);
    return {};
  },
  getElementById(id) {
    if (id !== 'root') throw Error('unexpected id: ' + id);
    return root;
  },
  head: {appendChild(script) {
    events.push('nonce:' + script.nonce);
    try { vm.runInContext(script.text, context); } catch (e) { errors.push(String(e)); }
  }},
};
const settle = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
async function drain() {
  let count = 0;
  while (tasks.length && count++ < 30) {
    tick++;
    const [taskKind, fn] = tasks.shift();
    kind = taskKind;
    try { fn(); } catch (e) { errors.push(String(e)); }
    await settle();
  }
  if (tasks.length) throw Error('task queue failed to drain');
}
function respond(url, status, text) {
  requests[url].resolve({status, text: () => Promise.resolve(text)});
}
(async () => {
  vm.runInContext(fs.existsSync(path) ? fs.readFileSync(path, 'utf8') : '', context);
  const beforeReady = {disabled, requests: Object.keys(requests).length};
  if (!ready) {
    console.log(JSON.stringify({disabled, ready: false}));
    return;
  }
  ready();
  await settle();
  const started = Object.keys(requests);
  if (started.length !== urls.length) {
    console.log(JSON.stringify({disabled, ready: true, beforeReady, started}));
    return;
  }
  if (scenario === 'success' || scenario === 'status-zero') {
    respond(urls[1], scenario === 'status-zero' ? 0 : 200, "events.push('b')");
    await settle();
    await drain();
    respond(urls[0], 200, "events.push('a')");
  } else if (scenario === 'fetch-error') {
    respond(urls[1], 200, "events.push('b')");
    await settle();
    respond(urls[0], 404, 'missing');
  } else if (scenario === 'network-error' || scenario === 'read-error') {
    respond(urls[1], 200, "events.push('b')");
    await settle();
    if (scenario === 'network-error') requests[urls[0]].reject(new TypeError('network failure'));
    else requests[urls[0]].resolve({status: 200, text: () => Promise.reject(new TypeError('read failure'))});
  } else if (scenario === 'compile-error') {
    respond(urls[0], 200, 'bad syntax');
    await settle();
    await drain();
    respond(urls[1], 200, "events.push('b')");
  } else if (scenario.startsWith('pricing-')) {
    respond(urls[0], 200, "events.push('a')");
    respond(urls[1], 200, "events.push('b')");
    await settle();
    await drain();
    held = events.slice(2);
    if (scenario === 'pricing-resolved') settlePricing.resolve();
    else if (scenario === 'pricing-rejected') settlePricing.reject(new Error('refused: HTTP 404'));
  } else if (scenario === 'compile-error-all-loaded') {
    respond(urls[1], 200, "events.push('b')");
    await settle();
    respond(urls[0], 200, 'bad syntax');
  }
  await settle();
  await drain();
  await new Promise((resolve) => setImmediate(resolve));
  console.log(JSON.stringify({disabled, ready: true, beforeReady, started, held, events, errors, calls,
    rootText: root.textContent ?? null,
    fetchOptions: urls.map(url => requests[url].options)}));
})().catch(e => { console.error(e.stack); process.exitCode = 1; });
"""


def _boot_probe(scenario: str) -> dict:
    proc = run_node(
        ["node", "-e", _BOOT_RUNNER, scenario, str(BOOT_LOADER)],
        capture_output=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    return json.loads(proc.stdout.decode())


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("scenario", ["success", "status-zero"])
def test_babel_loader_runs_in_order_in_separate_tasks(scenario: str) -> None:
    out = _boot_probe(scenario)
    assert out["disabled"] and out["ready"]
    assert out["beforeReady"] == {"disabled": True, "requests": 0}
    assert out["started"] == [
        "https://dashboard.test/a.jsx?v=1", "https://dashboard.test/b.jsx?v=2"]
    assert out["events"][2:] == [
        "nonce:nonce-a", "a", "nonce:nonce-b", "b", "nonce:nonce-inline", "inline-strict"]
    assert out["errors"] == []
    assert out["fetchOptions"] == [{"mode": "same-origin"}] * 2
    assert len({call["tick"] for call in out["calls"]}) == 3
    assert {call["kind"] for call in out["calls"]} == {"message"}
    assert [call["options"] for call in out["calls"]] == [
        {"presets": ["react"], "plugins": [], "filename": filename,
         "sourceFileName": filename, "sourceMaps": "inline"}
        for filename in (*out["started"], "Inline Babel script")]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_babel_loader_reports_failed_fetch_and_continues() -> None:
    out = _boot_probe("fetch-error")
    assert out["disabled"] and out["ready"]
    assert out["events"][2:] == ["nonce:nonce-b", "b", "nonce:nonce-inline", "inline-strict"]
    assert out["errors"] == ["Error: Could not load https://dashboard.test/a.jsx?v=1"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("scenario", ["network-error", "read-error"])
def test_babel_loader_uses_empty_source_for_network_failure(scenario: str) -> None:
    out = _boot_probe(scenario)
    assert out["disabled"] and out["ready"]
    assert out["events"][2:] == [
        "nonce:nonce-a", "nonce:nonce-b", "b", "nonce:nonce-inline", "inline-strict"]
    assert out["calls"][0]["source"] == ""
    assert out["errors"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_babel_loader_resumes_after_compile_error_when_another_fetch_finishes() -> None:
    out = _boot_probe("compile-error")
    assert out["disabled"] and out["ready"]
    assert out["errors"] == ["SyntaxError: compile failed"]
    assert out["events"][2:] == ["nonce:nonce-b", "b", "nonce:nonce-inline", "inline-strict"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_babel_loader_stops_after_compile_error_without_another_fetch() -> None:
    out = _boot_probe("compile-error-all-loaded")
    assert out["disabled"] and out["ready"]
    assert out["errors"] == ["SyntaxError: compile failed"]
    assert out["events"][2:] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_babel_scripts_wait_for_the_pricing_tables() -> None:
    out = _boot_probe("pricing-resolved")
    assert out["held"] == []
    assert out["events"][2:] == [
        "nonce:nonce-a", "a", "nonce:nonce-b", "b", "nonce:nonce-inline", "inline-strict"]
    assert out["errors"] == [] and out["rootText"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("scenario, message", [
    ("pricing-rejected", "refused: HTTP 404"),
    ("pricing-absent", "pricing.json: the rate loader never ran"),
])
def test_babel_scripts_never_run_over_refused_pricing(scenario: str, message: str) -> None:
    out = _boot_probe(scenario)
    assert out["held"] == [] and out["events"][2:] == []
    assert out["rootText"] == message
    assert out["errors"] == ["unhandled: Error: " + message]


def _early_read_probe(scenario: str) -> dict:
    proc = run_node(
        ["node", "-e", _EARLY_READ_RUNNER, scenario],
        input=json.dumps({"scripts": _classic_scripts()}).encode(),
        capture_output=True, timeout=60,
        env={**os.environ, "CLAUDIT_SRC_DIR": str(ROOT / "src")}, check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    return json.loads(proc.stdout.decode())


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_no_classic_script_reads_a_pricing_table_before_the_fetch_installs_it() -> None:
    out = _early_read_probe("installed")
    assert {"modelRates", "providerRates", "longContextMeters"} <= set(out["names"])
    assert out["early"] == []
    assert out["installed"] == out["names"]
    assert out["tier"] == "tier"
    assert not out["heldRun"] and out["appSaw"] is True
    assert out["rootText"] is None and out["unhandled"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_refused_pricing_fetch_shows_its_error_and_runs_no_app_script() -> None:
    out = _early_read_probe("refused")
    message = "pricing.json: HTTP 404 from https://claudit.test/src/pricing.json"
    assert not out["heldRun"] and out["appSaw"] is None
    assert out["rootText"] == message
    assert out["unhandled"] == ["Error: " + message]


def test_babel_loader_tag_follows_babel() -> None:
    collector = _ScriptTagCollector()
    collector.feed(INDEX_HTML.read_text(encoding="utf-8"))
    sources = [dict(attrs).get("src") for attrs, _ in collector.scripts]
    assert "/src/babel-loader.js" in sources
    assert sources.index("/src/babel-loader.js") == sources.index("/vendor/babel.min.js") + 1
