"""SQL for the web_sessions table in the auth database.

Kept separate from session.py so token logic stays free of query text.
"""
from __future__ import annotations

import logging

from psycopg import Connection

from backend import db

logger = logging.getLogger(__name__)


def record_session(user_id: int, nonce: str, user_agent: str, ip: str) -> None:
    """Insert the row that makes a freshly minted token revocable."""
    with db.auth_conn() as c:
        c.execute(
            "INSERT INTO web_sessions (nonce, user_id, user_agent, ip) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (nonce) DO NOTHING",
            (nonce, user_id, user_agent[:400], ip[:100]),
        )


def _is_active(c: Connection, nonce: str) -> bool:
    row = c.execute(
        "SELECT 1 FROM web_sessions "
        "WHERE nonce = %s AND revoked_at IS NULL",
        (nonce,),
    ).fetchone()
    return row is not None


def is_session_active(nonce: str, conn: Connection | None = None) -> bool:
    """True only for a nonce we issued and have not revoked.

    Pass an open conn to share one pool checkout with other lookups on
    the same request (the auth pool is small); omit it to take your own.
    """
    if conn is not None:
        return _is_active(conn, nonce)
    with db.auth_conn() as c:
        return _is_active(c, nonce)


def touch_session(nonce: str, min_interval_s: int = 300) -> None:
    """Bump last_seen_at, at most once per min_interval_s per session.

    Best-effort: last-seen is bookkeeping, and by the time this runs the
    session is already proven valid. A write-side failure (read-only
    replica after failover, lock/statement timeout, PoolTimeout) must
    never deny the request — log a warning and move on.
    """
    try:
        with db.auth_conn() as c:
            c.execute(
                "UPDATE web_sessions SET last_seen_at = now() "
                "WHERE nonce = %s AND revoked_at IS NULL "
                "AND last_seen_at < now() - make_interval(secs => %s)",
                (nonce, min_interval_s),
            )
    except Exception:
        logger.warning(
            "touch_session failed for nonce %s", nonce, exc_info=True
        )


def list_sessions(user_id: int) -> list[dict]:
    """Live sessions for one user, newest first."""
    with db.auth_conn() as c:
        rows = c.execute(
            "SELECT nonce, created_at, last_seen_at, user_agent, ip "
            "FROM web_sessions "
            "WHERE user_id = %s AND revoked_at IS NULL "
            "ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [
        {
            "nonce": r[0],
            "created_at": r[1].isoformat(),
            "last_seen_at": r[2].isoformat(),
            "user_agent": r[3],
            "ip": r[4],
        }
        for r in rows
    ]


def revoke_session(user_id: int, nonce: str) -> bool:
    """Revoke one session. False if it is not this user's, or already gone."""
    with db.auth_conn() as c:
        row = c.execute(
            "UPDATE web_sessions SET revoked_at = now() "
            "WHERE nonce = %s AND user_id = %s AND revoked_at IS NULL "
            "RETURNING nonce",
            (nonce, user_id),
        ).fetchone()
    return row is not None
