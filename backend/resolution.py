"""The resolver's outcome carrier. Split from backend/pricing.py — whose
module size is a ratcheted ceiling — a pure frozen dataclass: no rate
table is read here, and pricing re-exports the name so every
``pricing.Resolution`` reader keeps working."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving a model id to rates.

    kind: "exact" | "tier" | "default". Anything other than "exact" means
    the figure is an estimate and should be surfaced as such.
    """
    rates: dict
    kind: str
    key: str | None = None
    # True when the rates came from a weekly schedule's window or default
    # for this record's own time: a fold re-deriving cost at one
    # representative time cannot reproduce them (SV-RATE-DATA).
    scheduled: bool = False
    # The serving host's per-request fee in force (issue #469): USD this
    # one request costs beside its tokens, folded into compute_cost's
    # total and stored on records.request_fee_usd. Zero when the resolved
    # entry carries no fee note (every non-OpenRouter lane, every
    # unmodelled listing).
    request_fee: float = 0.0

    @property
    def estimated(self) -> bool:
        return self.kind != "exact"
