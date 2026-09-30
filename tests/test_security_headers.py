"""Security headers on every response (issues #369, #384).

The document CSP (_CSP_TEMPLATE: script-src over 'self', the unpkg CDN
and the response nonce; style-src over the Google Fonts stylesheet host
and the response nonce; the deny-by-default floor) plus framing
protection (`X-Frame-Options: DENY`, frame-ancestors 'none' in the
policy) ride on text/html; `X-Content-Type-Options: nosniff` rides on
every response, because every content type the app serves is correct
and the header only stops a MIME-confused response from being
reinterpreted as script or style.

The integration tests drive the real `backend.app.app`, so the
middleware placement is under test too: the headers must reach the
responses the auth middleware generates itself (its redirect for an
unauthenticated page load), which only an outermost middleware sees.
The middleware-level tests pin the per-class decisions (status
independence, no clobbering, streaming) against `_SecurityHeaders`
directly.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from backend.app import _CSP_TEMPLATE, _SecurityHeaders, app


@pytest.fixture(name="guest_client")
def _guest_client_fixture():
    client = TestClient(app)
    resp = client.post(
        "/login/guest",
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return client


def test_login_page_sends_framing_protection_and_nosniff():
    resp = TestClient(app).get("/login")
    assert resp.status_code == 200
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_dashboard_sends_framing_protection_and_nosniff(guest_client):
    resp = guest_client.get("/")
    assert resp.status_code == 200
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_auth_middleware_redirect_gets_nosniff():
    """The 302 the auth middleware returns itself for an
    unauthenticated page load is generated above every inner layer;
    seeing it proves the header middleware sits outside the auth
    middleware."""
    resp = TestClient(app).get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_json_401_gets_nosniff_only():
    """JSON responses carry nosniff; the framing headers are a document
    protection and stay off non-document responses."""
    resp = TestClient(app).get("/api/me")
    assert resp.status_code == 401
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in resp.headers
    assert "x-frame-options" not in resp.headers


def test_static_css_gets_nosniff_only(guest_client):
    resp = guest_client.get("/app.css")
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in resp.headers
    assert "x-frame-options" not in resp.headers


# --- middleware-level pins -------------------------------------------------

def _drive(status, raw_headers, body_chunks):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    async def downstream(scope, recv, snd):
        await snd({"type": "http.response.start", "status": status,
                   "headers": raw_headers})
        for chunk in body_chunks:
            await snd({"type": "http.response.body", "body": chunk,
                       "more_body": False})

    asyncio.run(_SecurityHeaders(downstream)(
        {"type": "http", "method": "GET", "path": "/x"}, receive, send))
    return sent


def _header_values(sent, name):
    start = sent[0]
    return [value.decode("ascii") for key, value in start["headers"]
            if key.decode("ascii") == name]


def test_sse_class_gets_nosniff_only():
    """A streaming text/event-stream response carries nosniff on its
    start message and none of the framing headers, and its body passes
    through unwrapped."""
    sent = _drive(status=200,
                  raw_headers=[(b"content-type", b"text/event-stream"),
                               (b"cache-control", b"no-cache, no-transform")],
                  body_chunks=[b": connected\n\n"])
    assert _header_values(sent, "x-content-type-options") == ["nosniff"]
    assert _header_values(sent, "content-security-policy") == []
    assert _header_values(sent, "x-frame-options") == []
    body = [m for m in sent if m["type"] == "http.response.body"]
    assert body == [{"type": "http.response.body", "body": b": connected\n\n",
                     "more_body": False}]


def test_html_error_page_gets_framing_headers():
    """Framing protection keys on the content type, not the status: an
    HTML error page is framed exactly like the dashboard."""
    sent = _drive(status=404,
                  raw_headers=[(b"content-type", b"text/html; charset=utf-8")],
                  body_chunks=[b"<html></html>"])
    assert "frame-ancestors 'none'" in _header_values(
        sent, "content-security-policy")[0]
    assert _header_values(sent, "x-frame-options") == ["DENY"]
    assert _header_values(sent, "x-content-type-options") == ["nosniff"]


def test_existing_headers_are_not_clobbered():
    """A response that sets its own header keeps it; the middleware only
    fills what is missing (the PNG export's Content-Disposition and any
    deliberate security header stay untouched)."""
    sent = _drive(status=200,
                  raw_headers=[(b"content-type", b"text/html"),
                               (b"x-frame-options", b"SAMEORIGIN")],
                  body_chunks=[b"<html></html>"])
    assert _header_values(sent, "x-frame-options") == ["SAMEORIGIN"]
    assert "frame-ancestors 'none'" in _header_values(
        sent, "content-security-policy")[0]
    assert _header_values(sent, "x-content-type-options") == ["nosniff"]


# --- full script-src policy (issue #384) ------------------------------------

# Every directive the app needs, pinned one by one. The evidence for each
# lives in the PR for #384; the short form: the unpkg scripts are admitted by
# host (SRI pins their bytes independently); every <script> tag the dashboard
# serves carries the response's nonce, and Babel standalone propagates a
# text/babel source tag's nonce onto the inline script element it generates
# for the compiled output (verified in the pinned 7.29.0 bytes), so the whole
# in-browser pipeline admits under script-src's one nonce with NO
# 'unsafe-inline' anywhere in the policy (hashes cannot cover compiler output
# that varies per file and per Babel version; 'unsafe-eval' is not required —
# no new Function in the pinned bytes). The sign-in page's inline style block
# is the only inline style, admitted by the response's nonce; React's CSSOM
# styling is not CSP-governed. fetch/XHR/SSE are same-origin; the Google
# Fonts pair needs one style host and one font host. default-src 'none',
# base-uri 'none' and form-action 'self' are the deny-by-default floor;
# frame-ancestors 'none' stays from #369. The two nonce-shaped directives
# (script-src, style-src) are pinned against the body nonce by the page
# tests, not by this fixed-value table.
_EXPECTED_CSP_DIRECTIVES = {
    "default-src": "'none'",
    "font-src": "https://fonts.gstatic.com",
    "connect-src": "'self'",
    "img-src": "'self'",
    "base-uri": "'none'",
    "form-action": "'self'",
    "frame-ancestors": "'none'",
}


def _csp_directives(policy: str) -> dict:
    out = {}
    for part in policy.split(";"):
        part = part.strip()
        if part:
            name, value = part.split(" ", 1)
            out[name] = value
    return out


def test_login_csp_pins_every_directive():
    resp = TestClient(app).get("/login")
    assert resp.status_code == 200
    policy = resp.headers["content-security-policy"]
    directives = _csp_directives(policy)
    assert set(directives) == set(_EXPECTED_CSP_DIRECTIVES) | {
        "script-src", "style-src"}
    for name, expected in _EXPECTED_CSP_DIRECTIVES.items():
        assert directives[name] == expected, name
    # Both nonce-shaped directives carry the response's one token, and
    # no 'unsafe-inline' snuck back into the policy.
    nonce = directives["style-src"].split("'nonce-")[1][:-1]
    assert directives["style-src"] == (
        f"'self' https://fonts.googleapis.com 'nonce-{nonce}'")
    assert directives["script-src"] == f"'self' https://unpkg.com 'nonce-{nonce}'"
    assert "'unsafe-inline'" not in policy


def test_dashboard_csp_pins_every_directive(guest_client):
    resp = guest_client.get("/")
    assert resp.status_code == 200
    policy = resp.headers["content-security-policy"]
    directives = _csp_directives(policy)
    assert set(directives) == set(_EXPECTED_CSP_DIRECTIVES) | {
        "script-src", "style-src"}
    for name, expected in _EXPECTED_CSP_DIRECTIVES.items():
        assert directives[name] == expected, name
    nonce = directives["style-src"].split("'nonce-")[1][:-1]
    assert directives["script-src"] == f"'self' https://unpkg.com 'nonce-{nonce}'"
    assert directives["style-src"] == (
        f"'self' https://fonts.googleapis.com 'nonce-{nonce}'")
    assert "'unsafe-inline'" not in policy


def test_dashboard_script_tags_all_carry_the_header_nonce(guest_client):
    """Babel admits only through the propagated nonce, so EVERY script
    tag the page serves must be nonced with the header's token — a
    nonce-less tag would be blocked at load."""
    resp = guest_client.get("/")
    nonce = _csp_directives(
        resp.headers["content-security-policy"])["style-src"].split(
        "'nonce-")[1][:-1]
    body = resp.text
    assert body.count("<script") == body.count(f'<script nonce="{nonce}"')
    # 3 unpkg + 1 injected classic + 6 plain /src/*.js + 11 text/babel
    # (10 src + 1 inline) as of this writing; the equality above is the
    # real pin, the floor just fails loud if the page empties.
    assert body.count("<script") >= 21


def test_login_inline_style_carries_the_header_nonce():
    """style-src admits the sign-in page's inline style block by the same
    per-response nonce the header carries — no 'unsafe-inline' in
    style-src."""
    resp = TestClient(app).get("/login")
    policy = resp.headers["content-security-policy"]
    nonce = _csp_directives(policy)["style-src"].split("'nonce-")[1][:-1]
    assert f'<style nonce="{nonce}">' in resp.text


def test_csp_nonce_differs_per_response():
    a = TestClient(app).get("/login").headers["content-security-policy"]
    b = TestClient(app).get("/login").headers["content-security-policy"]
    assert a != b


def test_html_error_page_carries_the_full_policy():
    sent = _drive(status=404,
                  raw_headers=[(b"content-type", b"text/html; charset=utf-8")],
                  body_chunks=[b"<html></html>"])
    policy = _header_values(sent, "content-security-policy")[0]
    directives = _csp_directives(policy)
    nonce = directives["style-src"].split("'nonce-")[1][:-1]
    assert directives["script-src"] == f"'self' https://unpkg.com 'nonce-{nonce}'"
    assert directives["style-src"] == (
        f"'self' https://fonts.googleapis.com 'nonce-{nonce}'")
    assert directives["frame-ancestors"] == "'none'"


def test_middleware_nonce_reaches_the_scope_state():
    """The nonce in the header is the one the middleware stashed in
    scope.state — the channel the sign-in page reads to nonce its style
    block."""
    scope: dict = {"type": "http", "method": "GET", "path": "/login"}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    async def downstream(scp, recv, snd):
        # Echo the stash into a header so the read is load-bearing: it
        # must have happened before this response starts.
        echo = scp["state"]["csp_nonce"]
        await snd({"type": "http.response.start", "status": 200,
                   "headers": [(b"content-type", b"text/html"),
                               (b"x-probe-nonce", echo.encode())]})

    asyncio.run(_SecurityHeaders(downstream)(scope, receive, send))
    policy = _header_values(sent, "content-security-policy")[0]
    # The header carries exactly the template instantiated with the
    # nonce the HTML producers find in scope.state — the same one the
    # downstream app echoed.
    echoed = _header_values(sent, "x-probe-nonce")[0]
    assert policy == _CSP_TEMPLATE.format(nonce=echoed)
    assert echoed == scope["state"]["csp_nonce"]
