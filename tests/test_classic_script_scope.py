"""Every classic script public/index.html loads runs in one shared realm.

A <script src> without type="text/babel" is a classic script: the browser
instantiates its top level into the page's one shared global scope. A
top-level const/let/class there is a one-way door: the next classic
script that declares the same name - or declares it over the
non-configurable global property another file's top-level function
declaration created - dies with a SyntaxError before its first statement
(issue #780: parser-codex.js's top-level ``const { ... } = window``
against parser-lanes.js's same-named function declarations silenced the
entire Codex lane parser; window.parseLaneCodex was never defined).

Node's require()-based tests cannot see this - each required file gets
its own module scope - so this drives the REAL scripts through node's
vm in index.html tag order: vm.runInContext gives each script the
browser's classic-script instantiation over one shared context, the same
semantics the page has, so the same collisions throw here as there.
The tag list is parsed from index.html at test time, at load order, so a
newly added classic script is guarded by construction. External CDN tags
(React, ReactDOM, Babel) are skipped: they are not ours to collide with
and the guard runs offline. type="text/babel" tags are out of scope too -
babel-standalone transpiles and evaluates those itself, outside the
classic-script instantiation.

The seams' typeof alone cannot catch a miswired seam: a rename in
parser-lanes.js leaves every seam a function bound to undefined helpers,
and typeof cannot see inside a closure. So the guard also runs the real
Codex fixture fixtures/parser/codex_min.jsonl through
window.parseTranscriptLanes after the load and requires usage records
out - the seams must not merely exist, they must work.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "public" / "index.html"
CODEX_PROBE_FIXTURE = ROOT / "fixtures" / "parser" / "codex_min.jsonl"

# The seams later scripts and the panels consume; all must survive the
# full classic-script load.
_SEAMS = (
    "parseLaneCodex",
    "parseTranscriptLanes",
    "sniffTranscriptFormat",
    "parseTranscript",
)

# Reads the (name, source) list as one JSON array on stdin, runs every
# script in order over ONE shared vm context (classic-script semantics),
# and reports the typeof of each seam after the full load. The first
# failing script is reported on stderr and exits nonzero.
_RUNNER = """
const vm = require('vm');
const { scripts, probe } = JSON.parse(require('fs').readFileSync(0, 'utf8'));
// pricing-loader.js takes its node path in the sandbox (no document
// here): it reads the rate document beside __dirname, so hand the
// context the require/__dirname pair its own docstring names as the
// test mode.
const ctx = vm.createContext({
  require: (name) => require(name),
  __dirname: process.env.CLAUDIT_SRC_DIR,
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
const seams = %SEAMS%;
const seam = (n) => vm.runInContext(`typeof window.${n}`, ctx);
ctx.__probe = probe;
const probeOut = vm.runInContext(`
  (() => {
    const r = window.parseTranscriptLanes(window.__probe);
    return { usage: r.meta.filter((m) => m.type === 'assistant_usage').length };
  })()
`, ctx);
console.log(JSON.stringify({
  ran: scripts.length,
  seams: Object.fromEntries(seams.map((n) => [n, seam(n)])),
  probe: probeOut,
}));
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
    """Return (name, source) per classic script tag, in document order.

    Classic = no type attribute (or a JS one). Babel tags are
    transpiled and evaluated by babel-standalone, not instantiated as
    classic scripts. CDN srcs (the unpkg.com trio) are skipped: not
    ours to collide with, and the guard runs offline.
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
            if not url.startswith("/"):
                continue
            path = ROOT / url.lstrip("/")
            out.append((url, path.read_text(encoding="utf-8")))
        elif body.strip():
            out.append(("<inline>", body))
    return out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_classic_scripts_load_in_one_realm() -> None:
    """All classic scripts instantiate over one shared global, no throw.

    A collision fails with the instantiation SyntaxError itself,
    prefixed by the failing script's index.html path.
    """
    scripts = _classic_scripts()
    assert scripts, "index.html lost its classic scripts; guard ran on nothing"
    runner = _RUNNER.replace("%SEAMS%", repr(list(_SEAMS)))
    proc = subprocess.run(
        ["node", "-e", runner],
        input=json.dumps({
            "scripts": scripts,
            "probe": CODEX_PROBE_FIXTURE.read_text(encoding="utf-8"),
        }).encode(),
        capture_output=True,
        timeout=60,
        env={**os.environ, "CLAUDIT_SRC_DIR": str(ROOT / "src")},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    summary = json.loads(proc.stdout.decode())
    for name in _SEAMS:
        assert summary["seams"][name] == "function", (
            f"window.{name} is {summary['seams'][name]!r} after the full "
            "classic-script load"
        )
    assert summary["probe"]["usage"] >= 1, (
        "the codex probe fixture yielded no assistant_usage through "
        "window.parseTranscriptLanes after the full load - a seam is "
        "miswired (e.g. a renamed helper left a seam a function bound "
        "to undefined)"
    )
