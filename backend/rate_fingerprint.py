"""The per-(model, provider) rate fingerprint (issue #351).

A reprice whose PRICING_VERSION bump moved no stored pair's rate data
restamps those rows without recomputing them: the pass compares each
stale row's stored ``records.rate_fingerprint`` against the CURRENT
fingerprint of its pair and lets SQL restamp the rows that agree, so
the read-and-recompute half of the pass costs O(changed pairs). The
soundness contract — equal fingerprints imply equal prices for every
(tokens, ts, long_context) on the pair — holds because the digest
covers every input pricing.resolve() consults: the rate tables'
consulted rows, the resolution's outcome (fold, key, tier, default)
and the pricing modules' source, so an edited entry, a correction, a
schedule change or a logic change all change it. All ts-dependence
lives in the windows/schedules/start fields; rows of a pair with equal
fingerprints price identically at every timestamp.

The structure is versioned by ``_STRUCTURE_VERSION``: changing its
shape bumps it, which invalidates every stored fingerprint at once
(conservative recompute) without touching PRICING_VERSION.

Every table is read through the ``pricing`` module attribute namespace
at CALL time — ``pricing.MODEL_RATES``, never a from-import local — so
the tests can patch tables exactly as they patch them for resolve().
"""
from __future__ import annotations

import hashlib
import inspect
import json
from datetime import datetime

from backend import long_context, pricing, pricing_load
from backend.pricing_load import RATE_FIELDS

# The digest's own version: bump when the structure's shape changes.
_STRUCTURE_VERSION = 1

# sha256 over the three pricing modules' source, concatenated in this
# order, computed once at import: any change to resolution or load
# logic invalidates every stored fingerprint.
_LOGIC_MODULES = (pricing, pricing_load, long_context)
_LOGIC = hashlib.sha256(
    "".join(inspect.getsource(m) for m in _LOGIC_MODULES).encode("utf-8")
).hexdigest()

# Memo per (raw model, provider). The rate tables load once at import
# and are immutable at runtime, so the memo cannot rot in production;
# tests patch tables between calls and go through
# clear_fingerprint_cache().
_FP_CACHE: dict[tuple[str, str | None], str] = {}


def clear_fingerprint_cache() -> None:
    """Empty the per-pair memo (tests patch the rate tables)."""
    _FP_CACHE.clear()


def _rates(rates: dict | None) -> list[float] | None:
    """A rates dict as a list in RATE_FIELDS order, or None."""
    return None if rates is None else [rates[f] for f in RATE_FIELDS]


def _iso(stamp: datetime | None) -> str | None:
    return None if stamp is None else stamp.isoformat()


def _windows(windows: pricing_load.Windows | None) -> list[list]:
    return [[_iso(end), _rates(rates)] for end, rates in windows or []]


def _schedule(
        schedule: list[pricing_load.ScheduleWindow] | None) -> list[list] | None:
    """Schedule windows as [sorted(days) | None, start, end, rates]."""
    if schedule is None:
        return None
    return [[sorted(days) if days is not None else None, start, end,
             _rates(rates)] for days, start, end, rates in schedule]


def _model_doc(norm: str) -> dict:
    """The model-alone resolution branch: rows before a provider row's
    start (or with no provider) resolve here, so both branches are
    covered."""
    # This module mirrors resolve() member-for-member, so the
    # private resolvers it fingerprints are its direct interface.
    key = pricing._match_key(norm)  # pylint: disable=protected-access
    if key is None:
        return {"key": None, "list": None, "windows": []}
    return {
        "key": key,
        "list": _rates(pricing.MODEL_RATES.get(key)),
        "windows": _windows(pricing.DATED_RATES.get(key)),
    }


def _tier(norm: str) -> list | None:
    """The FIRST matching family fallback's index and rates, when the
    id matches no table key."""
    if pricing._match_key(norm) is not None:  # pylint: disable=protected-access
        return None
    for index, (pattern, rates) in enumerate(
            pricing._TIER_FALLBACKS):  # pylint: disable=protected-access
        if pattern.search(norm):
            return [index, _rates(rates)]
    return None


def _provider_doc(norm: str, provider: str) -> dict:
    """The provider-row branch: the folded key, its start, list price,
    dated windows and the schedule riding each window's history entry."""
    fold = pricing._provider_key(norm, provider, None)  # pylint: disable=protected-access
    schedules: dict = {}
    start = None
    list_rates = None
    windows: list[tuple[datetime, dict]] = []
    if fold is not None:
        schedules = pricing.PROVIDER_SCHEDULES.get(fold, {})
        start = pricing.PROVIDER_STARTS.get(fold)
        list_rates = pricing.PROVIDER_RATES.get(fold)
        windows = pricing.PROVIDER_DATED_RATES.get(fold, [])
    return {
        "fold": None if fold is None else [fold[0], fold[1]],
        "start": _iso(start),
        "list": _rates(list_rates),
        "windows": [
            [_iso(end), _rates(rates), _schedule(schedules.get(index))]
            for index, (end, rates) in enumerate(windows)
        ],
        # resolve() consults the schedule riding the NEWEST history
        # entry (index == len(windows)) once every dated window has
        # ended — on a row with no windows, always. The per-window
        # entries above cannot carry it, so the fp names it here: a
        # whole-week schedule on a one-entry row must move the fp.
        "tail": _schedule(schedules.get(len(windows))),
    }


def _document(model: str, provider: str | None) -> dict:
    """The fingerprinted structure, exactly the inputs resolve()
    consults for this pair."""
    norm = pricing._normalise(model)  # pylint: disable=protected-access
    doc: dict = {
        "v": _STRUCTURE_VERSION,
        "logic": _LOGIC,
        "norm": norm,
        "free": pricing._is_free(model, norm),  # pylint: disable=protected-access
    }
    if doc["free"]:
        # A free id prices at zero without consulting any table, so its
        # fingerprint names no table data at all.
        return doc
    if provider:
        doc["provider"] = _provider_doc(norm, provider)
    doc["model"] = _model_doc(norm)
    doc["tier"] = _tier(norm)
    doc["default"] = _rates(pricing.DEFAULT_RATES)
    return doc


def pair_fingerprint(model: str, provider: str | None) -> str:
    """The current rate fingerprint of one (model, provider) pair.

    A stable sha256 hex digest over the canonical JSON of exactly the
    rate data pricing.resolve() consults for the pair (see the module
    docstring for the soundness contract). Memoized per pair.
    """
    cache_key = (model, provider)
    try:
        return _FP_CACHE[cache_key]
    except KeyError:
        pass
    digest = hashlib.sha256(json.dumps(
        _document(model, provider), sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    _FP_CACHE[cache_key] = digest
    return digest
