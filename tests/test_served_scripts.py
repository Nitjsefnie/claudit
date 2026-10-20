"""The authenticated source mount serves comment-free modules and original data."""
from __future__ import annotations

import os
import html
import json
import re
import shutil
import subprocess
import threading
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.staticfiles import StaticFiles

from backend.app import app
from backend import source_asset_identity, source_files
from backend.js_min import JsMinError, comment_spans
from tests.test_security_headers import _guest_client_fixture  # pylint: disable=unused-import


_ROOT = Path(__file__).resolve().parents[1]
_MODULES = subprocess.check_output(
    ["git", "ls-files", "src/*.js", "src/*.jsx", "src/views/*.jsx"],
    cwd=_ROOT, text=True,
).splitlines()
_TERMINATORS = re.compile(r"\r\n|[\r\n\u2028\u2029]")


@pytest.mark.parametrize("module", _MODULES)
def test_served_module(guest_client, module):
    source = (_ROOT / module).read_bytes()
    response = guest_client.get("/" + module, headers={"Accept-Encoding": "identity"})
    assert response.status_code == 200
    body = response.content.decode("utf-8")
    assert not comment_spans(body)
    assert not re.search(r"sourceMappingURL[^\r\n\u2028\u2029]*\s*\Z", body)
    assert _TERMINATORS.findall(body) == _TERMINATORS.findall(source.decode("utf-8"))
    assert len(response.content) <= len(source)
    expected_type = "text/javascript" if module.endswith(".js") else "application/octet-stream"
    assert response.headers["content-type"].split(";")[0] == expected_type
    assert int(response.headers["content-length"]) == len(response.content)
    assert response.headers["x-content-type-options"] == "nosniff"
    repeated = guest_client.get("/" + module, headers={"Accept-Encoding": "identity"})
    assert repeated.content == response.content
    assert repeated.headers["etag"] == response.headers["etag"]


@pytest.mark.parametrize("name", ["pricing.json", "pricing_notices.json"])
def test_source_json_unchanged(guest_client, name):
    response = guest_client.get(f"/src/{name}")
    assert response.status_code == 200
    assert response.content == (_ROOT / "src" / name).read_bytes()
    assert response.headers["content-type"] == "application/json"


def test_source_requires_session():
    response = TestClient(app).get("/src/parser.js", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


@pytest.fixture(name="source_tree")
def _source_tree_fixture(monkeypatch, tmp_path):
    mount, = (route for route in app.routes if getattr(route, "name", None) == "src")
    monkeypatch.setattr(mount, "app", source_files.MinifiedStaticFiles(directory=tmp_path))
    source_files._minified.cache_clear()  # pylint: disable=protected-access
    yield tmp_path
    source_files._minified.cache_clear()  # pylint: disable=protected-access


def test_source_cache_reuses_transform_and_keeps_path_identity(guest_client, source_tree, monkeypatch):
    calls = []
    original = source_files.minify_js

    def counted(source):
        calls.append(source)
        return original(source)

    monkeypatch.setattr(source_files, "minify_js", counted)
    first = source_tree / "first.js"
    second = source_tree / "second.js"
    first.write_bytes(b"// first\nconst x = 1;\n")
    second.write_bytes(b"// other\nconst x = 2;\n")
    os.utime(second, ns=(first.stat().st_atime_ns, first.stat().st_mtime_ns))
    response = guest_client.get("/src/first.js")
    assert response.content == b"\nconst x = 1;\n"
    assert guest_client.get("/src/first.js?v=other").content == response.content
    assert guest_client.get("/src/second.js").content == b"\nconst x = 2;\n"
    assert len(calls) == 2


@pytest.mark.parametrize("changed", ["mtime", "size"])
def test_source_edit_invalidates_cache(guest_client, source_tree, changed):
    source = source_tree / "edit.jsx"
    source.write_bytes(b"// before\nconst x = <div />;\n")
    stat = source.stat()
    before = guest_client.get("/src/edit.jsx")
    replacement = b"// after!\nconst x = <p   />;\n" if changed == "mtime" else b"const x = <span />;\n"
    if changed == "mtime":
        assert len(replacement) == stat.st_size
    source.write_bytes(replacement)
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + (1_000_000_000 if changed == "mtime" else 0)))
    after = guest_client.get("/src/edit.jsx", headers={"If-None-Match": before.headers["etag"]})
    assert after.status_code == 200
    assert after.content != before.content
    assert after.headers["etag"] != before.headers["etag"]
    assert not comment_spans(after.text)
    assert after.headers["last-modified"] == formatdate(source.stat().st_mtime, usegmt=True)


def test_source_validators_and_head(guest_client, source_tree):
    source = source_tree / "headers.js"
    source.write_bytes(b"/* remove me */\r\nconst x = 1;\r\n")
    response = guest_client.get("/src/headers.js")
    assert int(response.headers["content-length"]) == len(response.content)
    assert len(response.content) < source.stat().st_size
    assert response.headers["last-modified"] == formatdate(source.stat().st_mtime, usegmt=True)
    for header, validator in [("If-None-Match", "etag"), ("If-Modified-Since", "last-modified")]:
        unchanged = guest_client.get("/src/headers.js", headers={header: response.headers[validator]})
        assert unchanged.status_code == 304
        assert unchanged.content == b""
    head = guest_client.head("/src/headers.js")
    assert head.status_code == 200
    assert head.content == b""
    for header in ["content-length", "content-type", "last-modified", "etag"]:
        assert head.headers[header] == response.headers[header]


def test_source_mixed_line_terminators(guest_client, source_tree):
    source = "// comment\r\nconst x = 1;\r/* block\u2028comment */\nconst y = 2;\u2029//# sourceMappingURL=gone.map\r\n"
    (source_tree / "lines.js").write_bytes(source.encode("utf-8"))
    response = guest_client.get("/src/lines.js")
    body = response.content.decode("utf-8")
    assert response.status_code == 200
    assert not comment_spans(body)
    assert "sourceMappingURL" not in body
    assert _TERMINATORS.findall(body) == ["\r\n", "\r", "\u2028", "\n", "\u2029", "\r\n"]


@pytest.mark.parametrize("conditional", [False, True])
def test_source_transform_error_is_logged_and_request_fails(guest_client, source_tree, caplog, conditional):
    source = source_tree / "broken.js"
    source.write_bytes(b"/* unterminated")
    headers = {"If-Modified-Since": formatdate(source.stat().st_mtime, usegmt=True)} if conditional else {}
    with pytest.raises(JsMinError, match="Unterminated block comment"):
        guest_client.get("/src/broken.js", headers=headers)
    assert "Cannot minify source module" in caplog.text
    assert "broken.js" in caplog.text


def test_source_mount_keeps_static_path_and_method_checks(guest_client, source_tree, tmp_path):
    (source_tree / "ok.js").write_bytes(b"const x = 1;\n")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.js"
    outside.write_bytes(b"// inaccessible\n")
    (source_tree / "escape.js").symlink_to(outside)
    assert guest_client.get("/src/escape.js").status_code == 404
    assert guest_client.get("/src/missing.js").status_code == 404
    assert guest_client.post("/src/ok.js", headers={"Origin": "http://testserver"}).status_code == 405


def _asset_urls(client, prefix="/src/"):
    return [html.unescape(url) for url in re.findall(
        r'(?:src|data-pricing)="([^\"]+)"', client.get("/").text)
        if url.startswith(prefix)]


@pytest.fixture(name="transform_tree")
def _transform_tree_fixture(monkeypatch, tmp_path):
    for module in (_ROOT / "backend").glob("js_min*.py"):
        (tmp_path / module.name).write_bytes(module.read_bytes())
    monkeypatch.setattr(source_asset_identity, "__file__", str(tmp_path / "source_asset_identity.py"))
    source_asset_identity.source_transform_identity.cache_clear()
    yield tmp_path
    source_asset_identity.source_transform_identity.cache_clear()


@pytest.mark.parametrize("module", ["js_min.py", "js_min_context.py"])
def test_transform_code_edit_moves_module_urls_only(guest_client, transform_tree, module):
    before = _asset_urls(guest_client)
    vendors = _asset_urls(guest_client, "/vendor/")
    assert before == _asset_urls(guest_client)
    source = transform_tree / module
    stat = source.stat()
    source.write_bytes(source.read_bytes() + b"\nTRANSFORM_FIXTURE = 1\n")
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    source_asset_identity.source_transform_identity.cache_clear()
    after = _asset_urls(guest_client)
    assert before and len(before) == len(after)
    for old, new in zip(before, after):
        old_parts, new_parts = urlsplit(old), urlsplit(new)
        assert old_parts.path == new_parts.path
        old_query, new_query = parse_qs(old_parts.query), parse_qs(new_parts.query)
        assert old_query["v"] == new_query["v"] == [str(int((_ROOT / old_parts.path.lstrip("/")).stat().st_mtime))]
        if Path(old_parts.path).suffix in {".js", ".jsx"}:
            assert old_query["transform"] != new_query["transform"]
        else:
            assert old == new
            assert set(new_query) == {"v"}
    assert vendors == _asset_urls(guest_client, "/vendor/")
    assert any(urlsplit(url).path == "/src/pricing.json" for url in after)


def test_new_transform_helper_joins_url_identity(guest_client, transform_tree):
    before = _asset_urls(guest_client)
    (transform_tree / "js_min_helper.py").write_bytes(b"TRANSFORM_FIXTURE = 1\n")
    source_asset_identity.source_transform_identity.cache_clear()
    after = _asset_urls(guest_client)
    assert before and len(before) == len(after)
    for old, new in zip(before, after):
        transformed = Path(urlsplit(old).path).suffix in {".js", ".jsx"}
        assert (old != new) == transformed


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_warm_browser_refetches_source_after_minifying_upgrade(guest_client):
    path = _ROOT / "src" / "hhmm-spelling.js"
    original = path.read_bytes(), path.stat().st_mtime_ns
    old_url = f"/src/hhmm-spelling.js?v={int(path.stat().st_mtime)}"
    new_url, = (url for url in _asset_urls(guest_client) if url.startswith("/src/hhmm-spelling.js?"))
    raw_app = FastAPI()
    raw_app.mount("/src", StaticFiles(directory=_ROOT / "src"))
    raw_client = TestClient(raw_app)
    state = {"upgraded": False, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):  # pylint: disable=invalid-name
            if self.path == "/upgrade":
                state["upgraded"] = True
                body, headers = b"", {}
            elif self.path.startswith("/src/"):
                client = guest_client if state["upgraded"] else raw_client
                response = client.get(self.path, headers={"Accept-Encoding": "identity"})
                assert response.status_code == 200
                state["requests"].append((state["upgraded"], self.path))
                body, headers = response.content, dict(response.headers)
                headers["Cache-Control"] = "public, max-age=3600"
            else:
                url = new_url if state["upgraded"] else old_url
                body = f'<!doctype html><script src="{html.escape(url, quote=True)}"></script>'.encode()
                headers = {"Content-Type": "text/html", "Cache-Control": "no-store"}
            self.send_response(200)
            for key, value in headers.items():
                if key.lower() != "content-length":
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        result = _browser_upgrade(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        raw_client.close()
    assert not thread.is_alive()
    if result.get("skip"):
        pytest.skip(result["skip"])
    assert state["requests"] == [(False, old_url), (True, new_url)]
    assert new_url != old_url
    assert not comment_spans(result["body"])
    assert result["body"].encode() == guest_client.get(new_url).content
    assert original == (path.read_bytes(), path.stat().st_mtime_ns)


def _browser_upgrade(base):
    script = """(async () => {
  let playwrightPath;
  try {
    playwrightPath = require.resolve('playwright');
  } catch (error) {
    if (error.code !== 'MODULE_NOT_FOUND') throw error;
    const {lstatSync} = require('node:fs');
    const {join} = require('node:path');
    for (const directory of require.resolve.paths('playwright')) {
      for (const suffix of ['', '.js', '.json', '.node']) {
        try {
          lstatSync(join(directory, 'playwright' + suffix));
        } catch (statError) {
          if (['ENOENT', 'ENOTDIR'].includes(statError.code)) continue;
          throw statError;
        }
        throw error;
      }
    }
    console.log(JSON.stringify({skip: 'Playwright package is not installed'}));
    return;
  }
  const {chromium} = require(playwrightPath);
  let browser;
  try {
    browser = await chromium.launch({args: ['--no-sandbox', '--claudit-source-cache-upgrade']});
  } catch (error) {
    if (!error.message.includes("Executable doesn't exist")) throw error;
    console.log(JSON.stringify({skip: 'Playwright Chromium is not installed'}));
    return;
  }
  try {
    const page = await browser.newPage();
    await page.goto(BASE + '/?old');
    await page.goto(BASE + '/?still-old');
    await fetch(BASE + '/upgrade');
    const received = page.waitForResponse(response => response.url().includes('/src/'));
    await page.goto(BASE + '/?new');
    const response = await received;
    console.log(JSON.stringify({body: (await response.body()).toString('utf8')}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
""".replace("BASE", json.dumps(base))
    process = subprocess.run(
        ["node"], input=script, cwd=_ROOT, capture_output=True, text=True,
        encoding="utf-8", timeout=60, check=False,
    )
    assert process.returncode == 0, process.stderr or process.stdout
    return json.loads(process.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_missing_playwright_package_returns_a_skip(monkeypatch, tmp_path):
    monkeypatch.setitem(globals(), "_ROOT", tmp_path)
    assert _browser_upgrade("http://unused") == {"skip": "Playwright package is not installed"}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("metadata, body, message", [
    ('{"main":"missing-entry.js"}', None, "missing-entry.js"),
    ('{"name":"playwright","version":"1.0.0"}', None, "Cannot find module 'playwright'"),
    (None, "throw new SyntaxError('broken import');", "broken import"),
    (None, "require('fixture-missing-dependency');", "fixture-missing-dependency"),
    (None, "exports.chromium = {launch: async () => { throw new Error('broken launch'); }};", "broken launch"),
    (None, "exports.chromium = {launch: async () => ({newPage: async () => { throw new Error('broken runtime'); }, close: async () => {}})};", "broken runtime"),
])
def test_browser_dependency_defects_fail(monkeypatch, tmp_path, metadata, body, message):
    package = tmp_path / "node_modules" / "playwright"
    package.mkdir(parents=True)
    if metadata is not None:
        (package / "package.json").write_text(metadata, encoding="utf-8")
    if body is not None:
        (package / "index.js").write_text(body, encoding="utf-8")
    monkeypatch.setitem(globals(), "_ROOT", tmp_path)
    with pytest.raises(AssertionError, match=message):
        _browser_upgrade("http://unused")
