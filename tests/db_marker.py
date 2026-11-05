"""The mechanical db/portable test split.

A test is a DB test when its invocation hands it a PostgreSQL-backed
database. DB_FIXTURES is the registry of fixture names that do that;
``mark_db_items`` marks every collected test requesting any of them —
transitively, at no cost in the registry: pytest's ``item.fixturenames``
is the transitive fixture closure, so a test taking ``app_with_data``
(defined in test_api, re-exported by test_schema_autoapply and
test_api_token_types) marks through it without db_marker knowing that
fixture exists.

Tests that reach the server WITHOUT any DB fixture carry
``@pytest.mark.db`` explicitly. A forgotten registration leaves a DB test
in the portable CI matrix, where it fails on the missing server.
"""
from __future__ import annotations

from typing import Any, Iterable

import pytest

# Fixture names whose invocation hands the test a database (fixtures whose
# body, or a helper it calls, makes a scratch_db server call).
# app_with_data test_api / test_tokens_by_project; the fixture
# body calls a _build_api_client helper that
# creates the scratch DB
# app_with_prompt_range_data test_prompt_ts; creates its own scratch
# DB in-body ( fixture)
# dashboard_body test_breakdown_exact_rate; creates its own
# scratch DB in-body
# client test_provider_split_api; creates its own
# scratch DB in-body. test_web_metrics defines a
# second one over `viz` — the registry
# is by NAME, so both mark.
# api_client test_rate_boundary_fold_api; takes fresh_db,
# which creates the scratch DB
# _viz_ready test_db_schema_check; autouse, applies the app
# schema to its own scratch viz DB
# auth_env test_db_schema_check; creates its own empty
# scratch auth DB per case
# twin_app test_transcript_cache_key; creates its own
# scratch DB in-body ( fixture)
# server test_export_shutdown; provisions the export-shutdown
# server's viz + auth scratch databases
# viz test_web_metrics; a fresh schema as CLAUDIT_DATABASE_URL_VIZ,
# with no ingest
# gated_client test_web_metrics; the api router behind the real
# auth middleware, as a guest
# lat_client test_latency_read_canonical; creates its own
# scratch DB in-body
DB_FIXTURES = frozenset({
    "fresh_db",
    "drop_failing_pool",
    "redact_app",
    "codex_app",
    "sidecar_app",
    "app_with_data",
    "app_with_fresh_data",
    "app_with_rl_data",
    "app_with_prompt_range_data",
    "dashboard_body",
    "client",
    "api_client",
    "_viz_ready",
    "auth_env",
    "projects_client",
    "twin_app",
    "server",
    # See the `client` entry above for why it is not repeated.
    "viz",
    "gated_client",
    "lat_client",
    "cost_client",
    "parity_clients",
})


def mark_db_items(items: Iterable[Any]) -> None:
    """Add the `db` marker to every item requesting a DB fixture.

    ``item.fixturenames`` is pytest's TRANSITIVE fixture closure, so one
    set-membership test covers requested, requested-by-requested, and
    imported-from-another-module fixtures alike.
    """
    for item in items:
        if DB_FIXTURES.intersection(item.fixturenames):
            item.add_marker(pytest.mark.db)
