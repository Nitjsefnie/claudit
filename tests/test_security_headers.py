"""Security headers on every response (issue #369).

Framing protection (`Content-Security-Policy: frame-ancestors 'none'`
plus `X-Frame-Options: DENY`) rides on text/html; `X-Content-Type-Options:
nosniff` rides on every response, because every content type the app
serves is correct and the header only stops a MIME-confused response
from being reinterpreted as script or style.

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

from backend.app import _SecurityHeaders, app


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
    assert resp.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_dashboard_sends_framing_protection_and_nosniff(guest_client):
    resp = guest_client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-security-policy"] == "frame-ancestors 'none'"
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
    assert _header_values(sent, "content-security-policy") == [
        "frame-ancestors 'none'"]
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
    assert _header_values(sent, "content-security-policy") == [
        "frame-ancestors 'none'"]
    assert _header_values(sent, "x-content-type-options") == ["nosniff"]
