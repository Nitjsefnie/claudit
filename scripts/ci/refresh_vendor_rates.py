#!/usr/bin/env python3
"""Refresh src/pricing.json's first-party vendor rows (SV-VENDOR-RATES).

Runs inside refresh_provider_rates.main(), in the same hourly run: one
report, one PRICING_VERSION bump when anything moved, one commit. The
maintainer's ruling: claudit pulls Anthropic, OpenAI, Moonshot and Z.ai
first-party list pricing automatically, selected by VENDOR PREFIX over
OpenRouter's catalog — every id under anthropic/*, openai/*, moonshotai/*,
z-ai/* — priced at the vendor's own first-party endpoint (the endpoint whose
tag prefix is the vendor's own namespace), never a third-party host. No
per-model allowlist: a model the vendor adds under its prefix is picked up
on the next run.

A variant id (any ``:<suffix>`` — ``:batch``, ``:free``, ``:nitro``) is never
a row and never a move: the bare id is the model, and the vendor's free and
batch tiers are not the first-party list price (resolve() prices ``:free``
at zero before any table lookup, and ``:batch`` is a discounted tier of its
own).

The vendor's own endpoints resolve to ONE listing per model, refusing
rather than guessing:

- the BARE tag (the namespace with no suffix) is preferred: Anthropic lists
  ``anthropic`` beside ``anthropic/fast`` (a throughput tier at double the
  price), OpenAI lists ``openai`` beside ``openai/fast`` and ``openai/flex``
  (service tiers) — the base tag is the list price;
- no bare tag: every vendor-prefix endpoint must agree on one price —
  Moonshot lists ``moonshotai/mxfp4`` (one endpoint), Z.ai mostly
  ``z-ai/fp8`` — and two prices refuse (only one can be the list price); a
  {"tag": ...} pin recorded in openrouter.vendor.resolve.<derived key>
  (with a why) takes that endpoint and narrows first;
- no vendor-prefix endpoint at all: NOTICE + skip — the model is offered
  only through third-party hosts (the Codex ids, gpt-oss, the legacy Kimi
  models); there is no first-party list price to track, and any row's own
  history stands untouched.

An unmodelled listed shape is a NOTICE — "not tracked: <reason>": no row
is created and no existing row is touched. Red is reserved for ambiguity a
human must resolve (multi-price without a pin, a stale or malformed pin),
and for broken or unrecognised fetches — each clearing on a human action
or a retry:

- a pricing key outside PRICED at a nonzero price, unless it is a
  RECORDED_FEE (web_search). A fee is never dropped in silence, but on a
  MODELS row it is recorded as a provenance note WITHOUT the priced
  ``/request`` note shape: the models table prices first-party traffic,
  whose requests pay no per-request fee, and the priced note shape is
  exactly what the loaders fold in once per request;
- a WEEKLY schedule (pricing.overrides with utc windows): a models row
  cannot carry one — the loaders admit a schedule on provider rows only —
  so the model refuses until the table learns the shape;
- a long-context band (a min_prompt_tokens override) whose threshold or
  multipliers depart from the meter — pricing.LONG_CONTEXT_THRESHOLD, the
  input side at LONG_CONTEXT_INPUT_MULT, output at LONG_CONTEXT_OUTPUT_MULT.
  The table models exactly one band shape, the Codex meter's, by folding
  the key into long_context_models; the band's own rates never enter the
  row (the five stored rates stay the sub-threshold listing, and
  compute_cost applies the meter above the threshold). A departing shape
  refuses: the 200k-band Claude models are the live case.

Membership is data the listing governs, for vendor-tracked keys: a banded
model's key joins long_context_models (adding a member requires its
models-table row, which the same run adds), an unbanded vendor-tracked key
leaves it, and every non-vendor key stands untouched. Membership-only
changes move the version: the reprice pass re-derives records.long_context
from the membership.

A listed price change APPENDS a dated entry, the way the provider refresh
appends; a hand-curated row whose vendor source has moved on gets the
appended entry too (the listing governs a row the refresh owns), and a
quiet source writes nothing. The loaders' rules run on the would-be file
before anything is written.

    python3 scripts/ci/refresh_vendor_rates.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# pylint: disable=wrong-import-position
sys.path.insert(0, str(Path(__file__).resolve().parent))
from refresh_prices import (PRICED, RECORDED_FEES, RefreshError,  # noqa: E402
                            is_zero, rates_of)
from refresh_report import vendor_report  # noqa: E402
import refresh_pricelog  # noqa: E402
from backend import pricing  # noqa: E402


class Untracked(RefreshError):
    """A listed shape the table deliberately does not track: the model
    is a NOTICE — no row created, no existing row touched — never red."""


PRICING_JSON = REPO_ROOT / "src" / "pricing.json"

# The four vendors' namespaces: the catalog-id prefix and, the same string,
# the first segment of the vendor's own endpoint tags.
VENDOR_PREFIXES = ("anthropic", "openai", "moonshotai", "z-ai")

_INPUT_FIELDS = ("fresh", "create_5m", "create_1h", "read")
_RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")
_MIN_PROMPT = "min_prompt_tokens"


@dataclass
class VendorMove:
    """One models-table move: a new row, an appended entry, or a
    membership-only change (entries 0)."""
    id: str
    key: str
    old: dict | None
    new: dict
    entries: int = 1
    membership: str = ""  # "+" joined long_context_models, "-" left, "" untouched


@dataclass
class VendorOutcome:
    """One vendor pass: the moves, and what a human must read."""
    moves: list[VendorMove] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)


def derive_key(catalog_id: str) -> str:
    """The models-table key a catalog id derives: the slug, dot-folded —
    ``openai/gpt-5.5`` → ``gpt-5-5``, the same normalisation resolve()
    applies to a transcript naming the bare first-party id (the dots are
    the vendor's spelling). Prefix parity is a bare-id claim: a transcript
    spelling the vendor prefix is OpenRouter provider-row traffic, priced
    by that table by design."""
    return catalog_id.partition("/")[2].replace(".", "-").lower()


def catalog_ids(catalog: object) -> list[str]:
    """The catalog's vendor-prefixed, non-variant ids, sorted."""
    data = catalog.get("data") if isinstance(catalog, dict) else None
    if not isinstance(data, list):
        raise RefreshError("models response has no data list")
    return sorted(item["id"] for item in data
                  if (isinstance(item, dict) and isinstance(item.get("id"), str)
                      and ":" not in item["id"]
                      and item["id"].partition("/")[0] in VENDOR_PREFIXES))


def _extract_endpoints(model_id: str, payload: object) -> list:
    data = payload.get("data") if isinstance(payload, dict) else None
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, list):
        raise RefreshError(f"{model_id}: unrecognised endpoints response shape")
    if not endpoints:
        raise RefreshError(f"{model_id}: no endpoints listed; read as a broken "
                           "fetch, never as every host vanishing")
    return endpoints


def _split_overrides(price: dict, where: str) -> tuple[list, list]:
    """A price's overrides as (band overrides, weekly overrides). An
    unknown key, or a band mixed with utc fields, refuses."""
    overrides = price.get("overrides")
    if overrides in (None, []):
        return [], []
    if not isinstance(overrides, list) or not all(isinstance(o, dict) for o in overrides):
        raise RefreshError(f"{where}: pricing.overrides is not a list of windows")
    bands, weekly = [], []
    for override in overrides:
        if _MIN_PROMPT in override:
            if set(override) & {"utc_days", "utc_start", "utc_end"}:
                raise Untracked(f"{where}: a band and utc fields on one "
                                "override is not modelled")
            bands.append(override)
            continue
        unknown = set(override) - {"utc_days", "utc_start", "utc_end", *PRICED}
        if unknown:
            raise Untracked(f"{where}: override kind not modelled: {sorted(unknown)}")
        weekly.append(override)
    return bands, weekly


def _meter_shape_ok(base: dict, band: dict, band_rates: dict) -> bool:
    """Whether one band is the meter: the threshold constant (read off the
    raw band), the input side at the input multiplier, output at the output
    multiplier. `base` and `band_rates` are rates_of() shapes."""
    if band[_MIN_PROMPT] != pricing.LONG_CONTEXT_THRESHOLD:
        return False
    for f in _INPUT_FIELDS:
        if round(band_rates[f], 10) != round(base[f] * pricing.LONG_CONTEXT_INPUT_MULT, 10):
            return False
    return round(band_rates["output"], 10) == round(
        base["output"] * pricing.LONG_CONTEXT_OUTPUT_MULT, 10)


def _note_parts(price: dict, where: str) -> tuple[str, ...]:
    """The provenance notes an entry carries: the discount, then one part
    per RECORDED fee at a nonzero price — never the priced ``/request``
    note shape (see the module docstring)."""
    parts = []
    discount = price.get("discount", 0)
    if (not isinstance(discount, (int, float)) or isinstance(discount, bool)
            or not 0 <= discount < 1):
        raise RefreshError(f"{where}: discount {discount!r} is not a fraction")
    if discount:
        parts.append(f"{format((Decimal(str(discount)) * 100).normalize(), 'f')}% off")
    for key in RECORDED_FEES:
        value = price.get(key)
        if value is None or is_zero(value):
            continue
        try:
            amount = Decimal(value) if isinstance(value, str) else None
        except InvalidOperation:
            amount = None
        if amount is None or not amount.is_finite() or amount < 0:
            raise Untracked(f"{where}: fee {key} {value!r} is not a nonnegative "
                            "decimal string")
        parts.append(f"{key} ${format(amount.normalize(), 'f')} per tool call on "
                     "the listing: not a per-request cost")
    return tuple(parts)


def _listing(model_id: str, endpoint: object) -> tuple[dict, bool, tuple[str, ...]]:
    """One endpoint as (five rates, metered?, note parts), refusing an
    unmodelled shape. The metered flag reads the long-context band; the
    band's rates never enter the row."""
    if not (isinstance(endpoint, dict) and isinstance(endpoint.get("tag"), str)
            and isinstance(endpoint.get("pricing"), dict)):
        raise RefreshError(f"{model_id}: unrecognised endpoint shape")
    where = f"{model_id} via {endpoint['tag']!r}"
    price = endpoint["pricing"]
    for key, value in price.items():
        if (key not in (*PRICED, *RECORDED_FEES, "discount", "overrides")
                and not is_zero(value)):
            raise Untracked(f"{where}: pricing {key} {value!r} is not modelled")
    rates = rates_of(price, where)
    bands, weekly = _split_overrides(price, where)
    if weekly:
        raise Untracked(f"{where}: a weekly schedule: a models row carries "
                        "no schedule")
    metered = False
    if bands:
        if len(bands) > 1:
            raise Untracked(f"{where}: {len(bands)} long-context bands are "
                            "not modelled")
        band = bands[0]
        if set(band) - {*PRICED, _MIN_PROMPT}:
            raise Untracked(f"{where}: band kind not modelled: "
                            f"{sorted(set(band) - {*PRICED, _MIN_PROMPT})}")
        if not (isinstance(band.get("prompt"), str)
                and isinstance(band.get("completion"), str)):
            raise Untracked(f"{where}: the band does not restate input and "
                            "output: not modelled")
        if not _meter_shape_ok(rates, band, rates_of(band, where)):
            raise Untracked(
                f"{where}: long-context band departs from the meter "
                f"({band.get(_MIN_PROMPT)!r} against the meter's "
                f"{pricing.LONG_CONTEXT_THRESHOLD}): a models row cannot carry it")
        metered = True
    return rates, metered, _note_parts(price, where)


def _choose(model_id: str, endpoints: list, pin: object
            ) -> tuple[dict, bool, tuple[str, ...]] | None:
    """The vendor's own endpoint's listing, refusing an ambiguous one.
    None means the vendor lists no first-party endpoint (a notice, not a
    refusal)."""
    ns = model_id.partition("/")[0]
    mine = [e for e in endpoints
            if isinstance(e, dict) and isinstance(e.get("tag"), str)
            and e["tag"].partition("/")[0] == ns]
    if not mine:
        return None
    if pin is not None:
        if (not isinstance(pin, dict) or not isinstance(pin.get("tag"), str)
                or not set(pin) <= {"tag", "why"} or "tag" not in pin):
            raise RefreshError(f"{model_id}: the resolve pin is a dict of a "
                               f"'tag' string and an optional 'why': {pin!r}")
        mine = [e for e in mine if e["tag"] == pin["tag"]]
        if not mine:
            raise RefreshError(f"{model_id}: the pinned tag is not listed; the "
                               "pin is stale")
    else:
        # The bare tag is the list price; the vendor's other tags are
        # service tiers (fast, flex) or quantizations of their own price.
        bare = [e for e in mine if e["tag"] == ns]
        mine = bare or mine
    listings = [_listing(model_id, e) for e in mine]
    distinct = {json.dumps(listing, sort_keys=True) for listing in listings}
    if len(distinct) > 1:
        tags = ", ".join(sorted({e["tag"] for e in mine}))
        raise RefreshError(
            f"{model_id}: the vendor lists {len(mine)} endpoints ({tags}) at "
            f"{len(distinct)} prices; record a pin in openrouter.vendor.resolve"
            f".{derive_key(model_id)}")
    return listings[0]


def _append(entry: dict, stamp: str | None, rates: dict,
            notes: tuple[str, ...]) -> dict:
    entry["from"] = stamp
    entry.update(rates)
    if notes:
        entry["note"] = "; ".join(notes)
    return entry


def _fold_membership(members: list, key: str, metered: bool) -> str:
    """Fold `key` in or out of long_context_models per the listing, and
    name the fold for the move."""
    if metered and key not in members:
        members.append(key)
        return "+"
    if not metered and key in members:
        members.remove(key)
        return "-"
    return ""


def _select(model_id: str, fetch_endpoints, doc: dict, key: str,
            outcome: VendorOutcome):
    """The chosen listing, or None when the pass moves on: the findings
    (no first-party endpoint, not-tracked, refusal) land in `outcome`."""
    try:
        payload = fetch_endpoints(model_id)
    except Exception as exc:  # a broken fetch refuses, as any broken fetch does
        outcome.refusals.append(
            f"{model_id}: fetching its endpoints failed: {str(exc) or type(exc).__name__}")
        return None
    try:
        selected = _choose(
            model_id, _extract_endpoints(model_id, payload),
            ((doc.get("openrouter") or {}).get("vendor") or {}).get(
                "resolve", {}).get(key))
        if selected is None:
            outcome.notices.append(
                f"{model_id}: the vendor lists no first-party endpoint; skipped")
        return selected
    except Untracked as exc:
        outcome.notices.append(f"{model_id}: not tracked: {exc}")
    except RefreshError as exc:
        outcome.refusals.append(str(exc))
    return None


def _one_model(doc: dict, members: list, model_id: str, fetch_endpoints,
               stamp: str, at: datetime, outcome: VendorOutcome) -> None:
    """One catalog id's selection, comparison and row write; findings land
    in `outcome`. An Untracked shape notices; red stays for ambiguity,
    pins and broken fetches."""
    key = derive_key(model_id)
    selected = _select(model_id, fetch_endpoints, doc, key, outcome)
    if selected is None:
        return
    rates, metered, notes = selected
    if doc["models"].get(key) is None:
        membership = _fold_membership(members, key, metered)
        doc["models"][key] = [_append({}, None, rates, notes)]
        outcome.moves.append(VendorMove(
            model_id, key, None, doc["models"][key][0], 1, membership))
        return
    newest = doc["models"][key][-1]
    moved = any(round(float(newest[f]), 10) != round(float(rates[f]), 10)
                for f in _RATE_FIELDS)
    if moved and newest.get("from") is not None and pricing._instant(  # pylint: disable=protected-access
            newest["from"], key) >= at:
        outcome.notices.append(
            f"{key}: the stored newest entry is dated {newest['from']}, not "
            "before the detection instant; the row was left untouched")
        return
    membership = _fold_membership(members, key, metered)
    if not moved and not membership:
        return
    if moved:
        doc["models"][key].append(_append({}, stamp, rates, notes))
        outcome.moves.append(VendorMove(
            model_id, key, {f: newest[f] for f in _RATE_FIELDS},
            doc["models"][key][-1], 1, membership))
        return
    outcome.moves.append(VendorMove(model_id, key, None, rates, 0, membership))


def vendor_pass(doc: dict, fetch_models, fetch_endpoints, stamp: str,
                at: datetime) -> VendorOutcome:
    """One vendor pass over `doc` (mutated in place: models rows and
    long_context_models), returning the moves and what a human must read.
    An unmodelled shape is a NOTICE — "not tracked: <reason>": no row is
    created and no existing row is touched. Red is reserved for ambiguity a
    human must resolve (multi-price without a pin, a stale or malformed
    pin), and for broken or unrecognised fetches — each clearing on a human
    action or a retry."""
    outcome = VendorOutcome()
    try:
        ids = catalog_ids(fetch_models())
    except Exception as exc:  # a catalog the pass cannot read skips the pass
        outcome.notices.append(
            f"vendor pass skipped: the catalog is unreadable "
            f"({str(exc) or type(exc).__name__})")
        return outcome
    members: list = doc.setdefault("long_context_models", [])
    for model_id in ids:
        _one_model(doc, members, model_id, fetch_endpoints, stamp, at, outcome)
    members.sort()
    # The loaders' rules, run on what would be written: among them, a
    # member naming no models-table key, and a models entry with fields
    # the table does not carry.
    try:
        pricing.load_tables(doc)
    except ValueError as exc:
        raise RefreshError(f"the refreshed file would not load: {exc}") from exc
    return outcome


def main(argv: list[str] | None = None) -> int:
    """Standalone dry-run: report what a run would append; write nothing."""
    parser = argparse.ArgumentParser(
        description="Report moved first-party vendor list rates (dry-run).")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be appended; write nothing (the "
                             "standalone entry never writes)")
    args = parser.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    at = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    try:
        doc = json.loads(PRICING_JSON.read_text(encoding="utf-8"))
        vendor = vendor_pass(doc, refresh_pricelog.fetch_models,
                             refresh_pricelog.fetch_endpoints, stamp, at)
        print(vendor_report(stamp, vendor))
    except RefreshError as exc:
        print(f"refresh_vendor_rates: {exc}", file=sys.stderr)
        return 1
    if vendor.refusals:
        print("\n".join(f"refresh_vendor_rates: {r}" for r in vendor.refusals),
              file=sys.stderr)
        return 1
    _ = args  # the standalone entry never writes, dry-run or not
    return 0


if __name__ == "__main__":
    sys.exit(main())
