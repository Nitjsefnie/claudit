"""What each panel endpoint reports per token type.

Split out of test_api.py, whose module-scoped fixtures it borrows, once
that module crossed pylint's line budget. The through-line: a token type
is declared, summed and suppressed by the same rules wherever it appears
— except `thinking_tokens`, which is a SUBSET of output_tokens and must
never be added to a total or priced.
"""
from test_api import _app_with_data_fixture  # pylint: disable=unused-import

from backend import api_dashboard, db, ingest


def test_dashboard_payload_declares_its_token_types(app_with_data):
    """Endpoint contract for zero-suppression: whatever survives is
    declared in `token_types`, and the hourly entries carry exactly the
    declared fields — never a declared field that is missing, never a
    surviving field that went undeclared."""
    body = app_with_data.get("/api/dashboard?range=all").json()
    declared = body["token_types"]
    assert declared, "fixture produced no token types"
    assert declared == [f for f in api_dashboard.TOKEN_TYPE_FIELDS if f in declared], \
        "token_types must keep TOKEN_TYPES render order"
    for entry in body["hourly"]:
        present = {f for f in api_dashboard.TOKEN_TYPE_FIELDS if f in entry}
        assert present == set(declared)


def test_cost_by_agent_carries_total_tokens(app_with_data):
    """The panel charts tokens beside cost, so the endpoint reports the
    whole token tally per agent type — every tier, matching what Tokens
    by Model sums — not just output_tokens. Rollup and live path agree."""
    for rng in ("all", "1d"):
        body = app_with_data.get(f"/api/cost-by-agent?range={rng}").json()
        for a in body["agents"]:
            assert set(a) == {
                "agent_type", "requests", "output_tokens", "total_tokens",
                "cost_usd", "share",
            }
            assert a["total_tokens"] >= a["output_tokens"]
    rolled = app_with_data.get("/api/cost-by-agent?range=all").json()
    assert sum(a["total_tokens"] for a in rolled["agents"]) > 0
    assert rolled["total_tokens"] == sum(
        a["total_tokens"] for a in rolled["agents"]
    )


def test_cost_by_agent_total_tokens_matches_the_records_sum(app_with_data):
    """agent_rollup's new column is a pure sum, so it must equal the
    equivalent aggregate over `records` (SV-ROLLUP keeps that true)."""
    body = app_with_data.get("/api/cost-by-agent?range=all").json()
    with db.viz_conn() as c:
        expected = c.execute(
            "SELECT COALESCE(SUM(fresh_tokens + cache_creation_tokens "
            "+ cache_read_tokens + output_tokens), 0) FROM records "
            "WHERE is_canonical AND ts IS NOT NULL"
        ).fetchone()
    assert expected is not None
    assert body["total_tokens"] == int(expected[0])


def test_cost_by_context_carries_total_tokens(app_with_data):
    """The tokens variant of the panel reads the same endpoint, so each
    bucket reports the tokens processed there beside the dollars, and
    `cum_token_share` is the running fraction the tokens line plots."""
    for rng in ("all", "1d"):
        body = app_with_data.get(f"/api/cost-by-context?range={rng}").json()
        for b in body["buckets"]:
            assert set(b) == {
                "ctx_bucket", "requests", "cost_usd", "cum_share",
                "total_tokens", "cum_token_share",
            }
    body = app_with_data.get("/api/cost-by-context?range=all").json()
    assert body["total_tokens"] == sum(b["total_tokens"] for b in body["buckets"])
    assert body["total_tokens"] > 0
    shares = [b["cum_token_share"] for b in body["buckets"]]
    assert shares == sorted(shares), "a cumulative share cannot decrease"
    assert abs(shares[-1] - 1.0) < 1e-9


def test_cost_by_context_total_tokens_matches_the_records_sum(app_with_data):
    """ctx_cost_rollup's new column is a pure sum, so it must equal the
    equivalent aggregate over `records` (SV-ROLLUP keeps that true)."""
    body = app_with_data.get("/api/cost-by-context?range=all").json()
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


def test_thinking_tokens_are_suppressed_when_the_range_never_thought(app_with_data):
    """Same zero-suppression as every other token type: a corpus with no
    extended thinking gets no panel rather than a flat zero line."""
    with db.viz_conn() as c:
        c.execute("UPDATE records SET thinking_tokens = 0")
        c.commit()
    # usage_rollup is derived state: mutating `records` outside ingest
    # means rebuilding it, or the panel reads the stale pre-aggregate
    # (SV-ROLLUP).
    ingest.rebuild_rollup()
    body = app_with_data.get("/api/dashboard?range=all&fresh=1").json()
    assert "thinking_tokens" not in body["token_types"]
    for entry in body["hourly"]:
        assert "thinking_tokens" not in entry
