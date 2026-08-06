"""Login leaves a revocable session row; activity slides the cookie."""

# pylint: disable=import-outside-toplevel
# The deferred backend.* import mirrors the conftest.py guard: no backend
# module may be imported before conftest's os.environ setdefaults land.


def test_login_records_a_session_row(logged_in_client, auth_db):
    from backend import session as session_mod, sessions_repo
    cookie = logged_in_client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    parsed = session_mod.parse_session_token(cookie)
    assert parsed is not None
    nonce = parsed[2]
    assert sessions_repo.is_session_active(nonce) is True


def test_authenticated_request_refreshes_the_cookie(logged_in_client, auth_db):
    from backend import session as session_mod
    before = logged_in_client.cookies.get(session_mod.SESSION_COOKIE_NAME)
    resp = logged_in_client.get("/api/me")
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    # The SAME cookie value is re-issued — a re-mint here would mint a new
    # nonce per request, write a web_sessions row each time, and break the
    # session list.
    assert f"{session_mod.SESSION_COOKIE_NAME}={before}" in set_cookie
    # Flags must match the login sites' set_cookie calls.
    assert f"Max-Age={session_mod.SESSION_COOKIE_MAX_AGE}" in set_cookie


def test_guest_request_does_not_refresh_the_cookie(guest_client, auth_db):
    from backend import session as session_mod
    resp = guest_client.get("/api/me")
    assert resp.status_code == 200
    assert session_mod.SESSION_COOKIE_NAME not in resp.headers.get("set-cookie", "")


def test_admin_path_does_not_slide_the_cookie(logged_in_client, auth_db):
    from backend import session as session_mod
    # /admin/* authenticates by token, not session: _session_denied never
    # runs there, so no user_id is resolved and the slide must not fire —
    # even though a session cookie rides along on the request.
    resp = logged_in_client.post(
        "/admin/nope",
        headers={"X-Admin-Token": "test-admin", "origin": "http://testserver"},
    )
    assert resp.status_code == 404
    assert session_mod.SESSION_COOKIE_NAME not in resp.headers.get("set-cookie", "")
