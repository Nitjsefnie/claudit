"""Vendored classic scripts carry byte-matched SRI and no comments or maps."""
from __future__ import annotations

import base64
import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from backend.js_min import comment_spans

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "public" / "vendor"
SCRIPTS = ("react.production.min.js", "react-dom.production.min.js", "babel.min.js")


class _ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.scripts.append(dict(attrs))


def _vendor_tags() -> list[dict[str, str | None]]:
    collector = _ScriptCollector()
    collector.feed((ROOT / "public" / "index.html").read_text(encoding="utf-8"))
    return [attrs for attrs in collector.scripts
            if (attrs.get("src") or "").startswith("/vendor/")]


@pytest.mark.parametrize("name", SCRIPTS)
def test_vendor_script_has_no_comments_or_source_map(name: str) -> None:
    path = VENDOR / name
    assert path.is_file()
    source = path.read_text(encoding="utf-8")
    assert source.strip()
    assert not comment_spans(source)
    assert not re.search(r"(?m)^\s*//[#@]\s*sourceMappingURL=.*\s*\Z", source)


def test_vendor_tags_pin_every_served_script() -> None:
    tags = _vendor_tags()
    assert {attrs["src"] for attrs in tags} == {f"/vendor/{name}" for name in SCRIPTS}
    for attrs in tags:
        assert attrs.get("integrity"), attrs
        assert attrs.get("crossorigin") == "anonymous"
        raw = (ROOT / "public" / str(attrs["src"]).lstrip("/")).read_bytes()
        digest = base64.b64encode(hashlib.sha384(raw).digest()).decode("ascii")
        assert attrs["integrity"] == "sha384-" + digest


def test_vendor_licenses_keep_upstream_notices() -> None:
    path = VENDOR / "LICENSES.txt"
    assert path.is_file()
    notices = path.read_text(encoding="utf-8")
    assert "Copyright (c) Facebook, Inc. and its affiliates." in notices
    assert "This source code is licensed under the MIT license" in notices
    assert "regenerator-runtime -- Copyright (c) 2014-present, Facebook, Inc." in notices
    assert "license (MIT): https://github.com/babel/babel/blob/main/packages/babel-helpers/LICENSE" in notices
