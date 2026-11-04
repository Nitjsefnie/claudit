"""Sweeps serve the page's webfonts from one fetch and recover CDN resets."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

from backend.js_min import minify_js
from tests.node_run import run_node

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "ci" / "cdn_route.mjs"

# Supply a legacy page at its filesystem boundary to exercise CDN recovery.
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


def _node(body: str, *, cdn_page: bool = True) -> dict:
    script = (
        "import assert from 'node:assert/strict';\n"
        "import testChildProcess from 'node:child_process';\n"
        "import {syncBuiltinESMExports as syncTestExports} from 'node:module';\n"
        "const nativeExecFile = testChildProcess.execFile;\n"
        "testChildProcess.execFile = (command, ...args) => nativeExecFile(\n"
        f"  command === 'python3' ? {json.dumps(sys.executable)} : command, ...args);\n"
        "syncTestExports();\n"
        f"{CDN_PAGE_SETUP if cdn_page else ''}\n"
        f"const mod = await import({json.dumps(MODULE.as_uri())});\n"
        f"{body}\n"
    )
    proc = run_node(
        ["node", "--input-type=module"], input=script, capture_output=True,
        text=True, timeout=60, check=False, encoding="utf-8", errors="strict",
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


# The real page's webfonts behind a double of Google Fonts: the stylesheet
# names woff2 faces only for a browser user agent, as the live service does.
FONTS = r"""
  const {readFileSync} = await import('node:fs');
  const html = readFileSync('public/index.html', 'utf8');
  assert.ok(!html.includes('https://unpkg.com/'));
  const css2 = html.match(/href="(https:\/\/fonts\.googleapis\.com\/css2[^"]+)"/)[1];
  const preloads = [...html.matchAll(/href="(https:\/\/fonts\.gstatic\.com\/s\/[^"]+)"/g)]
    .map(match => match[1]);
  const extra = 'https://fonts.gstatic.com/s/fixture/v1/latin-ext.woff2';
  const faces = [...preloads, extra];
  const sheet = list => list.map(url => `@font-face{src:url(${url}) format('woff2')}`).join('\n');
  // Omits one preload, which then reaches the cache only through index.html.
  const woff2Css = sheet([preloads[1], extra, preloads[1]]);
  const ttfCss = sheet(['https://fonts.gstatic.com/s/fixture/v1/face.ttf']);
  const calls = new Map(), agents = [], waits = [], unmodelled = [];
  const bytes = url => Buffer.from(`face ${url} é`);
  const serve = (url, init) => {
    if (url === css2) {
      agents.push(init.headers?.['user-agent']);
      const css = /Chrome\/\d/.test(init.headers?.['user-agent'] ?? '') ? woff2Css : ttfCss;
      return new Response(css, {headers: {'content-type': 'text/css; charset=utf-8'}});
    }
    if (faces.includes(url)) return new Response(bytes(url), {headers: {'content-type': 'font/woff2'}});
    unmodelled.push(url);
    return new Response('', {status: 404, statusText: 'Not Modelled'});
  };
  let created = 0;
  const browser = {async newContext(options) {
    created++;
    const routes = [];
    return {options, routes, async route(matcher, handler) {
      if (typeof matcher !== 'string' && typeof matcher !== 'function') assert.fail('unmodelled matcher');
      routes.push({matcher, handler});
    },
    // Playwright hands a request to the last registered route that matches it.
    async request(url) {
      const hit = routes.findLast(({matcher}) => (typeof matcher === 'function'
        ? matcher(new URL(url)) : matcher === url));
      if (!hit) return {network: true};
      let result;
      await hit.handler({request: () => ({url: () => url}),
        fulfill: async value => { result = {fulfilled: value}; },
        abort: async () => { result = {aborted: true}; },
        continue: async () => assert.fail('a font route never falls through to the network')});
      return result;
    }};
  }};
  const deps = {fetchImpl: (...args) => fetchImpl(...args), wait: async ms => { waits.push(ms); }};
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_webfonts_are_fetched_once_and_served_identically_to_every_context():
    """Every context renders from one run-wide copy of the page's woff2 faces,
    so no context can measure the fallback font another context did not."""
    out = _node(FONTS + """
      async function fetchImpl(url, init) {
        const n = (calls.get(url) || 0) + 1;
        calls.set(url, n);
        if (n === 1) throw new TypeError('fetch failed', {cause: new Error('ECONNRESET')});
        return serve(url, init);
      }
      const options = {viewport: {width: 320, height: 900}};
      const contexts = await Promise.all([mod.createSweepContext(browser, options, deps),
        mod.createSweepContext(browser, options, deps)]);
      const served = [];
      for (const context of contexts) {
        assert.deepEqual(context.options, options);
        assert.equal(context.routes.length, 1, 'the webfonts are the only route');
        const sheet = (await context.request(css2)).fulfilled;
        assert.equal(sheet.body.toString('utf8'), woff2Css);
        assert.equal(sheet.headers['content-type'], 'text/css; charset=utf-8');
        for (const url of faces) {
          const face = (await context.request(url)).fulfilled;
          assert.deepEqual(face.body, bytes(url));
          assert.equal(face.status, 200);
          assert.equal(face.headers['content-type'], 'font/woff2');
          assert.equal(face.headers['access-control-allow-origin'], '*');
        }
        served.push({
          unknown: await context.request('https://fonts.gstatic.com/s/rotated/v9/face.woff2'),
          page: await context.request('http://127.0.0.1:8000/'),
          api: await context.request('http://127.0.0.1:8000/api/web-metrics'),
        });
      }
      console.log(JSON.stringify({calls: Object.fromEntries(calls), faces, css2, agents,
        waits, unmodelled, served, created, families: mod.fontFamilies}));
    """, cdn_page=False)
    assert out["created"] == 2
    assert out["calls"] == {url: 2 for url in [out["css2"], *out["faces"]]}
    assert len(out["waits"]) == len(out["calls"])
    assert out["agents"] and all("Chrome/" in agent for agent in out["agents"])
    assert out["unmodelled"] == []
    assert out["served"] == [{"unknown": {"aborted": True}, "page": {"network": True},
                              "api": {"network": True}}] * 2
    assert out["families"] == ["Inter", "JetBrains Mono"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("target", ["stylesheet", "face"])
def test_an_exhausted_webfont_fetch_rejects_setup_naming_its_url(target: str):
    out = _node(f"const target = {json.dumps(target)};\n" + FONTS + """
      async function fetchImpl(url, init) {
        const n = (calls.get(url) || 0) + 1;
        calls.set(url, n);
        if (url === (target === 'stylesheet' ? css2 : extra)) {
          throw new TypeError('fetch failed', {cause: new Error(`ECONNRESET attempt ${n}`)});
        }
        return serve(url, init);
      }
      let message;
      try { await mod.createSweepContext(browser, {}, deps); }
      catch (error) { message = error.message; }
      assert.ok(message, 'exhausted font setup must reject');
      await assert.rejects(() => mod.createSweepContext(browser, {}, deps),
        error => error.message === message);
      console.log(JSON.stringify({message, url: target === 'stylesheet' ? css2 : extra,
        created, unmodelled, failing: calls.get(target === 'stylesheet' ? css2 : extra)}));
    """, cdn_page=False)
    assert out["url"] in out["message"]
    assert "ECONNRESET attempt 3" in out["message"]
    assert out["failing"] == 3
    assert out["created"] == 0
    assert out["unmodelled"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_picker_context_deadline_never_pays_for_the_font_prefetch():
    """The prefetch is bounded by its own per-fetch timeouts; the fixture
    context's 30 s deadline starts only once the faces are in the cache."""
    out = _node(FONTS + """
      const {default: fsp} = await import('node:fs/promises');
      const {syncBuiltinESMExports} = await import('node:module');
      // The fixture read is the case's only threadpool I/O; reading it synchronously
      // arms the context deadline at a known point instead of a host-timed one.
      fsp.readFile = async (path, ...args) => readFileSync(path, ...args);
      syncBuiltinESMExports();
      const cases = await import('./scripts/ci/picker_cases.mjs');
      let release, now = 0, serial = 0, opened = 0, fetches = 0;
      const gate = new Promise(resolve => { release = resolve; });
      globalThis.fetch = async (url, init) => { fetches++; await gate; return serve(url, init); };
      const timers = new Map();
      globalThis.setTimeout = (fn, ms) => { timers.set(++serial, {fn, at: now + ms}); return serial; };
      globalThis.clearTimeout = id => timers.delete(id);
      const turn = () => new Promise(resolve => setImmediate(resolve));
      const until = async (label, done) => {
        for (const stop = Date.now() + 20_000; !done(); await turn()) {
          if (Date.now() > stop) throw new Error(`gave up waiting for ${label}`);
        }
      };
      const advance = async ms => {
        await turn();
        now += ms;
        for (const [id, timer] of [...timers]) if (timer.at <= now && timers.delete(id)) timer.fn();
        await turn();
      };
      const result = {};
      cases.runPickerCases({newContext: async () => { opened++; return new Promise(() => {}); }},
        'http://test', [], {}).catch(error => Object.assign(result, {error: error.message}));
      await until('the first font fetch', () => fetches > 0);
      await advance(31_000);
      const stalled = {...result, opened};
      release();
      await until('the fixture context or its failure', () => opened || 'error' in result);
      const fetched = {...result, opened};
      await advance(31_000);
      await until('the fixture context deadline', () => 'error' in result);
      console.log(JSON.stringify({stalled, fetched, final: result, unmodelled}));
    """, cdn_page=False)
    assert out["stalled"] == {"opened": 0}
    assert out["fetched"] == {"opened": 1}
    assert out["final"]["error"] == "timed out waiting for picker fixture context"
    assert out["unmodelled"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_sweep_server_serves_backend_bytes_and_cleans_up():
    expected = {}
    for path in sorted((ROOT / "src").rglob("*")):
        if path.suffix not in {".js", ".jsx", ".json"}:
            continue
        body = path.read_bytes()
        if path.suffix != ".json":
            body = minify_js(body.decode("utf-8")).encode("utf-8")
        expected["/" + path.relative_to(ROOT).as_posix()] = hashlib.sha256(body).hexdigest()
    for path in sorted((ROOT / "public" / "vendor").glob("*.js")):
        expected["/vendor/" + path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    out = _node(f"const expected = {json.dumps(expected)};\n" + """
      const fs = await import('node:fs/promises');
      const {default: childProcess} = await import('node:child_process');
      const {syncBuiltinESMExports} = await import('node:module');
      const {createHash} = await import('node:crypto');
      const {tmpdir} = await import('node:os');
      const {join, sep} = await import('node:path');
      let children = 0, output;
      const execOriginal = childProcess.execFile;
      childProcess.execFile = (...args) => {
        children++;
        output = args[1].at(-1);
        return execOriginal(...args);
      };
      syncBuiltinESMExports();
      const {serve, REPO} = await import('./scripts/ci/panel_server.mjs');
      process.chdir(tmpdir());
      const server = await serve();
      try {
        assert.equal(children, 1, 'transform all scripts in one child at startup');
        assert.ok(!output.startsWith(REPO + sep), 'transformed copies must be outside the repo');
        assert.ok(output.startsWith(join(tmpdir(), 'claudit-')));
        assert.ok((await fs.stat(output)).isDirectory());
        const base = `http://127.0.0.1:${server.address().port}`;
        for (const [path, digest] of Object.entries(expected)) {
          const response = await fetch(base + path + '?v=fixture');
          assert.equal(response.status, 200, path);
          const type = path.endsWith('.jsx') ? 'text/babel'
            : path.endsWith('.json') ? 'application/json' : 'text/javascript';
          assert.equal(response.headers.get('content-type'), type + '; charset=utf-8', path);
          const bytes = Buffer.from(await response.arrayBuffer());
          assert.equal(createHash('sha256').update(bytes).digest('hex'), digest, path);
        }
        assert.equal(children, 1, 'requests must reuse the startup transform');
        assert.equal((await fetch(base + '/absent.js')).status, 404);
      } finally { await new Promise(resolve => server.close(resolve)); }
      await assert.rejects(() => fs.stat(output), {code: 'ENOENT'});
      console.log(JSON.stringify({served: Object.keys(expected).length}));
    """, cdn_page=False)
    assert out["served"] == len(expected)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_sweep_server_cleans_up_on_immediate_exit():
    out = _node("""
      const {default: childProcess} = await import('node:child_process');
      const {syncBuiltinESMExports} = await import('node:module');
      let output;
      const execOriginal = childProcess.execFile;
      childProcess.execFile = (...args) => {
        output = args[1].at(-1);
        return execOriginal(...args);
      };
      syncBuiltinESMExports();
      const {serve} = await import('./scripts/ci/panel_server.mjs');
      const server = await serve();
      console.log(JSON.stringify({output}));
      server.close();
      process.exit(0);
    """, cdn_page=False)
    output = Path(out["output"])
    assert not output.is_relative_to(ROOT)
    assert not output.exists()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_sweep_server_cleans_up_on_startup_exit():
    out = _node("""
      const {default: childProcess} = await import('node:child_process');
      const {syncBuiltinESMExports} = await import('node:module');
      childProcess.execFile = (...args) => {
        console.log(JSON.stringify({output: args[1].at(-1)}));
        process.exit(0);
      };
      syncBuiltinESMExports();
      const {serve} = await import('./scripts/ci/panel_server.mjs');
      await serve();
    """, cdn_page=False)
    output = Path(out["output"])
    assert not output.is_relative_to(ROOT)
    assert not output.exists()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.skipif(os.name != "posix", reason="requires POSIX ps and real SIGTERM delivery")
@pytest.mark.parametrize("phase", ["startup", "ready"])
def test_sweep_server_cleans_up_on_sigterm(tmp_path: Path, phase: str):
    script = tmp_path / "sweep-server.mjs"
    script.write_text(
        "const phase = " + json.dumps(phase) + ";\n"
        + "const python = " + json.dumps(sys.executable) + ";\n" + """
      import childProcess from 'node:child_process';
      import {syncBuiltinESMExports} from 'node:module';
      const execOriginal = childProcess.execFile;
      childProcess.execFile = (...args) => {
        if (phase === 'startup') args[1][1] = 'import time; time.sleep(30)\\n' + args[1][1];
        const child = execOriginal(python, ...args.slice(1));
        console.log(JSON.stringify({phase: 'startup', output: args[1].at(-1), child: child.pid}));
        return child;
      };
      syncBuiltinESMExports();
    """ + f"const {{serve}} = await import({json.dumps((ROOT / 'scripts/ci/panel_server.mjs').as_uri())});\n"
        + "await serve(); console.log(JSON.stringify({phase: 'ready'}));\n",
        encoding="utf-8",
    )
    out = _node(f"const script = {json.dumps(str(script))}, phase = {json.dumps(phase)};\n" + """
      const {spawn, execFileSync} = await import('node:child_process');
      const {once} = await import('node:events');
      const {createInterface} = await import('node:readline');
      const {stat} = await import('node:fs/promises');
      const child = spawn(process.execPath, [script]);
      const closed = once(child, 'close');
      let output, transform, errors = '';
      child.stderr.on('data', data => { errors += data; });
      try {
        for await (const line of createInterface({input: child.stdout})) {
          const event = JSON.parse(line);
          if (event.output) { output = event.output; transform = event.child; }
          if (event.phase === phase) break;
        }
        assert.ok(output, errors);
        const command = execFileSync('ps', ['-ww', '-p', String(child.pid), '-o', 'args='],
          {encoding: 'utf8'});
        assert.ok(command.includes(script), command);
        child.kill('SIGTERM');
        const [code, signal] = await closed;
        assert.equal(code, 143, errors);
        assert.equal(signal, null);
        assert.throws(() => process.kill(child.pid, 0), {code: 'ESRCH'});
        assert.throws(() => process.kill(transform, 0), {code: 'ESRCH'});
        transform = null;
        await assert.rejects(() => stat(output), {code: 'ENOENT'});
        console.log(JSON.stringify({terminated: true, output}));
      } finally {
        if (transform) {
          try { process.kill(transform, 'SIGKILL'); }
          catch (error) { if (error.code !== 'ESRCH') throw error; }
        }
        if (child.exitCode === null && child.signalCode === null) {
          child.kill('SIGKILL');
          await closed;
        }
      }
    """, cdn_page=False)
    assert out["terminated"] is True
    assert not Path(out["output"]).exists()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_sweep_server_rejects_failed_transform():
    out = _node("""
      const {default: childProcess} = await import('node:child_process');
      const {syncBuiltinESMExports} = await import('node:module');
      const {stat} = await import('node:fs/promises');
      const execOriginal = childProcess.execFile;
      childProcess.execFile = (_command, args, options, callback) => execOriginal(
        process.execPath, ['-e', "process.stdout.write(process.argv[1]);"
          + "process.stderr.write('minify fixture failed\\\\n'); process.exit(42);", args.at(-1)],
        options, callback);
      syncBuiltinESMExports();
      let output;
      const {serve} = await import('./scripts/ci/panel_server.mjs');
      await assert.rejects(() => serve().then(server => { server.close(); }), error => {
        output = error.stdout;
        return error.code === 42 && error.stderr.includes('minify fixture failed');
      });
      assert.equal(typeof output, 'string');
      assert.ok(output.length > 0, 'the failing child must name its output directory');
      await assert.rejects(() => stat(output), {code: 'ENOENT'});
      console.log(JSON.stringify({rejected: true}));
    """, cdn_page=False)
    assert out["rejected"] is True


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
