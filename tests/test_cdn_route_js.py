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
  const bytes = Buffer.from('window.loaded = true; /* π */\n');
  const calls = new Map(), waits = [], signals = [];
  const response = () => new Response(bytes, {headers: {
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
          loaded.push(fulfilled);
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
        return response();
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
        return response();
      }
      await mod.createSweepContext(browser, {}, deps);
      console.log(JSON.stringify({calls: [...calls.values()]}));
    """)
    assert out["calls"]
    assert all(count == 2 for count in out["calls"])
