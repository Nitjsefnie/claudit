"""A successful login leaves a revocable session row."""

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
    resp = logged_in_client.get("/api/me")
    assert resp.status_code == 200
    assert session_mod.SESSION_COOKIE_NAME in resp.headers.get("set-cookie", "")
