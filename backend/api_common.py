"""Shared helpers for the read endpoints (issue #8 module split).

api.py grew past pylint's 1000-line module gate, so the endpoint groups
moved into backend/api_{export,dashboard,sessions,cache}.py. Everything
two or more of them need lives here so the split does not create import
cycles: the dated-rate fold, the range and bucket pickers, and the ISO
serialiser.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from backend import pricing
from backend.timing import Phases, TIMING_ON  # noqa: F401  (re-export)  # pylint: disable=unused-import

log = logging.getLogger("claudit.api")


# --- dated-rate helpers ---------------------------------------------------
# cost_total always comes from SUM(cost_usd) — the per-record cost computed
# at ingest against that record's own timestamp. cost_buckets, by contrast,
# is re-derived from summed tokens, so each record must be grouped by the
# boundaries of its own (model, provider) rate row. The global RATE_EPOCHS
# array is used only when a pair is missing from the rollup boundary map.


def rate_epoch_sql(ts_column: str) -> tuple[str, list]:
    """Yield a pair-specific rate epoch and a global fallback array.

    The per-model query LEFT JOINs pair boundaries as ``rate_bounds``.
    Missing pairs use the global array. Epoch -1 is the LIST-price epoch:
    ``epoch_ts(-1)`` is None, so ``pricing.resolve(model, None, provider)``
    uses the same rates persist and reprice used for a NULL-ts record.
    """
    return (
        f"(CASE WHEN {ts_column} IS NULL THEN -1 "
        f"WHEN rate_bounds.pair_model IS NOT NULL "
        f"THEN width_bucket({ts_column}, rate_bounds.boundaries) "
        f"ELSE width_bucket({ts_column}, %s::timestamptz[]) END)",
        [pricing.RATE_EPOCHS],
    )


def epoch_ts(index: int) -> datetime | None:
    """Return a global-fallback representative, or None for LIST price."""
    if index < 0:
        return None
    if not pricing.RATE_EPOCHS:
        return None
    if index <= 0:
        return pricing.RATE_EPOCHS[0] - timedelta(microseconds=1)
    return pricing.RATE_EPOCHS[min(index, len(pricing.RATE_EPOCHS)) - 1]


def _pair_epoch_ts(index: int, model: str, provider: str | None,
                   pair_bounds: Mapping[tuple[str, str], list[datetime]]) \
        -> datetime | None:
    """Return the representative instant from the boundary array SQL used."""
    if index < 0:
        return None
    key = (model, provider or "")
    if key not in pair_bounds:
        return epoch_ts(index)
    boundaries = pair_bounds[key]
    if not boundaries:
        return epoch_ts(index)
    if index == 0:
        return boundaries[0] - timedelta(microseconds=1)
    return boundaries[min(index, len(boundaries)) - 1]


def _empty_model_entry(model: str) -> dict:
    return {
        "model": model,
        "turns": 0,
        "fresh": 0,
        "cache_create": 0,
        "cache_read": 0,
        "output": 0,
        "eph5": 0,
        "eph1h": 0,
        "cost_total": 0.0,
        # OR of every folded row's resolution, so an entry is an estimate
        # when any of its rows priced at a guessed rate.
        "estimated_rate": False,
        "_buckets": {
            "fresh": 0.0, "create_5m": 0.0, "create_1h": 0.0,
            "read": 0.0, "output": 0.0,
        },
    }


def _accumulate_buckets(entry: dict, rates: dict, fresh: int, cc: int,
                        cr: int, output: int, eph5: int, eph1h: int,
                        unsplit: int, long_context: bool = False) -> None:
    """Price one row's tokens into the entry's per-epoch cost buckets.

    long_context applies the Codex long-context meter exactly as
    pricing.compute_cost stores it (2x the whole input side, 1.5x
    output), so a row billed on that meter keeps its buckets summing to
    the stored cost_total.
    """
    b = entry["_buckets"]
    in_mult = (pricing.LONG_CONTEXT_INPUT_MULT if long_context else 1.0)
    out_mult = (pricing.LONG_CONTEXT_OUTPUT_MULT if long_context else 1.0)
    b["fresh"] += fresh * rates["fresh"] * in_mult / 1_000_000
    b["create_5m"] += eph5 * rates["create_5m"] * in_mult / 1_000_000
    # An undeclared TTL is priced as 1h, exactly as pricing.compute_cost
    # stores it, so the buckets keep summing to the stored total.
    b["create_1h"] += ((eph1h + unsplit) * rates["create_1h"] * in_mult
                       / 1_000_000)
    b["read"] += cr * rates["read"] * in_mult / 1_000_000
    b["output"] += output * rates["output"] * out_mult / 1_000_000


# The token columns of a fold row, in SELECT order after `turns`.
_FOLD_TOKENS = ("fresh", "cache_create", "cache_read", "output", "eph5", "eph1h")


def _accumulate_model_row(
        acc: dict, row, by_provider: bool,
        pair_bounds: Mapping[tuple[str, str], list[datetime]]) -> None:
    """Fold one (model, provider, rate_epoch, long_context, turns, fresh,
    cache_create, cache_read, output, eph5, eph1h, cost_total) row.

    Each row is priced by its own provider whichever way the entries are
    keyed, so a per-model entry's buckets still reconcile with its stored
    cost when its rows came from several hosts.
    """
    model, provider, tokens, res = _model_row_pricing(row, pair_bounds)
    key = (model, provider) if by_provider else model
    if key not in acc:
        acc[key] = _empty_model_entry(model)
        if by_provider:
            acc[key]["provider"] = provider
    entry = acc[key]
    entry["estimated_rate"] = entry["estimated_rate"] or res.estimated
    entry["turns"] += int(row[4] or 0)
    for field, value in tokens.items():
        entry[field] += value
    stored = float(row[11] or 0)
    entry["cost_total"] += stored
    _accumulate_row_buckets(entry, res, tokens, bool(row[3]), stored,
                            scaled=res.scheduled or bool(res.request_fee))


def _model_row_pricing(
        row, pair_bounds: Mapping[tuple[str, str], list[datetime]]) \
        -> tuple[str, str | None, dict, pricing.Resolution]:
    """Resolve one aggregate row from its pair and SQL epoch index."""
    # NULL is not masked here (issue #653): a stored model is never the
    # string `unknown`, and a NULL would mean ingest's refusal missed a
    # file — passing it through prices at the fallback rates, flagged
    # estimated, instead of renaming the hole.
    model = row[0]
    provider = row[1] or None
    tokens = dict(zip(_FOLD_TOKENS, (int(v or 0) for v in row[5:11])))
    representative = _pair_epoch_ts(int(row[2] or 0), model, provider,
                                    pair_bounds)
    return model, provider, tokens, pricing.resolve(model, representative, provider)


def _accumulate_row_buckets(entry: dict, res: pricing.Resolution, tokens: dict,
                            long_context: bool, stored: float,
                            scaled: bool = False) -> None:
    """Price one fold row's tokens into the entry's buckets.

    A scheduled row's records were priced by their own time of day, which
    one representative time cannot reproduce: its buckets take their split
    from these rates and are scaled to its stored total (SV-RATE-DATA). A
    fee row's total carries the serving host's per-request fees (issue
    #469), which no token bucket re-derives: the same scaling applies, so
    the decomposition still sums to what it decomposes.
    """
    target = {"_buckets": dict.fromkeys(entry["_buckets"], 0.0)} \
        if (res.scheduled or scaled) else entry
    _accumulate_buckets(
        target, res.rates, tokens["fresh"], tokens["cache_create"],
        tokens["cache_read"], tokens["output"], tokens["eph5"], tokens["eph1h"],
        max(0, tokens["cache_create"] - tokens["eph5"] - tokens["eph1h"]),
        long_context)
    if target is not entry:
        derived = sum(target["_buckets"].values())
        scale = stored / derived if derived else 1.0
        for field, value in target["_buckets"].items():
            entry["_buckets"][field] += value * scale


def _fold(rows, by_provider: bool,
          pair_bounds: Mapping[tuple[str, str], list[datetime]]) -> list[dict]:
    acc: dict = {}
    for row in rows:
        _accumulate_model_row(acc, row, by_provider, pair_bounds)

    out = []
    for entry in acc.values():
        buckets = entry.pop("_buckets")
        total_in = entry["fresh"] + entry["cache_create"] + entry["cache_read"]
        entry["hit_rate_pct"] = round(
            (entry["cache_read"] / total_in * 100.0) if total_in else 0.0, 1
        )
        entry["cost_total"] = round(entry["cost_total"], 4)
        entry["cost_buckets"] = {k: round(v, 4) for k, v in buckets.items()}
        out.append(entry)
    out.sort(key=lambda e: e["cost_total"], reverse=True)
    return out


def fold_per_model(
        rows, *, pair_bounds: Mapping[tuple[str, str], list[datetime]]) \
        -> list[dict]:
    """Fold (model, provider, rate_epoch, ...) rows into one entry per model.

    Token counts and cost_total sum across epochs and providers;
    cost_buckets use each pair's boundary array so they reconcile with
    cost_total.
    """
    return _fold(rows, by_provider=False, pair_bounds=pair_bounds)


def fold_per_model_provider(
        rows, *, pair_bounds: Mapping[tuple[str, str], list[datetime]]) \
        -> list[dict]:
    """The same fold, one entry per (model, provider). `provider` is None
    for a record that named no serving host (every non-OpenRouter lane)."""
    return _fold(rows, by_provider=True, pair_bounds=pair_bounds)


# Activity-heatmap timezone. Bound as a SQL parameter (never interpolated);
# Postgres tzdata makes AT TIME ZONE fully DST-aware (CET/CEST transitions).
HEATMAP_TZ = "Europe/Prague"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


_BUCKET_CANDIDATES_S = (60, 5*60, 15*60, 30*60, 3600, 6*3600, 12*3600, 86400)


def _bucket_seconds(delta: timedelta) -> int:
    """Pick the LARGEST bucket size in [60s, 86400s] (≤ 1 day) that
    still produces ≥100 bins across the range. Mirrors the frontend's
    dashboard binMs picker; applied to every server-side bucketed
    query so 24h ranges don't get hardcoded-hourly 24 buckets."""
    span_s = max(1, int(delta.total_seconds()))
    chosen = _BUCKET_CANDIDATES_S[0]
    for b in _BUCKET_CANDIDATES_S:
        if b > 86400:
            break
        if span_s / b < 100:
            break
        chosen = b
    return chosen


def _parse_range(s: str) -> timedelta:
    """`Nd` / `Nh` parse normally. `all` returns now-epoch so callers
    that compute `since = now - delta` end up at the unix epoch — i.e.
    every row in the DB, not an arbitrary 100-year window.

    A non-integer count (`1e5d`), an empty value (`?range=`), or one that
    overflows the caller's `since = now - delta` (`999999999d` — timedelta
    accepts the day count, the subtraction then runs past datetime.min)
    raises the same 400 the unknown-suffix branch raises, naming the bad
    parameter, so a malformed query value cannot escape any endpoint as a
    500 (issues #112, #262).
    """
    if s == "all":
        return datetime.now(timezone.utc) - _EPOCH
    if not s:
        raise HTTPException(400, f"bad range: {s!r}")
    unit, count = s[-1], s[:-1]
    if unit in ("d", "h"):
        try:
            delta = (timedelta(days=int(count)) if unit == "d"
                     else timedelta(hours=int(count)))
            # Trial subtraction: guards the arithmetic every caller is
            # about to do, HERE, so no endpoint needs its own try/except.
            _ = datetime.now(timezone.utc) - delta
        except (ValueError, OverflowError):
            raise HTTPException(400, f"bad range: {s!r}") from None
        return delta
    raise HTTPException(400, f"bad range: {s!r}")


def _iso(v) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)
