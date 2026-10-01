"""Source-level and route-level guards for the page shell (issues #447/#448).

Same boundary as test_a11y_wiring.py: node cannot parse JSX and nothing
here renders React, so a page can lose its level-one heading or its main
landmark while the whole suite stays green, and index.html can drop the
icon declaration so every page load 404s /favicon.ico. These guards pin:

1. exactly one <h1> renders per page state -- Overview (Dashboard),
   Sessions (SessionsList), Cache (CacheView), Inspector (SessionView:
   one in the loaded path and one in the empty path, plus the App's
   fetch-error branch), sign-in (login_page.py, already carried);
2. the src tree carries exactly ONE <main> -- the shell's, inside App --
   and the Inspector's detail pane is a named <section>;
3. index.html declares a same-origin icon (no data: URI -- the CSP's
   img-src 'self' admits none), /favicon.ico is public (favicons load
   on the sign-in page too) and serves the committed bytes.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import app

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "app.jsx"
SESSIONS = ROOT / "src" / "sessions-list.jsx"
CACHE = ROOT / "src" / "views" / "cache-view.jsx"
CSS = ROOT / "public" / "app.css"
INDEX = ROOT / "public" / "index.html"
LOGIN = ROOT / "backend" / "login_page.py"

# h1 SITES per file across the src tree: app.jsx carries Dashboard's
# Overview heading, SessionView's two Inspector paths and the App's
# fetch-error branch.
_H1_SITES = {"app.jsx": 4, "sessions-list.jsx": 1, "cache-view.jsx": 1}


def _src_files() -> list[Path]:
    """Every .js and .jsx file under src/: the censuses cover both, so a
    heading or landmark in a node-executed .js file cannot hide from
    them either.
    """
    return sorted(p for p in (ROOT / "src").rglob("*")
                  if p.suffix in (".js", ".jsx"))


def _strip_line_comments(src: str) -> str:
    """Drop `//` line comments so prose ABOUT the wiring is not read as
    the wiring. The lookbehind spares `https://` (a colon precedes those
    slashes), the only // that shows up mid-expression here.
    """
    return re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)


def _panel_src(name: str, src: str) -> str:
    """One component's body: from its `function <name>(` to the next
    top-level `function`/`window.` -- the shared a11y-guard helper, so
    a sibling occurrence in another panel cannot satisfy a pin.
    """
    start = src.index(f"function {name}(")
    nxt = re.search(r"^(?:function |window\.)", src[start + 1:], re.M)
    end = start + 1 + nxt.start() if nxt else len(src)
    return src[start:end]


# -- One h1 per page (issue #447) ---------------------------------------

def test_every_page_renders_exactly_one_h1():
    """Each page state renders exactly one level-one heading, named for
    its page."""
    app_src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    dashboard = _panel_src("Dashboard", app_src)
    assert dashboard.count("<h1") == 1, (
        "Dashboard must render exactly one h1 (its page title)")
    assert "<h1>Overview</h1>" in dashboard
    session_view = _panel_src("SessionView", app_src)
    assert session_view.count("<h1") == 2, (
        "SessionView must render exactly one h1 per render path (loaded "
        "and empty), so the page keeps its heading in both states")
    assert session_view.count("<h1>Inspector</h1>") == 2
    app_fn = _panel_src("App", app_src)
    assert app_fn.count("<h1") == 1, (
        "the App's session fetch-error branch must carry the page h1")
    assert "<h1>Inspector</h1>" in app_fn
    sessions_list = _panel_src(
        "SessionsList",
        _strip_line_comments(SESSIONS.read_text(encoding="utf-8")))
    assert sessions_list.count("<h1") == 1
    assert "<h1>Sessions</h1>" in sessions_list
    cache_view = _panel_src(
        "CacheView", _strip_line_comments(CACHE.read_text(encoding="utf-8")))
    assert cache_view.count("<h1") == 1
    assert "<h1>Cache</h1>" in cache_view
    # Tree census: no other h1 site may appear anywhere in src/.
    for path in _src_files():
        n = len(re.findall(r"<h1\b", _strip_line_comments(
            path.read_text(encoding="utf-8"))))
        assert n == _H1_SITES.get(path.name, 0), (
            f"{path.name} carries {n} h1 sites (expected "
            f"{_H1_SITES.get(path.name, 0)}) -- an unexpected h1 breaks "
            f"the one-per-page contract")
    # The page-head style tracks h1, not the demoted h2.
    assert re.search(r"^\.page-head h1 \{", CSS.read_text(encoding="utf-8"),
                     re.M), ".page-head h1 rule missing from app.css"


def test_sign_in_page_carries_exactly_one_h1():
    """The sign-in page already carried its h1 ({app_name} · sign in);
    the pin keeps it from regressing."""
    src = LOGIN.read_text(encoding="utf-8")
    assert len(re.findall(r"<h1\b", src)) == 1


# -- The main landmark (issue #447) --------------------------------------

def test_main_landmark_is_unique_and_shell_level():
    """Exactly one <main> exists across the src tree: the shell's, in
    App's own render, wrapping the pickers and every route branch."""
    hits = []
    for path in _src_files():
        src = _strip_line_comments(path.read_text(encoding="utf-8"))
        hits.extend(
            (path.name, src.count("\n", 0, m.start()) + 1)
            for m in re.finditer(r"<main\b", src))
    assert len(hits) == 1, (
        f"the src tree carries {len(hits)} <main> landmarks: {hits} -- "
        f"main is the ONE page landmark and belongs to the shell")
    name, _line = hits[0]
    assert name == "app.jsx"
    app_fn = _panel_src("App", _strip_line_comments(
        APP.read_text(encoding="utf-8")))
    assert '<main id="main">' in app_fn, (
        'the shell landmark moved; relocate this guard with it')
    assert app_fn.index('<main id="main">') < app_fn.index(
        "route === 'dashboard'"), (
        "the main landmark does not wrap the route branches")
    assert app_fn.rindex("</main>") > app_fn.index("route === 'session'"), (
        "the main landmark does not wrap the route branches")
    assert app_fn.rindex("</main>") < app_fn.index('aria-live="polite"'), (
        "the live region must sit outside the main landmark")


def test_inspector_detail_pane_is_a_named_section():
    """The Inspector's detail pane demotes to a named <section>: the
    page-level main is the shell's, and an unlabeled nested landmark
    misreads the page outline."""
    sv = _panel_src("SessionView", _strip_line_comments(
        APP.read_text(encoding="utf-8")))
    assert '<main className="session-detail">' not in sv
    assert '<section className="session-detail" aria-label="Event detail">' in sv


# -- The favicon (issue #448) --------------------------------------------

def test_index_declares_a_same_origin_icon():
    """index.html declares the icon as a same-origin path: the CSP's
    img-src 'self' governs favicon fetches and admits no data: URI, so
    the data-URI/inline-SVG shape cannot serve here."""
    html = INDEX.read_text(encoding="utf-8")
    m = re.search(r'<link rel="icon" href="([^"]+)"[^>]*>', html)
    assert m, 'no <link rel="icon"> in index.html'
    assert m.group(1).startswith("/favicon.ico"), (
        f'icon href {m.group(1)!r} is not the same-origin /favicon.ico -- '
        f"img-src 'self' (backend/app.py _CSP_TEMPLATE) admits no data: "
        f"URI and no cross-host icon")
    tag = m.group(0)
    assert 'sizes="32x32"' in tag


def test_favicon_route_serves_the_committed_icon_publicly():
    """/favicon.ico answers 200 without a session (favicons load on the
    sign-in page too), with the bytes of the committed icon."""
    icon = (ROOT / "public" / "favicon.ico").read_bytes()
    assert icon[:4] == b"\x00\x00\x01\x00", (
        "public/favicon.ico lost its ICO magic")
    resp = TestClient(app).get("/favicon.ico", follow_redirects=False)
    assert resp.status_code == 200, (
        "GET /favicon.ico must be public (session._AUTH_PUBLIC_PATHS) -- "
        "an unauthenticated visitor's browser still fetches it")
    assert resp.headers["content-type"].startswith("image/x-icon")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"] == "no-cache, must-revalidate"
    assert resp.content == icon
