"""The sign-in page records the start of the sign-in journey (#436).

Issue #436 item (1) measures sign-in submit -> signed-in page, and that
journey spans a document that no longer exists by the time the signed-in
page runs: the form POSTs, the server answers a bare `303` to `/`, and
the marker has to survive the navigation to be measured at all.

So the sign-in page stamps the instant the form is submitted, and
`src/perf.js` picks it up on the next document (see test_perf_js.py). The
half that survives the redirect is measured in the LOGIN page's time
origin, so the marker carries that origin alongside the elapsed time:
the two documents' `performance.now()` are unrelated numbers, and only
the origins make the span comparable.

The sign-in page has its own CSP -- one per-response nonce, no
'unsafe-inline' in script-src -- so the marker is an inline script
carrying that nonce, and the failure paths re-render the same page with
the same script.
"""
from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth
from backend import login as login_mod
from backend import session as session_mod
from backend.app import _SecurityHeaders

_ORIGIN = {"Origin": "http://testserver"}

START_KEY = "claudit.signin.start"
ORIGIN_KEY = "claudit.signin.origin"


# The sign-in routes over the REAL middleware stack, so the nonce the
# page renders is the one the CSP header carries -- which is the whole
# point of the assertion. The user and session stores are stubbed so a
# POST never reaches the auth DB.
@pytest.fixture(name="page")
def _page(monkeypatch):
    config: dict = {}
    auth.set_web_password(config, "hunter2")
    store = {12345: config}
    monkeypatch.setattr(session_mod, "load_user_config", store.get)
    monkeypatch.setattr(session_mod, "get_or_create_session_row",
                        lambda uid, fp: ("s" * 32, 0))
    monkeypatch.setattr(session_mod, "load_session_row", lambda uid: None)

    a = FastAPI()
    a.middleware("http")(session_mod.auth_middleware)
    # Raw ASGI middleware, added last so it wraps the auth middleware and
    # can stash the nonce on the scope before the login page reads it --
    # the same order backend/app.py builds.
    a.add_middleware(_SecurityHeaders)
    a.include_router(login_mod.router)
    return TestClient(a)


def _nonce(resp) -> str:
    policy = resp.headers["content-security-policy"]
    match = re.search(r"script-src ([^;]+);", policy)
    assert match, policy
    return match.group(1).split("'nonce-")[1].rstrip("'")


def test_the_signin_page_carries_a_marked_up_script(page):
    """The marker is an inline script, so script-src admits it by the
    response's per-response nonce -- the same token the page's style
    block already carries, and the only one script-src allows."""
    resp = page.get("/login")
    assert f'<script nonce="{_nonce(resp)}">' in resp.text, (
        "the sign-in page's inline script carries no nonce, or a "
        "different one from the header's: script-src has no "
        "'unsafe-inline', so the marker would never run")
    assert "'unsafe-inline'" not in resp.headers["content-security-policy"]


def test_the_signin_page_has_no_nonce_less_script(page):
    """One inline script without the nonce would be blocked, and the
    marker would silently never be written."""
    resp = page.get("/login")
    nonce = _nonce(resp)
    scripts = re.findall(r"<script\b[^>]*>", resp.text)
    assert scripts, ("the sign-in page no longer carries any script -- "
                     "the start marker cannot be there")
    assert all(f'nonce="{nonce}"' in s for s in scripts), scripts


def test_the_marker_names_the_two_keys_the_client_adopts(page):
    """Both halves are load-bearing. Without the elapsed time there is
    no start; without the origin, the elapsed time is a number on a
    timeline unrelated to the next document's."""
    body = page.get("/login").text
    assert START_KEY in body, (
        f"the marker never writes {START_KEY}")
    assert ORIGIN_KEY in body, (
        f"the marker never writes {ORIGIN_KEY}")


def test_the_marker_stamps_the_submit_not_the_load(page):
    """A stamp taken at page load measures the sign-in form's dwell time
    too -- however long the user spent typing. The journey is submit ->
    signed-in page."""
    body = page.get("/login").text
    assert "'submit'" in body, (
        "the marker does not bind on the form's submit event")
    assert "'click'" not in body, (
        "the marker binds on a click, so a submit that never navigates "
        "(a validation failure) would stamp a journey that never ended")


def test_the_marker_stamps_the_login_pages_own_time_origin(page):
    """`performance.now()` is relative to THIS document's origin. The
    signed-in page's own `performance.now()` starts near zero again, so a
    raw elapsed value cannot be compared across the redirect. The origin
    is what makes the two comparable."""
    body = page.get("/login").text
    assert "performance.timeOrigin" in body, (
        "the marker records the elapsed time without the time origin it "
        "is relative to, so the client cannot bridge the redirect")
    assert "performance.now()" in body


def test_a_browser_without_session_storage_still_submits(page):
    """Private browsing and blocked storage both make sessionStorage
    throw on write. A telemetry marker must not be the thing that stops a
    sign-in."""
    body = page.get("/login").text
    assert re.search(r"try\s*\{[^}]*sessionStorage", body), (
        "the marker writes to sessionStorage unguarded")
    assert "catch" in body, "the marker has no catch around the write"


def test_every_rendered_signin_page_carries_the_marker(page):
    """A failed sign-in re-renders the full page (issue #395), and the
    user submits again from it. A marker present only on the first render
    would time the FIRST attempt's start against the SECOND attempt's
    render -- a number nobody measured."""
    bodies = [page.get("/login").text]
    wrong = page.post("/login", data={"user_id": "12345", "password": "x"},
                      headers=_ORIGIN)
    assert wrong.status_code == 401, wrong.status_code
    bodies.append(wrong.text)
    for body in bodies:
        assert START_KEY in body, (
            "a re-rendered sign-in page lost the start marker")
        assert "performance.now()" in body


def test_a_successful_signin_still_redirects(page):
    """The marker is an addition to the page; the bare 303 to `/` that
    the sign-in journey spans is untouched."""
    resp = page.post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        headers=_ORIGIN, follow_redirects=False)
    assert resp.status_code == 303, resp.status_code
    assert resp.headers["location"] == "/"
