"""The per-(model, provider) rate fingerprint (issue #351).

A reprice whose PRICING_VERSION bump moved no stored pair's rate data
restamps those rows without recomputing them: the pass compares each
stale row's stored ``records.rate_fingerprint`` against the CURRENT
fingerprint of its pair and lets SQL restamp the rows that agree, so
the read-and-recompute half of the pass costs O(changed pairs). The
soundness contract — equal fingerprints imply equal prices for every
(tokens, web_search_requests, ts, long_context) on the pair — holds because
the digest covers every input pricing.resolve() consults: the rate tables'
consulted rows, the resolution's outcome (fold, key, tier, default), the
parser's source that produces stored search counts, the pricing modules'
source AND the reprice pass's own source, whose
``_record_updates`` is part of the derivation (the long-context
re-derivation rule and the unsplit arithmetic live there) — so an
edited entry, a correction, a schedule change, a logic change in the
pricing modules or in the pass itself all change it (issue #377).
All ts-dependence lives in the windows/schedules/start fields; rows of
a pair with equal fingerprints price identically at every timestamp.

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

from backend import (long_context, meter_tables, model_names, pricing,
                     pricing_load)
from backend.pricing_load import RATE_FIELDS

# The digest's own version: bump when the structure's shape changes.
_STRUCTURE_VERSION = 6

# Pricing modules are the stable imports; parser and reprice modules join
# them at first use in hashed_modules to keep their import edges acyclic.
_LOGIC_MODULES = (pricing, pricing_load, model_names, meter_tables,
                  long_context)

# The digest, computed on first use: by then parser and reprice imports
# have settled and getsource can read their modules.
_LOGIC_CACHE: list[str] = []


def hashed_modules() -> tuple:
    """The parser, pricing and reprice source hashed by the logic digest.

    Parser modules are imported at call time so this module stays outside
    their import graph; the reprice pass is lazy because it imports this
    module in the opposite direction.
    """
    # pylint: disable=import-outside-toplevel,cyclic-import
    from backend import ingest_reprice, parse, parse_codex, parse_common
    return (*_LOGIC_MODULES, parse, parse_common, parse_codex,
            ingest_reprice)


def _logic() -> str:
    if not _LOGIC_CACHE:
        _LOGIC_CACHE.append(hashlib.sha256("".join(
            inspect.getsource(m) for m in hashed_modules()
        ).encode("utf-8")).hexdigest())
    return _LOGIC_CACHE[0]


# Memo per (raw model, provider). The rate tables load once at import
# and are immutable at runtime, so the memo cannot rot in production;
# tests patch tables between calls and go through
# clear_fingerprint_cache().
_FP_CACHE: dict[tuple[str, str | None], str] = {}


def clear_fingerprint_cache() -> None:
    """Empty every memo derived from the loaded tables, including the
    tier fallbacks' (pricing.clear_tier_fallbacks): tests patch the
    tables between calls, and a memo computed under a patch must never
    outlive it."""
    pricing.clear_tier_fallbacks()
    _FP_CACHE.clear()


def _rates(rates: dict | None) -> list[float] | None:
    """Token rates plus the optional per-search rate, or None."""
    return (None if rates is None else
            [rates[f] for f in RATE_FIELDS]
            + [rates.get("web_search", 0.0)])


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
    windows = pricing.DATED_RATES.get(key)
    return {
        "key": key,
        "list": _rates(pricing.MODEL_RATES.get(key)),
        "windows": _windows(windows),
    }


def _tier(norm: str) -> list | None:
    """The FIRST matching family fallback's index and rates, when the
    id matches no table key."""
    if pricing._match_key(norm) is not None:  # pylint: disable=protected-access
        return None
    for index, (pattern, rates) in enumerate(
            pricing._tier_fallbacks()):  # pylint: disable=protected-access
        if pattern.search(norm):
            return [index, _rates(rates)]
    return None


def _provider_doc(norm: str, provider: str) -> dict:
    """The provider-row branch: the folded key, its start, list price,
    dated windows, and the schedule riding each window's history entry."""
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
        # ended — on a row with a schedule on the tail entry, the tail
        # is where it rides. The per-window entries above cannot carry
        # it, so the fp names it here: a whole-week schedule on a
        # one-entry row must move the fp.
        "tail": _schedule(schedules.get(len(windows))),
    }


def _vendor_doc(norm: str) -> dict:
    """The bare-id vendor branch: the tracked key the bare form matches,
    and that row's list price, dated windows and start — the inputs the
    bare path consults (the provider-specific schedule is not applied)."""
    tracked = pricing._vendor_match(norm)  # pylint: disable=protected-access
    if tracked is None:
        return {"key": None, "list": None, "windows": [], "start": None}
    row = (tracked, pricing.VENDOR_HOSTS[tracked])
    return {
        "key": [tracked, row[1]],
        "list": _rates(pricing.PROVIDER_RATES.get(row)),
        "windows": _windows(pricing.PROVIDER_DATED_RATES.get(row)),
        "start": _iso(pricing.PROVIDER_STARTS.get(row)),
    }


def _document(model: str, provider: str | None) -> dict:
    """The fingerprinted structure, exactly the inputs resolve()
    consults for this pair."""
    norm = pricing._normalise(model)  # pylint: disable=protected-access
    doc: dict = {
        "v": _STRUCTURE_VERSION,
        "logic": _logic(),
        "norm": norm,
        "free": pricing._is_free(model, norm),  # pylint: disable=protected-access
        # The reprice pass's meter re-derivation consults membership and
        # complete per-model entries (issue #878), so a data-only edit to
        # any threshold or factor moves every pair's fingerprint.
        "metered": sorted(pricing.LONG_CONTEXT_MODELS),
        "meter_entries": sorted(pricing.LONG_CONTEXT_METERS.items()),
        # The bare-id vendor branch's inputs: the bare-form match table
        # (through the match itself) and the vendor row's data. Included
        # for every non-free pair — a provider-named pair never consults
        # it, and a stale fingerprint there only means a recompute, never
        # a wrong price.
        "vendor": _vendor_doc(norm),
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
