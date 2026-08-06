"""web_sessions repository — record, lookup, revoke.

Uses the shared `auth_db` fixture from tests/conftest.py.
"""


def test_record_then_active(auth_db):
    from backend import sessions_repo
    sessions_repo.record_session(7, "nonce-a", "curl/8", "10.0.0.1")
    assert sessions_repo.is_session_active("nonce-a") is True


def test_unknown_nonce_is_not_active(auth_db):
    from backend import sessions_repo
    assert sessions_repo.is_session_active("never-issued") is False


def test_revoked_nonce_is_not_active(auth_db):
    from backend import sessions_repo
    sessions_repo.record_session(7, "nonce-b", "curl/8", "10.0.0.1")
    assert sessions_repo.revoke_session(7, "nonce-b") is True
    assert sessions_repo.is_session_active("nonce-b") is False


def test_revoke_only_affects_own_user(auth_db):
    from backend import sessions_repo
    sessions_repo.record_session(7, "nonce-c", "curl/8", "10.0.0.1")
    assert sessions_repo.revoke_session(8, "nonce-c") is False
    assert sessions_repo.is_session_active("nonce-c") is True


def test_list_excludes_revoked(auth_db):
    from backend import sessions_repo
    sessions_repo.record_session(9, "nonce-d", "firefox", "10.0.0.2")
    sessions_repo.record_session(9, "nonce-e", "chrome", "10.0.0.3")
    sessions_repo.revoke_session(9, "nonce-e")
    rows = sessions_repo.list_sessions(9)
    nonces = {r["nonce"] for r in rows}
    assert nonces == {"nonce-d"}
    assert rows[0]["user_agent"] == "firefox"
