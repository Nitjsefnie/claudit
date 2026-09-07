"""Zero-suppression for token-type panels.

A token type whose total is zero across the data in view is dropped from
the payload and from the `token_types` list, so the frontend renders no
panel for it. This is presentation-layer suppression, not data-layer
omission: the column exists, the value is stored, the rate is wired.

Not hypothetical, and not specific to another provider's format: this
same codebase is deployed as glmmeter over the `zai` bucket, where
cache_creation/eph5/eph1h are 0 across all 90,316 canonical records, so
Cache Create and the whole Prompt-Cache TTL Split render permanently
flat.
"""
from backend import api_dashboard as ad


def _entries(**totals):
    """Two buckets splitting each supplied total, so a per-entry rule and
    a per-response rule are distinguishable."""
    a = {k: v // 2 for k, v in totals.items()}
    b = {k: v - v // 2 for k, v in totals.items()}
    return [{"hour": "2026-05-07T10:00:00Z", **a},
            {"hour": "2026-05-07T11:00:00Z", **b}]


def test_zero_token_type_is_dropped_from_every_entry():
    entries = _entries(input_tokens=100, output_tokens=50,
                       cache_5m_tokens=0, cache_1h_tokens=0,
                       cache_read_tokens=900)
    ad.drop_zero_token_types(entries)
    for e in entries:
        assert "cache_5m_tokens" not in e
        assert "cache_1h_tokens" not in e
        assert "input_tokens" in e and "cache_read_tokens" in e


def test_nonzero_token_type_survives_even_when_a_bucket_is_zero():
    """Suppression is decided per RESPONSE over the summed totals, not
    per entry — otherwise a series flickers in and out between buckets."""
    entries = [{"hour": "h1", "input_tokens": 0, "output_tokens": 5},
               {"hour": "h2", "input_tokens": 7, "output_tokens": 5}]
    ad.drop_zero_token_types(entries)
    assert all("input_tokens" in e for e in entries)


def test_survivors_are_listed_in_declared_render_order():
    entries = _entries(input_tokens=100, output_tokens=50,
                       cache_5m_tokens=0, cache_1h_tokens=0,
                       cache_read_tokens=900)
    ad.drop_zero_token_types(entries)
    assert ad.surviving_token_types(entries) == [
        "input_tokens", "output_tokens", "cache_read_tokens"]


def test_all_zero_leaves_no_token_types():
    entries = _entries(input_tokens=0, output_tokens=0, cache_5m_tokens=0,
                       cache_1h_tokens=0, cache_read_tokens=0)
    ad.drop_zero_token_types(entries)
    assert ad.surviving_token_types(entries) == []
