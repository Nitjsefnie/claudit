"""Privacy-notice rendering of the login page (#270)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import login as login_mod
from backend import session as session_mod


@pytest.fixture(name="app")
def _app_fixture():
    """Build a fresh FastAPI app per test, with the auth DB mocked."""
    a = FastAPI()
    a.middleware("http")(session_mod.auth_middleware)
    a.include_router(login_mod.router)

    @a.get("/api/me")
    def me(request: Request):
        return {"user_id": request.state.user_id}

    return a


def test_login_page_without_notice_renders_no_privacy_link(app, monkeypatch):
    """#270: APP_PRIVACY_NOTICE_URL unset — no privacy-link anchor, no
    placeholder, and no leftover styling; the page is unchanged."""
    monkeypatch.delenv("APP_PRIVACY_NOTICE_URL", raising=False)
    body = TestClient(app).get("/login").text
    assert '<a class="privacy-link"' not in body
    assert "privacy-link" not in body


def test_login_page_renders_escaped_privacy_link(app, monkeypatch):
    """#270: APP_PRIVACY_NOTICE_URL set — the anchor renders near the
    guest button with the URL attribute-escaped exactly."""
    monkeypatch.setenv(
        "APP_PRIVACY_NOTICE_URL", 'https://ex.example/p?x=1&z="q"'
    )
    body = TestClient(app).get("/login").text
    assert (
        '<a class="privacy-link" '
        'href="https://ex.example/p?x=1&amp;z=&quot;q&quot;">'
        "Privacy notice</a>"
    ) in body


def test_login_page_privacy_link_label_is_present(app, monkeypatch):
    """#270: the rendered link reads exactly 'Privacy notice'."""
    monkeypatch.setenv("APP_PRIVACY_NOTICE_URL", "https://ex.example/privacy")
    body = TestClient(app).get("/login").text
    assert ">Privacy notice</a>" in body
