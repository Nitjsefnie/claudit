"""The mechanical db/portable test split (issue #27).

A test is a DB test when its invocation hands it a PostgreSQL-backed
database. DB_FIXTURES is the registry of fixture names that do that;
``mark_db_items`` marks every collected test requesting any of them —
transitively, at no cost in the registry: pytest's ``item.fixturenames``
is the transitive fixture closure, so a test taking ``app_with_data``
(defined in test_api, re-exported by test_schema_autoapply and
test_api_token_types) marks through it without db_marker knowing that
fixture exists.

Tests that reach the server WITHOUT any DB fixture carry
``@pytest.mark.db`` explicitly (test_scratch_db.py, test_smoke_cleanup.py,
one epoch-SQL test in test_cost_buckets.py). tests/test_db_marker.py
re-derives both from tests/*.py source and fails on drift in either
direction: a forgotten registration leaves a DB test in the portable CI
matrix, where it fails on the missing server — the fail-loud backstop is
running the suite with ``-m "not db"`` against no reachable server.
"""
from __future__ import annotations

from typing import Any, Iterable

import pytest

# Fixture names whose invocation hands the test a database. Derived from
# the repo, not hand-kept: tests/test_db_marker.py re-derives the set
# from tests/*.py source (fixtures whose body — or the body of a helper
# they call — mentions a scratch_db server call, closed transitively
# over fixture parameters and helper calls) and fails when it no longer
# equals this frozenset.
#
#   fresh_db             six modules define it — the canonical scratch-DB
#                        fixture (test_bucket_redact, -ingest,
#                        -multi_bucket, -parse_lanes, -long_context_fold,
#                        -project_case_fold)
#   redact_app           takes fresh_db (test_bucket_redact)
#   codex_app            takes fresh_db (test_long_context_fold)
#   sidecar_app          takes fresh_db (test_multi_bucket)
#   app_with_data        test_api / test_tokens_by_project; the fixture
#                        body calls a _build_api_client helper that
#                        creates the scratch DB
#   app_with_fresh_data  ditto, the function-scoped variant
#   app_with_rl_data     test_api; creates its own scratch DB in-body
#   app_with_prompt_range_data  test_prompt_ts; creates its own scratch
#                        DB in-body (issue #214 fixture)
#   dashboard_body       test_breakdown_exact_rate; creates its own
#                        scratch DB in-body
#   client               test_provider_split_api; creates its own
#                        scratch DB in-body. test_web_metrics defines a
#                        second one over `viz` (issue #436) — the registry
#                        is by NAME, so both mark.
#   api_client           test_rate_boundary_fold_api; takes fresh_db,
#                        which creates the scratch DB
#   _viz_ready           test_db_schema_check; autouse, applies the app
#                        schema to its own scratch viz DB
#   auth_env             test_db_schema_check; creates its own empty
#                        scratch auth DB per case
#   twin_app             test_transcript_cache_key; creates its own
#                        scratch DB in-body (issue #375 fixture)
#   server               test_export_shutdown; provisions the export-shutdown
#                        server's viz + auth scratch databases
#   viz                  test_web_metrics; a fresh schema as DATABASE_URL_VIZ,
#                        with no ingest (issue #436)
#   gated_client         test_web_metrics; the api router behind the real
#                        auth middleware, as a guest
DB_FIXTURES = frozenset({
    "fresh_db",
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
    # issue #436; see the `client` entry above for why it is not repeated.
    "viz",
    "gated_client",
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


# Tests the marking guard flags whose bodies provably never reach a
# server, each with the reason. Every entry names a test that EXISTS: an
# entry for a name no test carries excuses nothing, and reads as cover
# while the real offender is left to be found. Moved here from the guard
# test module so the registry and its allowlist live in one place (issue
# #715); the guard reads it as tests.db_marker.MARK_ALLOWLIST.
MARK_ALLOWLIST: dict[str, str] = {
    "test_version.py:test_health_error_branch_reports_version":
        "monkeypatches db.viz_conn with a function that raises, so the "
        "health endpoint's error branch runs with no server at all",
    "test_version.py:test_health_error_branch_answers_503":
        "monkeypatches db.viz_conn with a function that raises, so the "
        "health endpoint's error branch runs with no server at all",
    "test_version.py:test_health_ok_branch_reports_version":
        "monkeypatches db.viz_conn with a fake connection, so the "
        "health endpoint's ok branch runs with no server at all",
    "test_version.py:test_health_last_ingest_carries_newer":
        "monkeypatches db.viz_conn with a fake connection, so the "
        "health endpoint's ok branch runs with no server at all",
    "test_schema_autoapply.py:test_apply_schema_unlock_failure_does_not_mask_the_ddl_error":
        "monkeypatches db.viz_conn with a fake connection, so "
        "apply_schema's unlock guard runs with no server at all",
    "test_schema_autoapply.py:test_apply_schema_swallows_unlock_failure_after_success":
        "monkeypatches db.viz_conn with a fake connection, so "
        "apply_schema's unlock guard runs with no server at all",
}
