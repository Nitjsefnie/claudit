"""Context-size bucketing for the Cost-by-Context panel.

Pure-function tests only: the bucket edges are baked into stored
`ctx_cost_rollup` rows and cannot be re-derived at read time, so they
get their own guard. The DB-backed rebuild + SV-ROLLUP equality tests
live in test_ingest.py beside the other rollups', which is where the
schema fixtures are.
"""
from __future__ import annotations

import pytest

from backend import constants


def test_bucket_edges_are_uniform_and_cover_the_observed_range():
    """50k-wide buckets from 0 to 1M. Fixed width, not log: measured on
    the live corpus, cost is near-flat from 50k to 750k (7.8% peak at
    100-150k, still 5.2% at 700-750k), so log buckets would compress
    exactly the region carrying the money."""
    assert constants.CTX_BUCKET_WIDTH == 50_000
    assert constants.CTX_BUCKET_MAX == 1_000_000
    assert constants.CTX_BUCKET_MAX % constants.CTX_BUCKET_WIDTH == 0


@pytest.mark.parametrize("ctx,expected", [
    # Defensive: a negative bucket would render off the panel's left edge.
    pytest.param(-1, 0, id="negative-clamped-to-zero"),
    (0, 0),
    (1, 0),
    (49_999, 0),
    (50_000, 50_000),
    (137_402, 100_000),
    (999_999, 950_000),
    # Everything at or above the max folds into ONE open-ended overflow
    # bucket. The [1m] model variants really do exceed 1M, and a panel
    # that silently dropped them would under-report the most expensive
    # calls in the corpus.
    (1_000_000, 1_000_000),
    (1_048_576, 1_000_000),
    (99_000_000, 1_000_000),
])
def test_ctx_bucket_assignment(ctx, expected):
    assert constants.ctx_bucket(ctx) == expected
