"""Import first: seeds the child's rates."""
from __future__ import annotations

from types import SimpleNamespace

from backend import pricing_load
from tests.refresh_fixture_builders import SEED_JSON

setattr(pricing_load, "PRICING_JSON", SimpleNamespace(read_text=lambda encoding: SEED_JSON))
