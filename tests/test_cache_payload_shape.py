"""Issue #916: /api/cache's per_model[] and per_model_provider[] must not
carry `cost_buckets` — no frontend file reads it there — while
session_total still does, since the cache view renders
session_total.cost_buckets (src/views/cache-view.jsx). Pinned here so the
strip cannot silently regress. Lives in its own module to keep test_api.py
under the test-size ceiling.
"""
from __future__ import annotations

from test_api import _app_with_fresh_data_fixture  # noqa: F401

__all__ = ["_app_with_fresh_data_fixture"]

_COST_BUCKETS = {"fresh", "create_5m", "create_1h", "read", "output",
                 "web_search"}


def test_cache_payload_omits_cost_buckets_outside_the_session_total(
        app_with_fresh_data):
    body = app_with_fresh_data.get("/api/cache?range=3650d").json()
    assert body["per_model"], "fixture produced no per-model rows"
    assert body["per_model_provider"], "fixture produced no per-provider rows"
    for entry in body["per_model"]:
        assert "cost_buckets" not in entry, entry
    for entry in body["per_model_provider"]:
        assert "cost_buckets" not in entry, entry
    # session_total keeps the decomposition, with a nonempty value.
    buckets = body["session_total"]["cost_buckets"]
    assert set(buckets) == _COST_BUCKETS
    assert sum(buckets.values()) > 0
