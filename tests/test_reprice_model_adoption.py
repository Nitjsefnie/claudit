"""An adopted replay's price must survive a reprice at unchanged rates."""
from __future__ import annotations

import pytest

# Register the existing private-DB fixture, as the reprice parity suite does.
from test_canonical_model_attribution import (  # pylint: disable=unused-import
    DIFFERENT_MODEL_FORK_FIXTURE,
    REPLAY_UUIDS,
    _fresh_db_fixture,
    _mirror,
    _switching_parent_rollout,
)

from backend import (constants, db, ingest, ingest_reprice, parse,
                     rate_fingerprint, uuid_split)


def _replay_price():
    """Read the losing first request in the reprice calculator's row shape."""
    with db.viz_conn() as c:
        rows = c.execute(
            f"""
            SELECT r.file_id, r.line_num, dm.value, r.fresh_tokens,
                   r.cache_creation_tokens, r.cache_read_tokens,
                   r.output_tokens, r.eph5_tokens, r.eph1h_tokens,
                   r.ts, rp.long_context, dp.value, rp.cost_usd,
                   pv.value, df.value, rp.request_fee_usd,
                   rp.long_context_input_mult, rp.long_context_output_mult,
                   r.web_search_requests, r.is_replay, r.is_canonical
              FROM records r JOIN files f USING (file_id)
              JOIN records_price rp USING (file_id, line_num)
              JOIN dim_model dm ON dm.id = r.model_id
              LEFT JOIN dim_provider dp ON dp.id = r.provider_id
              LEFT JOIN dim_pricing_version pv ON pv.id = rp.pricing_version_id
              LEFT JOIN dim_rate_fingerprint df ON df.id = rp.rate_fingerprint_id
             WHERE {uuid_split.uuid_text_sql('r')} = %s
               AND f.file_key LIKE '%%subagents/forkthread/wire.jsonl'
            """, (REPLAY_UUIDS[0],)).fetchall()
    assert len(rows) == 1, rows
    assert rows[0][-2:] == (True, False), "compare the losing replay itself"
    return ingest_reprice._StaleRow(*rows[0][:-2])  # pylint: disable=protected-access


@pytest.mark.parametrize("phase", ["current", "version-only-bump"])
@pytest.mark.parametrize("field", ["cost_usd", "rate_fingerprint"])
def test_reprice_preserves_fresh_ingests_adopted_replay_price(
        fresh_db, monkeypatch, tmp_path, phase, field):
    """Both current-row calculation and stale-row writes agree with fresh ingest.

    The real parser/persist/canonical pass supplies the reference; only the
    version changes to exercise stale repricing, never the rates or tokens.
    """
    parent_bytes = _switching_parent_rollout()
    _mirror(monkeypatch, tmp_path, DIFFERENT_MODEL_FORK_FIXTURE, parent_bytes)
    fork_key = "mini/sessions/toyproj/sessA/subagents/forkthread/wire.jsonl"
    parsed_fork = parse.parse_file(
        fork_key, DIFFERENT_MODEL_FORK_FIXTURE.read_bytes())["records"][0]
    parsed_parent = parse.parse_file(
        "mini/sessions/toyproj/sessA/wire.jsonl", parent_bytes)["records"][0]
    assert parsed_fork["uuid"] == parsed_parent["uuid"] == REPLAY_UUIDS[0]
    assert parsed_fork["model"] != parsed_parent["model"], (
        "the replay must need model adoption to exercise the price transition")
    assert 0 < parsed_fork["cost_usd"] != parsed_parent["cost_usd"] > 0, (
        "distinct nonzero model prices must make lost price provenance visible")

    assert ingest.run_ingest(trigger="manual")["error"] is None
    fresh = _replay_price()
    assert fresh.model == parsed_parent["model"]
    assert fresh.provider == parsed_fork.get("provider")
    assert fresh.pricing_version == constants.PRICING_VERSION
    assert fresh.fresh_tokens > 0 and fresh.output_tokens > 0
    assert fresh.cost_usd > 0 and fresh.rate_fingerprint

    if phase == "current":
        # The public pass skips current rows. Check its calculator on that
        # same stored row as well, so a skipped row cannot hide disagreement.
        assert ingest.reprice_stale() == 0
        actual = {
            "cost_usd": ingest_reprice._record_updates(fresh)["cost_usd"],  # pylint: disable=protected-access
            "rate_fingerprint": rate_fingerprint.pair_fingerprint(
                fresh.model, fresh.provider),
        }
    else:
        monkeypatch.setattr(constants, "PRICING_VERSION",
                            str(int(constants.PRICING_VERSION) + 1))
        ingest.reprice_stale()
        repriced = _replay_price()
        assert repriced[:12] == fresh[:12], "reprice must keep row identity and inputs"
        assert repriced.pricing_version == constants.PRICING_VERSION
        actual = {"cost_usd": float(repriced.cost_usd),
                  "rate_fingerprint": repriced.rate_fingerprint}

    expected = {"cost_usd": float(fresh.cost_usd),
                "rate_fingerprint": fresh.rate_fingerprint}
    assert actual[field] == expected[field], (
        f"{phase} reprice must preserve the fresh write's {field} for the "
        "same adopted replay at unchanged rates")
