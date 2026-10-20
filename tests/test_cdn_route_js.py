"""The rendered sweeps recover CDN resets without changing the served page."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "ci" / "cdn_route.mjs"

# The current helper requires a page with unpkg URLs. Supply that legacy
# input at its filesystem boundary; the real page now uses local vendors.
CDN_PAGE_SETUP = """
  const {default: fs} = await import('node:fs');
  const {syncBuiltinESMExports} = await import('node:module');
  const readOriginal = fs.readFileSync;
  const fixtureHtml = '<script src="https://unpkg.com/fixture-a@1/script.js"></script>'
    + '<script src="https://unpkg.com/fixture-b@1/script.js"></script>';
  fs.readFileSync = (path, ...args) => {
    if (path === 'public/index.html'
      || (path instanceof URL && path.href === %INDEX_URL%)) return fixtureHtml;
    return readOriginal(path, ...args);
  };
  syncBuiltinESMExports();
""".replace("%INDEX_URL%", json.dumps((ROOT / "public" / "index.html").as_uri()))


def test_every_sweep_creates_contexts_through_cdn_helper():
    """Catch new page/context entry points that bypass CDN recovery."""
    bypasses = []
    for path in sorted((ROOT / "scripts" / "ci").glob("*.mjs")):
        if path == MODULE:
            continue
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\.\s*(newContext|newPage|launchPersistentContext)\s*\(",
                                 source):
            if match[1] == "newPage":
                if re.search(r"\b(?:ctx|context)\s*$", source[:match.start()]):
                    continue
            bypasses.append(f"{path.name}:{match[1]}")
    assert not bypasses, f"CDN recovery bypassed: {bypasses}"


def _node(body: str) -> dict:
    script = (
        "import assert from 'node:assert/strict';\n"
        f"{CDN_PAGE_SETUP}\n"
        f"const mod = await import({json.dumps(MODULE.as_uri())});\n"
        f"{body}\n"
    )
    proc = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True,
        text=True, timeout=60, check=False, encoding="utf-8", errors="strict",
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


SETUP = r"""
  const {readFileSync} = await import('node:fs');
  const html = readFileSync('public/index.html', 'utf8');
  const urls = [...html.matchAll(/<script[^>]*src="(https:\/\/unpkg.com\/[^\"]+)"/g)]
    .map(match => match[1]);
  const bytesFor = new Map(urls.map(url => [url,
    Buffer.from(`window.loaded = ${JSON.stringify(url)}; /* π */\n`)]));
  const calls = new Map(), waits = [], signals = [];
  const response = url => new Response(bytesFor.get(url), {headers: {
    'content-type': 'text/javascript', 'content-encoding': 'gzip',
    'content-length': '7', 'access-control-allow-origin': '*',
  }});
  let created = 0;
  const browser = {async newContext(options) {
    created++;
    const routes = new Map();
    return {options, async route(url, handler) { routes.set(url, handler); },
      async newPage() { return {async goto() {
        const loaded = [];
        for (const url of urls) {
          let fulfilled;
          if (routes.has(url)) await routes.get(url)({
            fulfill: async value => { fulfilled = value; },
          });
          else {
            const upstream = await fetchImpl(url, {});
            fulfilled = {status: upstream.status, body: Buffer.from(await upstream.arrayBuffer())};
          }
          loaded.push({...fulfilled, url});
        }
        return loaded;
      }};},
    };
  }};
  const deps = {fetchImpl: (...args) => fetchImpl(...args),
    wait: async ms => { waits.push(ms); }};
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("failure", ["ECONNRESET", "ECONNREFUSED", "TimeoutError"])
def test_reset_recovers_page_setup_and_memoizes_exact_bytes(failure: str):
    out = _node(f"const failure = {json.dumps(failure)};\n" + SETUP + """
      async function fetchImpl(url, {signal}) {
        signals.push(signal);
        const n = (calls.get(url) || 0) + 1;
        calls.set(url, n);
        if (n === 1) throw new TypeError('fetch failed', {
          cause: new Error(failure),
        });
        return response(url);
      }
      const options = {viewport: {width: 320, height: 900}};
      const contexts = await Promise.all([mod.createSweepContext(browser, options, deps),
        mod.createSweepContext(browser, options, deps)]);
      for (const context of contexts) {
        assert.deepEqual(context.options, options);
        const page = await context.newPage();
        const loaded = await page.goto('http://fixture/');
        assert.equal(loaded.length, urls.length);
        for (const item of loaded) {
          const bytes = bytesFor.get(item.url);
          assert.deepEqual(item.body, bytes);
          assert.equal(item.status, 200);
          assert.equal(item.headers['access-control-allow-origin'], '*');
          assert.equal(item.headers['content-type'], 'text/javascript');
          assert.equal(item.headers['content-encoding'], undefined);
          assert.equal(item.headers['content-length'], undefined);
        }
      }
      assert.ok(signals.every(signal => signal instanceof AbortSignal));
      console.log(JSON.stringify({calls: [...calls.values()], waits, count: urls.length}));
    """)
    assert out["count"] > 0
    assert out["calls"] == [2] * out["count"]
    assert len(out["waits"]) == out["count"]
    assert all(0 < delay <= 1000 for delay in out["waits"])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_exhausted_reset_bound_names_url_and_last_error():
    out = _node(SETUP + """
      async function fetchImpl(url) {
        const n = (calls.get(url) || 0) + 1;
        calls.set(url, n);
        throw new TypeError('fetch failed', {cause: new Error(`ECONNRESET attempt ${n}`)});
      }
      let message;
      try { await mod.createSweepContext(browser, {}, deps); }
      catch (error) { message = error.message; }
      assert.ok(message, 'exhausted setup must reject');
      assert.ok(message.includes(urls[0]), message);
      assert.match(message, /ECONNRESET attempt 3/);
      await assert.rejects(() => mod.createSweepContext(browser, {}, deps),
        error => error.message === message);
      assert.equal(created, 0, 'fail before allocating a browser context');
      console.log(JSON.stringify({calls: [...calls.values()], waits}));
    """)
    assert all(count == 3 for count in out["calls"])
    assert all(0 < delay <= 1000 for delay in out["waits"])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_http_error_fails_without_retry():
    out = _node(SETUP + """
      async function fetchImpl(url) {
        calls.set(url, (calls.get(url) || 0) + 1);
        return new Response('missing', {status: 404, statusText: 'Not Found'});
      }
      await assert.rejects(() => mod.createSweepContext(browser, {}, deps), error =>
        error.message.includes(urls[0]) && /HTTP 404.*Not Found/.test(error.message));
      console.log(JSON.stringify({calls: [...calls.values()], waits}));
    """)
    assert all(count == 1 for count in out["calls"])
    assert out["waits"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_reset_while_reading_body_retries():
    out = _node(SETUP + """
      async function fetchImpl(url) {
        const n = (calls.get(url) || 0) + 1;
        calls.set(url, n);
        if (n === 1) return {ok: true, arrayBuffer: async () => {
          throw new Error('body ECONNRESET');
        }};
        return response(url);
      }
      await mod.createSweepContext(browser, {}, deps);
      console.log(JSON.stringify({calls: [...calls.values()]}));
    """)
    assert out["calls"]
    assert all(count == 2 for count in out["calls"])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_timeout_signal_aborts_stalled_transport_within_bound():
    out = _node(SETUP + """
      let now = 0, serial = 0, aborted = 0, escaped = 0;
      const timers = new Map();
      const schedule = (ms, run) => {
        const id = ++serial;
        timers.set(id, {at: now + ms, run});
        return id;
      };
      AbortSignal.timeout = ms => {
        assert.ok(Number.isFinite(ms) && ms > 0 && ms < 10_000);
        const controller = new AbortController();
        schedule(ms, () => controller.abort(new DOMException('CDN deadline expired', 'TimeoutError')));
        return controller.signal;
      };
      async function fetchImpl(url, {signal}) {
        calls.set(url, (calls.get(url) || 0) + 1);
        return new Promise((resolve, reject) => {
          const abort = () => {
            timers.delete(escape);
            aborted++;
            reject(signal.reason);
          };
          const escape = schedule(10_000, () => {
            signal.removeEventListener('abort', abort);
            escaped++;
            reject(new Error('transport escaped without abort'));
          });
          signal.addEventListener('abort', abort, {once: true});
          if (signal.aborted) abort();
        });
      }
      let outcome;
      const setup = mod.createSweepContext(browser, {}, deps).then(
        () => { outcome = {ok: true}; }, error => { outcome = {error}; });
      const flush = async () => {
        for (let i = 0; i < 100; i++) await Promise.resolve();
      };
      await flush();
      for (let step = 0; !outcome && step < 20 && timers.size; step++) {
        now = Math.min(...[...timers.values()].map(timer => timer.at));
        for (const [id, timer] of [...timers]) {
          if (timer.at <= now && timers.delete(id)) timer.run();
        }
        await flush();
      }
      assert.ok(outcome?.error, 'setup must reject within the finite clock budget');
      assert.equal(outcome.error.cause?.name, 'TimeoutError', outcome.error.message);
      assert.ok(outcome.error.message.includes(urls[0]), outcome.error.message);
      assert.match(outcome.error.message, /CDN deadline expired/);
      assert.equal(created, 0);
      await setup;
      console.log(JSON.stringify({aborted, escaped, now, calls: [...calls.values()]}));
    """)
    assert out["aborted"] > 0
    assert out["escaped"] == 0
    assert 0 < out["now"] < 30_000
    assert out["calls"]
    assert all(count == 3 for count in out["calls"])
