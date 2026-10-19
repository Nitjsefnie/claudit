"""Token totals and thinking-token suppression across panel endpoints."""
import pytest

from test_api import (  # pylint: disable=unused-import
    _app_with_data_fixture,
    _app_with_fresh_data_fixture,
)

from backend import db, ingest


@pytest.mark.parametrize("endpoint", [
    pytest.param("/api/cost-by-agent?range=all", id="agent"),
    pytest.param("/api/cost-by-context?range=all", id="context"),
])
def test_cost_by_agent_total_tokens_matches_the_records_sum(app_with_data, endpoint):
    """Rollup total_tokens equals the sum over `records` (SV-ROLLUP)."""
    body = app_with_data.get(endpoint).json()
    with db.viz_conn() as c:
        expected = c.execute(
            "SELECT COALESCE(SUM(fresh_tokens + cache_creation_tokens "
            "+ cache_read_tokens + output_tokens), 0) FROM records "
            "WHERE is_canonical AND ts IS NOT NULL"
        ).fetchone()
    assert expected is not None
    assert body["total_tokens"] == int(expected[0])


def test_dashboard_reports_thinking_tokens_as_a_non_additive_type(app_with_data):
    """Thinking is a SUBSET of output_tokens, not a sixth slice of the
    billed partition — the API reports it so the dashboard can plot it,
    and it must never be summed into a total or priced. Both the rollup
    path and the 24h live path carry it, with the same numbers."""
    body = app_with_data.get("/api/dashboard?range=all").json()
    with db.viz_conn() as c:
        expected = c.execute(
            "SELECT COALESCE(SUM(thinking_tokens), 0), "
            "       COALESCE(SUM(output_tokens), 0) "
            "  FROM records WHERE is_canonical AND ts IS NOT NULL"
        ).fetchone()
    assert expected is not None
    think, out = int(expected[0]), int(expected[1])
    assert think > 0, "fixture must exercise thinking tokens"
    assert think <= out, "a subset cannot exceed its superset"
    assert sum(e.get("thinking_tokens", 0) for e in body["hourly"]) == think
    assert "thinking_tokens" in body["token_types"]
    # Declared AFTER output_tokens: it is a breakdown of that panel's
    # series, and the render order is the panel order.
    order = body["token_types"]
    assert order.index("thinking_tokens") == order.index("output_tokens") + 1


def test_thinking_tokens_are_suppressed_when_the_range_never_thought(
    app_with_fresh_data,
):
    """Same zero-suppression as every other token type: a corpus with no
    extended thinking gets no panel rather than a flat zero line."""
    with db.viz_conn() as c:
        c.execute("UPDATE records SET thinking_tokens = 0")
        c.commit()
    # usage_rollup is derived state: mutating `records` outside ingest
    # means rebuilding it, or the panel reads the stale pre-aggregate
    # (SV-ROLLUP).
    ingest.rebuild_rollup()
    body = app_with_fresh_data.get("/api/dashboard?range=all&fresh=1").json()
    assert "thinking_tokens" not in body["token_types"]
    for entry in body["hourly"]:
        assert "thinking_tokens" not in entry
