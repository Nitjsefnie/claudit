"""Guest-login configuration through the real routes and auth middleware."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.guest_login import guest_login_enabled
from backend import session


@pytest.fixture(name="client")
def _client_fixture(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Skip lifespan, as page-shell tests do, so no ingest or DB is needed."""
    monkeypatch.delenv("GUEST_LOGIN", raising=False)
    return TestClient(app)


@pytest.mark.parametrize("value, enabled", [
    (None, True), ("", True), (" \t ", True),
    ("1", True), ("true", True), ("yes", True), ("on", True),
    (" TRUE ", True), (" YeS ", True), (" ON ", True), (" 1 ", True),
    ("0", False), ("false", False), ("no", False), ("off", False),
    (" OFF ", False), ("disabled", False), ("nope", False),
])
def test_guest_login_setting(
    monkeypatch: pytest.MonkeyPatch, value: str | None, enabled: bool
) -> None:
    if value is None:
        monkeypatch.delenv("GUEST_LOGIN", raising=False)
    else:
        monkeypatch.setenv("GUEST_LOGIN", value)
    assert guest_login_enabled() is enabled


def test_sign_in_guest_controls_follow_setting(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = client.get("/login")
    assert default.status_code == 200
    assert 'formaction="/login/guest"' in default.text
    assert "Continue as guest" in default.text
    assert '<div class="or">or</div>' in default.text

    monkeypatch.setenv("GUEST_LOGIN", "0")
    disabled = client.get("/login")
    assert disabled.status_code == 200
    assert 'formaction="/login/guest"' not in disabled.text
    assert "Continue as guest" not in disabled.text
    assert '<div class="or">or</div>' not in disabled.text
    assert '<form method="post" action="/login">' in disabled.text
    assert '<button type="submit">Sign in</button>' in disabled.text


def test_guest_route_disabled_returns_404_without_cookie(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = {"Origin": "http://testserver"}
    default = client.post("/login/guest", headers=headers, follow_redirects=False)
    assert default.status_code == 303
    assert session.SESSION_COOKIE_NAME in default.cookies
    assert "session=" in default.headers["set-cookie"]

    monkeypatch.setenv("GUEST_LOGIN", "0")
    disabled = client.post("/login/guest", headers=headers, follow_redirects=False)
    assert disabled.status_code == 404
    assert "set-cookie" not in disabled.headers


def test_disabled_guest_route_never_mints_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GUEST_LOGIN", "0")
    monkeypatch.setattr(
        session, "make_guest_session_token",
        lambda: pytest.fail("disabled guest login must not mint a token"),
    )
    response = client.post(
        "/login/guest", headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 404
    assert "set-cookie" not in response.headers


def test_guest_cookie_refused_by_middleware_when_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    anonymous = client.get("/api/me", follow_redirects=False)
    assert anonymous.status_code == 401
    client.cookies.set(session.SESSION_COOKIE_NAME, session.make_guest_session_token())
    enabled = client.get("/api/me")
    assert enabled.status_code == 200
    assert enabled.json() == {"user_id": 0, "is_guest": True}

    monkeypatch.setenv("GUEST_LOGIN", "0")
    disabled = client.get("/api/me", follow_redirects=False)
    assert disabled.status_code == anonymous.status_code
    assert disabled.content == anonymous.content


def test_disabled_guest_cookie_never_bootstraps_guest_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    anonymous = client.get("/", follow_redirects=False)
    assert anonymous.status_code == 302
    assert anonymous.headers["location"] == "/login"
    client.cookies.set(session.SESSION_COOKIE_NAME, session.make_guest_session_token())
    enabled = client.get("/", follow_redirects=False)
    assert enabled.status_code == 200
    assert "window.IS_GUEST = true" in enabled.text

    monkeypatch.setenv("GUEST_LOGIN", "0")
    disabled = client.get("/", follow_redirects=False)
    assert "window.IS_GUEST = true" not in disabled.text
    assert disabled.status_code == anonymous.status_code
    assert disabled.headers["location"] == anonymous.headers["location"]
