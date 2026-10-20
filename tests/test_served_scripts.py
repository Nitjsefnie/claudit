"""The authenticated source mount serves comment-free modules and original data."""
from __future__ import annotations

import os
import re
import subprocess
from email.utils import formatdate
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend import source_files
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
