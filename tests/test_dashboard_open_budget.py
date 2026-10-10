"""The dashboard must not run the context-growth trace query (issue #838).

After PR #787 the dashboard payload stopped carrying the per-file ctx
traces — the Per-Session Context Growth panel fetches
/api/context-growth/traces itself — but /api/dashboard still ran the FULL
per-file trace query (api_ctx_growth.ctx_traces_rows, with its file_models
CTE over records) only to fold each session's ctx_at_end, and then threw
away every trace but the last turn. Nothing in the suite bounded the
dashboard's query count or its payload size, so the trace query could come
back — or the traces could quietly re-enter the payload — with no test
failing.

This pins both, from the outside (a statement-counting cursor proxy and
the raw response bytes), so neither can regress unnoticed.
"""
from __future__ import annotations

import contextlib
import os
from contextlib import closing

import psycopg
import pytest

# Reuses the API suite's fresh-DB + mini-R2 fixtures rather than standing
# up a second one; having the function objects in this module's globals is
# what registers them here. Imported under their private function names —
# the registered names stay "app_with_data" / "app_with_fresh_data" — and
# listed in __all__ so the re-export is explicit, not an unused import.
from test_api import _app_with_data_fixture, _app_with_fresh_data_fixture

from backend import cache, db

__all__ = ["_app_with_data_fixture", "_app_with_fresh_data_fixture"]


# The most statements one /api/dashboard?range=all may run. It is the
# endpoint's SHAPE, not a tuned number: 1 `SET LOCAL work_mem` + the eight
# panel queries (hourly, response_sizes, churn, total_sessions,
# cost_by_project, tokens_by_project, file_counts, sessions) + the
# ctx-end query + the rate-limit query. The trace query is deliberately
# NOT among them (issue #838). The count pin fails if a panel query is
# added without a thought; the file_models pin below fails specifically if
# the trace query comes back.
_DASHBOARD_STATEMENT_BUDGET = 11

# The payload byte budget for /api/dashboard?range=all on the mini
# fixture. Measured at ~5.4 KB bare; the seed in
# test_dashboard_payload_stays_within_budget grows the trace-bearing body
# past 9 KB, so 8 KB sits well clear of the real body and well below the
# body the traces would produce. The proof that it goes red when the
# traces return is recorded in the PR.
_PAYLOAD_BUDGET_BYTES = 8 * 1024


class _CountingCursor:
    """A cursor proxy that records every statement it is asked to run."""

    def __init__(self, cur, log):
        self._cur = cur
        self._log = log

    def execute(self, sql, *args, **kwargs):
        self._log.append(sql)
        return self._cur.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._cur, name)


@contextlib.contextmanager
def _counting_conn(real_ctx, log):
    with real_ctx as conn:
        yield _CountingCursor(conn, log)


@pytest.fixture(name="statement_log")
def _statement_log_fixture(monkeypatch):
    """Record every SQL statement the read endpoints run, per request.

    `db.viz_conn` is the single seam every panel query goes through; the
    proxy logs the SQL text and delegates. Patching the `db` name as seen
    by api_dashboard and api_ctx_growth too is belt-and-braces (they are
    the same module object), so a future refactor to a module-level
    ``from backend.db import viz_conn`` cannot slip past the count.
    """
    log: list[str] = []
    real = db.viz_conn

    def patched():
        return _counting_conn(real(), log)

    monkeypatch.setattr(db, "viz_conn", patched)
    monkeypatch.setattr("backend.api_dashboard.db.viz_conn", patched)
    monkeypatch.setattr("backend.api_ctx_growth.db.viz_conn", patched)
    return log


def test_dashboard_runs_no_context_trace_query(app_with_data, statement_log):
    """Opening the dashboard must not run the panel's trace query.

    `file_models` is the trace SQL's distinctive CTE (a records scan
    grouped per file); its presence means the full per-file trace query
    ran — exactly what the ctx_at_end fold no longer needs (issue #838).
    """
    cache.response_cache.clear()
    r = app_with_data.get("/api/dashboard?range=all")
    assert r.status_code == 200

    traces = [s for s in statement_log if "file_models" in s]
    assert not traces, (
        "the dashboard ran the context-growth trace query "
        f"({len(traces)} statement(s)); it needs only each main file's "
        "last turn (api_ctx_growth.ctx_end_rows)")
    assert len(statement_log) <= _DASHBOARD_STATEMENT_BUDGET, (
        f"the dashboard ran {len(statement_log)} statements, over the "
        f"budget of {_DASHBOARD_STATEMENT_BUDGET}")


def test_opening_the_dashboard_runs_the_trace_query_at_most_once(
        app_with_data, statement_log):
    """A dashboard open plus a panel open runs the trace query ONCE — the
    panel's own fetch. The dashboard contributes none (issue #838)."""
    cache.response_cache.clear()
    app_with_data.get("/api/dashboard?range=all")
    after_dashboard = len(statement_log)
    dash_traces = [s for s in statement_log[:after_dashboard]
                   if "file_models" in s]
    assert not dash_traces, "the dashboard ran the trace query"

    cache.response_cache.clear()
    r = app_with_data.get("/api/context-growth/traces?range=all")
    assert r.status_code == 200
    panel_traces = [s for s in statement_log[after_dashboard:]
                    if "file_models" in s]
    assert len(panel_traces) == 1, (
        "the panel endpoint must run the trace query exactly once, ran "
        f"{len(panel_traces)}")


def _seed_large_ctx_turns(turns: int = 400) -> None:
    """Rewrite one main file's ctx_turns to a large, realistic trace.

    The mini fixture's traces are tiny, so a payload budget alone would
    not notice them returning. This gives one file 400 turns carrying the
    same keys backend/ctx_turns.py writes, so the body the traces would
    add is unmistakably over budget.
    """
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn, \
            conn.cursor() as cur:
        cur.execute(
            "UPDATE files SET ctx_turns = ("
            "  SELECT jsonb_agg(jsonb_build_object("
            "    'idx', i, 'ts', '', 'line', i,"
            "    'input', i * 1000, 'output', 5, 'delta', 1000))"
            "  FROM generate_series(1, %s) AS i) "
            "WHERE file_key = (SELECT file_key FROM files "
            "                  WHERE is_main ORDER BY file_key LIMIT 1)",
            (turns,),
        )
        conn.commit()


def test_dashboard_payload_stays_within_budget(app_with_fresh_data):
    """The dashboard payload carries no ctx turns and stays inside budget.

    `"turns"` is the traces' per-file key; the dashboard's own
    `total_turns` scalar does not contain the quoted token, so its absence
    is exactly "the traces did not return". The byte budget is the second
    half: even a re-shaped trace payload cannot slip under it.
    """
    _seed_large_ctx_turns()
    cache.response_cache.clear()
    r = app_with_fresh_data.get("/api/dashboard?range=all")
    assert r.status_code == 200
    body = r.content
    assert b'"turns"' not in body, (
        "the dashboard payload carries per-file ctx turns again")
    assert len(body) < _PAYLOAD_BUDGET_BYTES, (
        f"dashboard payload is {len(body)} bytes, over the "
        f"{_PAYLOAD_BUDGET_BYTES}-byte budget")
