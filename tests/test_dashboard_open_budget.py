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
the raw response bytes), so neither can regress unnoticed. It also pins
the panel-side half of the fix — the traces fetch handing `setTraces` to
`window.deferred.run` — at the source level, since node cannot parse the
JSX and nothing in the suite renders React.
"""
from __future__ import annotations

import contextlib
import os
import re
from contextlib import closing
from pathlib import Path

import psycopg

# Reuses the API suite's fresh-DB + mini-R2 fixtures rather than standing
# up a second one; having the function objects in this module's globals is
# what registers them here. Imported under their private function names —
# the registered names stay "app_with_data" / "app_with_fresh_data" — and
# listed in __all__ so the re-export is explicit, not an unused import.
from test_api import _app_with_data_fixture, _app_with_fresh_data_fixture

from backend import cache, db

__all__ = ["_app_with_data_fixture", "_app_with_fresh_data_fixture"]

_ROOT = Path(__file__).resolve().parents[1]
_EXTRA_JSX = _ROOT / "src" / "dashboard-charts-extra.jsx"


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


def _log_statements(monkeypatch):
    """Record every SQL statement the read endpoints run, per request.

    A plain helper, not a fixture: `db.viz_conn` is the single seam every
    panel query goes through, but the proxy only LOGS — it hands no test a
    database — so registering it in db_marker.DB_FIXTURES would over-mark
    (every fixture there creates a database). tests/test_db_marker.py
    roots fixtures by their source, so a plain helper roots nothing.

    The proxy logs the SQL text and delegates. Patching the `db` name as
    seen by api_dashboard and api_ctx_growth too is belt-and-braces (they
    are the same module object), so a future refactor to a module-level
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


def test_dashboard_runs_no_context_trace_query(app_with_data, monkeypatch):
    """Opening the dashboard must not run the panel's trace query.

    `file_models` is the trace SQL's distinctive CTE (a records scan
    grouped per file); its presence means the full per-file trace query
    ran — exactly what the ctx_at_end fold no longer needs (issue #838).
    """
    statement_log = _log_statements(monkeypatch)
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
        app_with_data, monkeypatch):
    """A dashboard open plus a panel open runs the trace query ONCE — the
    panel's own fetch. The dashboard contributes none (issue #838)."""
    statement_log = _log_statements(monkeypatch)
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


def _main_file_with_records() -> tuple[str, str]:
    """A main file that also has usage records, so its session lands in
    the dashboard's sessions panel (which is folded from usage_rollup)."""
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn, \
            conn.cursor() as cur:
        cur.execute(
            "SELECT f.file_key, f.session_id FROM files f "
            "WHERE f.is_main "
            "  AND EXISTS (SELECT 1 FROM records r "
            "              WHERE r.file_key = f.file_key) "
            "ORDER BY f.file_key LIMIT 1")
        row = cur.fetchone()
        assert row is not None, "fixture has no main file with records"
        return row


def _seed_ctx_turn_inputs(file_key: str, inputs: list[int]) -> None:
    """Rewrite one file's ctx_turns to `inputs`, in order."""
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn, \
            conn.cursor() as cur:
        cur.execute(
            "UPDATE files SET ctx_turns = ("
            "  SELECT jsonb_agg(jsonb_build_object("
            "    'idx', i, 'ts', '', 'line', i, 'input', arr.v,"
            "    'output', 5, 'delta', 0) ORDER BY i) "
            "  FROM generate_series(1, %s) AS i "
            "  CROSS JOIN LATERAL (SELECT (%s::int[])[i] AS v) arr"
            ") WHERE file_key = %s",
            (len(inputs), inputs, file_key),
        )
        conn.commit()


def test_ctx_at_end_is_the_last_turn_not_the_first(app_with_fresh_data):
    """ctx_at_end must read the LAST ctx turn, not the first.

    Every file in fixtures/r2_mini carries exactly ONE ctx turn, so
    `ctx_turns -> -1` and `-> 0` are indistinguishable there: a future
    edit to `-> 0` would silently make every ctx_at_end the FIRST turn.
    Seed a multi-turn trace whose first and last inputs differ, and pin
    that the dashboard reports the last one (issue #838).
    """
    file_key, sid = _main_file_with_records()
    _seed_ctx_turn_inputs(file_key, [11, 22, 333])
    cache.response_cache.clear()
    body = app_with_fresh_data.get("/api/dashboard?range=all").json()
    entries = {s["session_id"]: s for s in body["sessions"]}
    assert sid in entries, "the seeded session is absent from the payload"
    assert entries[sid]["ctx_at_end"] == 333, (
        "ctx_at_end is not the LAST ctx turn's input (333); the jsonb "
        "projection may have become `-> 0`")
    assert entries[sid]["ctx_at_end"] != 11, (
        "ctx_at_end reported the FIRST ctx turn's input")


def _strip_line_comments(src: str) -> str:
    """Drop `//` line comments so prose about the routing is not read as
    the routing itself (the lookbehind spares `https://`)."""
    return re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)


def test_traces_fetch_routes_setTraces_through_the_deferred_helper():
    """The Context Growth panel's traces fetch must hand `setTraces` to
    `window.deferred.run`, not call it in the commit that lands the fetch
    (issue #838).

    This is a source-level pin on purpose: node cannot parse the JSX and
    nothing in the suite renders React, so the deferral is invisible to
    every behavioural test — the routing can be deleted and nothing else
    fails.
    """
    src = _strip_line_comments(_EXTRA_JSX.read_text(encoding="utf-8"))
    assert re.search(
        r"window\.deferred\.run\(\s*b\.traces\s*\|\|\s*\[\]\s*,\s*setTraces\s*\)",
        src), (
        "the traces fetch no longer routes setTraces through "
        "window.deferred.run (issue #838)")
    assert not re.search(r"setTraces\(\s*b\.traces\s*\|\|\s*\[\]\s*\)", src), (
        "the traces fetch calls setTraces directly again, undoing the "
        "deferral (issue #838)")
