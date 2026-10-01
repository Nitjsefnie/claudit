"""Branding from config: APP_NAME / APP_TITLE / APP_DESCRIPTION drive every
user-visible brand string — the FastAPI title, the served page's <title>
and meta description, the injected window.BRAND, the login page's title
and heading, the export-PNG download filename and the frontend logo.

With nothing set, every surface reproduces today's claudit strings
byte-identically. HTML contexts (title element, meta attribute, login
page) get html-escaped values; the window.BRAND payload is JSON with `</`
escaped at the JS level, so it cannot close the <script> it lives in.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import app as app_mod
from backend.app import _nonce_script_elements
from backend import api_export
from backend import branding
from backend import login as login_mod
from backend import session as session_mod


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "public" / "index.html"


@pytest.fixture(name="page_client")
def _page_client_fixture():
    """The real backend.app, but WITHOUT lifespan (no `with`), so no DB
    is needed — `/` reads only template files and the environment. A
    guest cookie: `/` is session-gated (302 → /login without one), and a
    guest may load it, with IS_GUEST=true injected."""
    client = TestClient(app_mod.app)
    client.cookies.set(
        session_mod.SESSION_COOKIE_NAME,
        session_mod.make_guest_session_token(),
    )
    return client


@pytest.fixture(name="login_client")
def _login_client_fixture():
    a = FastAPI()
    a.include_router(login_mod.router)
    return TestClient(a)


def _diff_lines(template: str, served: str) -> list[str]:
    """+/- lines of the line diff template → served (no context)."""
    return [
        line for line in difflib.unified_diff(
            template.splitlines(), served.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]


# ---------------------------------------------------------------------------
# Served index page
# ---------------------------------------------------------------------------


def test_default_page_differs_from_template_only_by_injections(page_client):
    """Nothing set: today's page, byte-identical, except the injected
    window.BRAND (which rides the same <script> line as BACKEND_URL) and
    the mtime cache-busts the page always carried."""
    served = page_client.get("/").text
    template = INDEX.read_text(encoding="utf-8")
    ops = _diff_lines(template, served)
    assert ops, "the rewrite must touch the template"
    for line in ops:
        payload = line[1:]
        if line.startswith("+"):
            assert any(k in payload for k in (
                "window.BACKEND_URL = '/';", "window.BRAND = {", "?v=",
                '<script nonce="'
            )), f"unexpected served-line change: {payload}"
        else:
            assert any(k in payload for k in (
                "window.BACKEND_URL = window.BACKEND_URL",
                'href="/app.css"', 'src="/src/', 'src="https://unpkg.com/',
                '<script type="text/babel">'
            )), f"unexpected template-line change: {payload}"


def test_default_page_injects_default_brand(page_client):
    served = page_client.get("/").text
    assert (
        'window.BRAND = {"name": "claudit", '
        '"title": "claudit · Claude Code Usage Dashboard", '
        '"description": "Self-hosted dashboards for Claude Code session '
        'JSONL: cost-by-model, token breakdown, prompt-cache TTL split, '
        'response sizes, per-session context growth, session burn rate, '
        'tool-usage ratio, reply latency."}'
    ) in served
    assert "<title>claudit · Claude Code Usage Dashboard</title>" in served


def test_env_overrides_served_title_meta_and_brand(page_client, monkeypatch):
    monkeypatch.setenv("APP_NAME", "codexmeter")
    monkeypatch.setenv("APP_TITLE", "Codexmeter <x>")
    monkeypatch.setenv("APP_DESCRIPTION", 'He said "hi" & left')
    served = page_client.get("/").text
    assert "<title>Codexmeter &lt;x&gt;</title>" in served
    assert 'content="He said &quot;hi&quot; &amp; left"' in served
    assert 'window.BRAND = {"name": "codexmeter", "title": "Codexmeter <x>"' in served


def test_brand_payload_cannot_close_its_script_tag(page_client, monkeypatch):
    monkeypatch.setenv("APP_TITLE", 'a</script><b>')
    served = page_client.get("/").text
    assert 'a<\\/script><b>' in served
    # The only literal </script>s left are the template's own tag closes.
    assert served.count("</script>") == INDEX.read_text(
        encoding="utf-8").count("</script>")


def test_brand_rides_the_existing_injection_script(page_client):
    resp = page_client.get("/")
    served = resp.text
    # The guest cookie the fixture set: IS_GUEST comes out true, and
    # window.BRAND rides the SAME <script> line as BACKEND_URL/IS_GUEST,
    # which now also carries the response's CSP nonce.
    match = re.search(r"'nonce-([^']+)'",
                      resp.headers["content-security-policy"])
    assert match
    nonce = match.group(1)
    assert (
        f'<script nonce="{nonce}">window.BACKEND_URL = \'/\'; '
        "window.IS_GUEST = true; "
        "window.BRAND = {" in served
    )


# ---------------------------------------------------------------------------
# The CSP nonce rewrite vs. a brand value containing `<script` (#439)
# ---------------------------------------------------------------------------


class _ScriptCollector(HTMLParser):
    """Every script ELEMENT a real HTML parser sees, as (open tag,
    content) — stdlib `html.parser` brings its own CDATA handling for
    script raw text, so this is an INDEPENDENT oracle. A hand-rolled
    walk would re-declare the implementation's own regexes and could
    only ever agree with them; under the blanket-`replace` bug the
    injected tag lands inside a JS string literal, which is not an
    element to this parser either — so independence is what makes the
    question ("does every element the browser sees carry a nonce?")
    answerable at all."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.elements: list[tuple[str, str]] = []
        self._open: str | None = None
        self._body: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._open = self.get_starttag_text()
            self._body = []

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag == "script" and self._open is not None:
            self.elements.append((self._open, "".join(self._body)))
            self._open = None

    def handle_data(self, data):
        if self._open is not None:
            self._body.append(data)

    def close(self):
        super().close()
        if self._open is not None:          # an unterminated element
            self.elements.append((self._open, "".join(self._body)))


def _script_elements(html: str) -> list[tuple[str, str]]:
    parser = _ScriptCollector()
    parser.feed(html)
    parser.close()
    return parser.elements


def _raw_js_script_bodies(html: str) -> list[str]:
    """The content of every script element meant to be plain JavaScript —
    skipping `type="text/babel"`, which is JSX the browser compiles in
    place and node rightly rejects."""
    return [body for tag, body in _script_elements(html)
            if "text/babel" not in tag]


def _node_parses(js: str) -> tuple[bool, str]:
    """(does node accept this as JavaScript, the error it printed)."""
    res = subprocess.run(["node", "--check", "-"], input=js,
                         capture_output=True, text=True, check=False)
    if res.returncode == 0:
        return True, ""
    lines = [ln for ln in res.stderr.strip().splitlines() if ln.strip()]
    named = [ln for ln in lines if "Error" in ln]
    return False, (named[0] if named else (lines[0] if lines else ""))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_brand_containing_an_open_script_tag_leaves_the_page_loadable(
        page_client, monkeypatch):
    """#439: a brand value may legally contain the text `<script` — it is
    hostile but not forbidden config. The per-response nonce rewrite must
    not reach INSIDE the bootstrap script's JSON payload and inject its
    attribute's raw quotes there, which terminates the JS string literal
    and takes down every page load. The served inline script must still
    parse as JavaScript, checked by node itself."""
    monkeypatch.setenv("APP_TITLE", "Overview <script>alert(1)</script>")
    served = page_client.get("/").text

    bodies = _raw_js_script_bodies(served)
    bootstrap = [b for b in bodies if "window.BRAND" in b]
    assert bootstrap, "the bootstrap script must still be in the page"
    for body in bootstrap:
        ok, err = _node_parses(body)
        assert ok, f"bootstrap script is not valid JS: {err}\n{body}"

    # And the value survived as a string, quotes and all: a hostile-but-
    # legal config value degrades to a cosmetic string.
    assert '"Overview <script>alert(1)<\\/script>"' in served


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_bootstrap_stays_valid_js_for_every_script_unsafe_brand(
        page_client, monkeypatch):
    """The whole script-unsafe set in one value: each sequence
    script_json neutralises, plus a bare `<script` which it does not have
    to neutralise (it cannot close the tag) but the nonce rewrite must
    still not confuse for an element."""
    monkeypatch.setenv(
        "APP_TITLE",
        'a<script src="/x">b</script>c<!--d e'
        'f"g"h\'i\'j</script>k',
    )
    served = page_client.get("/").text
    for body in _raw_js_script_bodies(served):
        ok, err = _node_parses(body)
        assert ok, f"served inline script is not valid JS: {err}\n{body}"


@pytest.mark.parametrize("title", [
    None,
    "Overview <script>alert(1)</script>",
    '</script><script src="/x">',
    '<script>unterminated',
    '</SCRIPT><SCRIPT SRC="/x">',
])
def test_every_served_script_element_carries_the_nonce(
        page_client, monkeypatch, title):
    """The invariant #439's fix rests on, checked under a hostile brand
    value too: the rewrite tags script ELEMENTS, so every element an
    independent HTML parser sees in the served page carries the
    response's nonce — and, against the template's own count, that no
    payload `<script` has become an element the rewrite did not tag."""
    if title is not None:
        monkeypatch.setenv("APP_TITLE", title)
    resp = page_client.get("/")
    served = resp.text
    match = re.search(r"'nonce-([^']+)'", resp.headers["content-security-policy"])
    assert match, "every page load carries a fresh CSP nonce"
    nonce = match.group(1)
    elements = _script_elements(served)
    assert elements
    for tag, _body in elements:
        assert f'nonce="{nonce}"' in tag, f"untagged script element: {tag}"
    # The template's own element count, parsed the same independent way:
    # a brand value carrying `<script` must not add an element.
    assert len(elements) == len(_script_elements(INDEX.read_text("utf-8")))


# The rewrite is a pass-through: strip the attribute it added and the
# document must come back byte for byte. The walk REWRITES the served
# HTML rather than scanning it, so a tail it dropped would be as
# invisible to a reader as a tag it missed.
_WALKER_CASES = [
    "",
    "no scripts at all",
    "<script></script>",
    "<script>a<script>b</script>c</script>",
    "<SCRIPT>x</SCRIPT>",
    "<ScRiPt src='/x'>y</ScRiPt>",
    "<scriptx>a</scriptx>",
    "<script>a</script/><script>b</script>",
    "<script>a</script foo='1'>b</script>",
    "<script>a</script >b</script >",
    "<p>before</p><script>a</script><p>after</p>",
    "<script>unterminated tail",
    "<script>a<script>unterminated after a nested open",
    "<script>a</script>trailing text after the last close",
]


@pytest.mark.parametrize("html", _WALKER_CASES)
def test_nonce_rewrite_conserves_the_document(html):
    """Every byte survives the rewrite: removing the injected attribute
    returns the input exactly, casing included. An unterminated element
    runs to the end of the document, and an end tag the browser accepts
    (`</script/>`, `</script foo="1">`) ends the element as it does."""
    out = _nonce_script_elements(html, "NONCE")
    assert out.replace(' nonce="NONCE"', "") == html


@pytest.mark.parametrize("html", [
    "<script>a</script/><script>b</script>",
    "<script>a</script foo='1'>b</script>",
    "<script>a</script >b</script >",
    "<script>a</script\t>b</script\t>",
])
def test_nonce_rewrite_tags_every_element_a_browser_sees(html):
    """The end-tag grammar is the browser's, not `</script>` alone: a
    self-closing or attribute-bearing end tag closes the element as
    surely as a bare one, so the element after it must still be tagged
    — an untagged one is blocked by the very CSP the nonce satisfies."""
    out = _nonce_script_elements(html, "NONCE")
    tags = _script_elements(out)
    assert len(tags) == len(_script_elements(html)), (
        f"element count changed: {out!r}")
    for tag, _body in tags:
        assert 'nonce="NONCE"' in tag, f"untagged element: {out!r}"


# ---------------------------------------------------------------------------
# FastAPI / OpenAPI title
# ---------------------------------------------------------------------------


def test_fastapi_title_follows_env():
    """The FastAPI title is read at import; each case boots a fresh
    interpreter (no lifespan, no DB) and prints app.title."""
    code = "import backend.app as m; print(m.app.title)"
    base = {k: v for k, v in os.environ.items() if not k.startswith("APP_")}
    for env_name, want in (("APP_NAME", "codexmeter"), ("APP_UNSET_X", None)):
        env = dict(base)
        if want is not None:
            env[env_name] = want
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, env=env,
            capture_output=True, text=True, check=True,
        )
        assert out.stdout.strip() == ("codexmeter" if want else "claudit")


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------


def test_login_page_default_is_byte_identical(login_client, monkeypatch):
    monkeypatch.delenv("APP_NAME", raising=False)
    html = login_client.get("/login").text
    assert "<title>Sign in · CLAUDIT</title>" in html
    assert "<h1>CLAUDIT · sign in</h1>" in html


def test_login_page_follows_app_name(login_client, monkeypatch):
    monkeypatch.setenv("APP_NAME", "codexmeter")
    html = login_client.get("/login").text
    assert "<title>Sign in · CODEXMETER</title>" in html
    assert "<h1>CODEXMETER · sign in</h1>" in html


def test_login_page_escapes_the_name(login_client, monkeypatch):
    monkeypatch.setenv("APP_NAME", 'a<b>&"c')
    html = login_client.get("/login").text
    assert "<title>Sign in · A&lt;B&gt;&amp;&quot;C</title>" in html
    assert "<h1>A&lt;B&gt;&amp;&quot;C · sign in</h1>" in html


# ---------------------------------------------------------------------------
# URL-attribute context (the privacy-notice href, #365)
# ---------------------------------------------------------------------------


def test_url_attr_allows_only_http_https_and_site_relative():
    assert branding.url_attr("https://ex.example/privacy") == (
        "https://ex.example/privacy")
    assert branding.url_attr("http://ex.example/privacy") == (
        "http://ex.example/privacy")
    assert branding.url_attr("/privacy") == "/privacy"


def test_url_attr_escapes_the_accepted_value_for_the_attribute():
    """The function owns the whole context: an accepted value comes back
    ready to drop into a double-quoted attribute, `&` and `"` included."""
    assert branding.url_attr('https://ex.example/p?x=1&z="q"') == (
        'https://ex.example/p?x=1&amp;z=&quot;q&quot;')


@pytest.mark.parametrize("value", [
    "javascript:alert(document.domain)",
    "JaVaScRiPt:alert(document.domain)",
    " javascript:alert(document.domain)",
    "\tjavascript:alert(document.domain)",
    "\x01javascript:alert(document.domain)",
    "jav\tascript:alert(document.domain)",
    "&#x6A;avascript:alert(document.domain)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "vbscript:MsgBox",
    "//evil.example/steal",
    # #438: the URL parser maps `\` to `/` for special schemes, so these
    # are the `//` scheme-relative shape spelled with a backslash.
    "/\\evil.example/steal",
    "/\\\\evil.example",
    "/\t\\evil.example",
    "/\n\\evil.example",
    "/\r\\evil.example",
    # A bare leading `\` alone resolves same-origin (the parser reads it as
    # a relative path); refused anyway, because the rule is positional
    # and a value this shape reaches in one browser may not in another.
    "\\evil.example/steal",
    # #450: the same ride with a character the parser REMOVES rather than
    # one it rewrites. A tab, LF or CR is stripped from anywhere in the
    # value, so `/\t/evil.example` is `//evil.example` by the time the
    # browser resolves it — the `//` this guard already refused, spelled
    # across a removed character.
    "/\t/evil.example",
    "/\n/evil.example",
    "/\r/evil.example",
    "/\t//evil.example",
    "/\n//evil.example",
    "/\r//evil.example",
    # ... and the same ride on the http(s) branch's authority.
    "https://\\evil.example",
    "https:///\\evil.example",
    "http:\\\\evil.example",
])
def test_url_attr_refuses_every_non_allowlisted_shape(value):
    """#365: an allow-list, not a javascript: deny-list. Refused in every
    spelling a browser normalises — mixed case, leading whitespace and
    C0 controls the URL parser strips, an inner tab it removes, an HTML
    entity the attribute parser decodes before the URL parser runs —
    plus data: and vbscript:, the scheme-relative `//` shape, and #438's
    backslash spelling of it (`/\\host`, which browsers read as
    `//host`)."""
    assert branding.url_attr(value) is None


# The values #438 pins as still valid, asserted on the exact body the
# function returns — a refusal must be the ONLY thing that changes.
# Absolute http(s) URLs name the operator's own notice host, so any host
# is allowed there; a SITE-RELATIVE value must stay on this origin.
_ABSOLUTE_URLS = [
    "https://ex.example/privacy",
    "http://ex.example/privacy",
    "https://ex.example/privacy?v=1#top",
    "https://ex.example/a%5Cb",
]
_SITE_RELATIVE_URLS = [
    "/privacy",
    "/privacy/notice?a=1",
    "/privacy#top",
    "/a%5Cb",
]


@pytest.mark.parametrize("value", _ABSOLUTE_URLS + _SITE_RELATIVE_URLS)
def test_url_attr_accepts_the_still_valid_values_unchanged(value):
    """#438 must not narrow the allow-list: each of these comes back as
    its own body, so a backslash guard cannot have broken a legitimate
    privacy-notice URL (a backslash that means itself is spelled `%5C`)."""
    assert branding.url_attr(value) == value


def test_url_attr_accepts_a_backslash_percent_encoded():
    """The URL parser leaves `%5C` alone, so refusing a raw `\\` costs a
    real URL nothing (node: new URL('/a%5Cb', base) stays same-origin)."""
    assert branding.url_attr("/a%5Cb") == "/a%5Cb"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_every_accepted_site_relative_url_resolves_same_origin(caplog):
    """#438 / #450, the property the site-relative branch exists to
    provide: whatever `url_attr` hands back goes into an href, and a
    browser resolves it against this origin.

    The negative space is DERIVED, not written out. Two character sets
    ride the front of the path — every C0 control and space the parser
    removes or percent-encodes, plus the `/` and the backslash it
    rewrites to a separator — crossed with every position the escape
    can sit in. The
    whole class is measured in node's own WHATWG parser, the
    implementation this check models, so a shape the repo reasons about
    wrongly fails here instead of in a browser. A hand-written list can
    only ever be as complete as the person who wrote it, and two of the
    three escapes in this file's history (the backslash, then the
    tab-separated `//`) were exactly the members a hand-written list
    missed.

    A browser that cannot parse a value at all is not a leak — the
    anchor goes nowhere — so only a value that parses to a DIFFERENT
    origin fails. The oracle comes from node, independently of
    `branding`, so it can contradict the repo's own belief, and both
    extremes bite: a refuse-everything and an accept-everything mutant
    each fail this file."""
    base = "https://op.example/dashboard"
    origin = "https://op.example"
    # The class is thousands of refusals by design; the warnings they
    # log are asserted separately, once, on one value.
    caplog.set_level(logging.ERROR, logger="claudit.branding")
    seps = [chr(i) for i in range(0x21)] + ["\\", "/", "//"]
    class_values = {
        pre + s1 + s2 + "evil.example" + tail
        for pre in ("/", "/a", "/a/b")
        for s1 in seps for s2 in ("", "/", "\\")
        for tail in ("", "/x", "/x?q", "#f")
    }
    offsite = ["/\\evil.example", "/\\\\evil.example", "/\t\\evil.example",
               "/\t/evil.example", "/\n/evil.example", "/\r/evil.example",
               "https://\\evil.example", "https:///\\evil.example",
               "//evil.example", "javascript:alert(1)"]
    candidates = sorted(set(class_values) | set(_SITE_RELATIVE_URLS)
                        | set(offsite))
    script = (
        "const out = [];"
        # A JSON array literal is a valid JS array literal.
        f"for (const v of {json.dumps(candidates)}) {{"
        f"  let href; try {{ href = new URL(v, {base!r}).href; }}"
        f"  catch (e) {{ href = null; }}"
        "  out.push([v, href]);"
        "} console.log(JSON.stringify(out));"
    )
    # The script goes in over STDIN, not argv: 1296 JSON-encoded values
    # is a ~100 KB command line, and Windows caps one at 32,767 characters
    # (WinError 206, both windows legs of the matrix).
    res = subprocess.run(["node", "-"], input=script, capture_output=True,
                         text=True, check=True)
    resolved = dict(json.loads(res.stdout))

    for value in _SITE_RELATIVE_URLS:
        assert branding.url_attr(value) is not None, value
        assert resolved[value].startswith(origin + "/"), (
            f"{value!r} resolves off-origin: {resolved[value]}")

    # The class: whatever this repo ACCEPTS must not leave the origin.
    leaked = [
        (value, resolved[value]) for value in class_values
        if branding.url_attr(value) is not None
        and resolved[value] is not None
        and not resolved[value].startswith(origin + "/")
    ]
    assert not leaked, f"accepted values that resolve off-origin: {leaked[:5]}"

    for value in offsite:
        assert not (resolved[value] is not None
                    and resolved[value].startswith(origin + "/")), (
            f"{value!r} no longer resolves off-origin, so it no longer "
            f"needs refusing: {resolved[value]}")
        assert branding.url_attr(value) is None, value


def test_url_attr_refusal_is_visible_to_the_operator(caplog):
    """A refused value drops the link AND logs a warning: a silent drop
    would hide the operator's misconfiguration."""
    with caplog.at_level(logging.WARNING, logger="claudit.branding"):
        assert branding.url_attr("javascript:alert(1)") is None
    assert any(
        "javascript:alert(1)" in record.getMessage()
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Export-PNG download filename
# ---------------------------------------------------------------------------


def test_export_filename_default(monkeypatch):
    monkeypatch.delenv("APP_NAME", raising=False)
    assert api_export.export_filename("30d", "proj/one") == (
        "claudit_proj_one_30d.png")


def test_export_filename_follows_brand(monkeypatch):
    monkeypatch.setenv("APP_NAME", "codexmeter")
    assert api_export.export_filename("30d", "proj/one") == (
        "codexmeter_proj_one_30d.png")
    monkeypatch.setenv("APP_NAME", "My Meter")  # slugified like project
    assert api_export.export_filename("all", None) == "My_Meter_all_all.png"


def test_export_filename_never_leads_with_underscore(monkeypatch):
    """APP_NAME set but empty used to export "_all_all.png"; an empty
    or whitespace-only value must fall back to the default name."""
    monkeypatch.setenv("APP_NAME", "")
    assert api_export.export_filename("all", None) == (
        f"{branding.DEFAULT_NAME}_all_all.png")
    monkeypatch.setenv("APP_NAME", "   ")
    assert api_export.export_filename("30d", "proj/one") == (
        f"{branding.DEFAULT_NAME}_proj_one_30d.png")


# ---------------------------------------------------------------------------
# Frontend source-level guards
# ---------------------------------------------------------------------------


def test_app_jsx_has_no_literal_brand():
    """The brand reaches app.jsx only through window.BRAND; a literal
    CLAUDIT / claudit_ there is a regression to the hardcoded name."""
    src = (ROOT / "src" / "app.jsx").read_text(encoding="utf-8")
    src = re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)
    assert not re.search(r"CLAUDIT", src)
    assert not re.search(r"claudit_", src)
    assert re.search(r"window\.BRAND", src), "brand must come from the page"


def test_script_json_escapes_close_tag():
    assert branding.script_json({"a": "x</script>y"}) == (
        '{"a": "x<\\/script>y"}')


def test_script_json_escapes_every_script_unsafe_sequence():
    """Beyond `</`: `<!--` opens a script-hiding HTML comment, and raw
    U+2028/U+2029 are JS line terminators inside a string literal even
    though JSON treats them as ordinary whitespace. The `<\\!--` output
    is a JS string escape, deliberately not JSON-valid (`\\!` is no JSON
    escape): the payload is consumed as a JS object literal
    (src/app.jsx's `window.BRAND`), never JSON.parse'd."""
    assert branding.script_json("a b c") == (
        '"a\\u2028b\\u2029c"')
    assert "<!--" not in branding.script_json("x<!--y")
    assert branding.script_json("x<!--y") == '"x<\\!--y"'


def test_brand_payload_through_the_page_survives_hostile_title(
        page_client, monkeypatch):
    """End to end: every script-unsafe sequence in an env value is
    escaped by the time the page is served."""
    monkeypatch.setenv("APP_TITLE", 'a</script>b<!--c d')
    served = page_client.get("/").text
    assert "a<\\/script>b<\\!--c\\u2028d" in served


def test_brand_defaults_are_todays_strings():
    assert branding.DEFAULT_NAME == "claudit"
    assert branding.DEFAULT_TITLE == "claudit · Claude Code Usage Dashboard"
    assert branding.DEFAULT_DESCRIPTION == (
        "Self-hosted dashboards for Claude Code session JSONL: "
        "cost-by-model, token breakdown, prompt-cache TTL split, response "
        "sizes, per-session context growth, session burn rate, "
        "tool-usage ratio, reply latency.")


def test_empty_or_whitespace_brand_values_fall_back_to_defaults(monkeypatch):
    """APP_NAME set but EMPTY renders an empty logo and exports
    "_all_all.png": a value that is empty or whitespace-only must read
    as unset for all three brand strings."""
    for value in ("", "   "):
        monkeypatch.setenv("APP_NAME", value)
        monkeypatch.setenv("APP_TITLE", value)
        monkeypatch.setenv("APP_DESCRIPTION", value)
        assert branding.brand() == {
            "name": branding.DEFAULT_NAME,
            "title": branding.DEFAULT_TITLE,
            "description": branding.DEFAULT_DESCRIPTION,
        }
