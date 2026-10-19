"""Vendor context-length refresh tests with synthetic endpoint data."""
from __future__ import annotations

import pytest

from tests.vendor_rate_refresh_helpers import (
    GPT_ID, GPT_KEY, TRACKED, _catalog, _doc, _endpoint, _payload, _price, _run,
)


def test_vendor_context_updates_existing_metadata_without_rewriting_identity():
    context_length = 1048576
    existing = {**TRACKED, 'id': 'openai/retained-id', 'vendor_host': 'Kept',
                'context_length': 131072}
    endpoints = [_endpoint('openai', _price(1, 5)),
                 _endpoint('openai/fast', _price(2, 10))]
    for endpoint in endpoints:
        endpoint['context_length'] = context_length
    doc, out = _run(_doc(tracked={GPT_KEY: existing}), _catalog(GPT_ID),
                    {GPT_ID: _payload(*endpoints)})
    assert not out.refusals and out.moves
    assert doc['openrouter']['models'][GPT_KEY] == {
        **existing, 'context_length': context_length}


def test_vendor_context_disagreement_keeps_whole_tracked_entry_untouched():
    existing = {**TRACKED, 'context_length': 131072}
    endpoints = [_endpoint('openai', _price(1, 5)),
                 _endpoint('openai/fast', _price(2, 10))]
    endpoints[1]['context_length'] = 1048576
    doc, out = _run(_doc(tracked={GPT_KEY: existing}), _catalog(GPT_ID),
                    {GPT_ID: _payload(*endpoints)})
    assert not out.moves
    assert doc['openrouter']['models'][GPT_KEY] == existing
    assert any('context_length' in line for line in [*out.notices, *out.refusals])


@pytest.mark.parametrize('bad', [None, True, 0, -1, 1.5, '786432', 9007199254740992])
def test_invalid_vendor_context_refuses_without_touching_tracked_state(bad):
    endpoint = _endpoint('openai', _price(1, 5))
    if bad is None:
        endpoint.pop('context_length')
    else:
        endpoint['context_length'] = bad
    before = _doc(tracked={GPT_KEY: dict(TRACKED)}, members=[GPT_KEY])
    after, outcome = _run(before, _catalog(GPT_ID), {GPT_ID: _payload(endpoint)})
    assert after == before and not outcome.moves
    assert len(outcome.refusals) == 1 and 'context_length' in outcome.refusals[0]


def test_overbound_context_on_unselected_vendor_endpoint_refuses():
    endpoints = [_endpoint('openai', _price(1, 5)),
                 _endpoint('openai/fast', _price(2, 10))]
    endpoints[1]['context_length'] = 9007199254740992
    before = _doc(tracked={GPT_KEY: dict(TRACKED)}, members=[GPT_KEY])
    after, outcome = _run(before, _catalog(GPT_ID), {GPT_ID: _payload(*endpoints)})
    assert after == before and not outcome.moves
    assert len(outcome.refusals) == 1 and 'context_length' in outcome.refusals[0]


def test_no_vendor_endpoint_preserves_existing_context_and_membership():
    before = _doc(tracked={GPT_KEY: dict(TRACKED)}, members=[GPT_KEY])
    after, outcome = _run(before, _catalog(GPT_ID), {
        GPT_ID: _payload(_endpoint('third-party', _price(1, 5)))})
    assert after == before and not outcome.moves and not outcome.refusals
    assert any('no first-party endpoint' in line for line in outcome.notices)
