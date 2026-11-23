"""CLAUDIT_DEBUG_POOL_RESET: name who left a viz-pool connection non-idle."""
from __future__ import annotations

import inspect
import logging
import threading
import traceback
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg.pq import TransactionStatus

from backend.env_config import getenv

log = logging.getLogger("claudit.db")


class TracedConnection(psycopg.Connection):
    moved: tuple[str, str, list[traceback.FrameSummary]] = ("none recorded", "", [])

    def wait(self, gen: Any, *args: Any, **kwargs: Any) -> Any:
        # Undocumented psycopg internals: wait() and Cursor.execute's `query`; the test pins both.
        if self.pgconn.transaction_status == TransactionStatus.IDLE:
            frame = inspect.currentframe()
            caller = frame.f_back if frame else None
            query = caller.f_locals.get("query") if caller else None
            self.moved = (threading.current_thread().name, str(query)[:300], traceback.extract_stack()[:-1])
        return super().wait(gen, *args, **kwargs)


class ResetWarning(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for conn in record.args if isinstance(record.args, tuple) else ():
            if isinstance(conn, TracedConnection):
                try:  # untested guard: a diagnostic must not break the reset
                    thread, statement, stack = conn.moved
                    log.warning(
                        "POOL_RESET status=%s conn=%r now thread=%s\n%s"
                        "  moved out of IDLE by thread=%s statement=%r\n%s",
                        TransactionStatus(conn.pgconn.transaction_status).name, conn,
                        threading.current_thread().name, "".join(traceback.format_stack()[:-1]),
                        thread, statement, "".join(traceback.format_list(stack)))
                except Exception:
                    log.exception("POOL_RESET diagnostic failed")
        return True


FILTER = ResetWarning()


def pool_options(check: Callable[[Any], None]) -> dict[str, Any]:
    """Off: `check=` alone; on: plus the traced class and filter."""
    if getenv("CLAUDIT_DEBUG_POOL_RESET", "").lower() in ("", "0", "false", "no"):
        return {"check": check}
    logging.getLogger("psycopg.pool").addFilter(FILTER)
    return {"check": check, "connection_class": TracedConnection}
