#!/usr/bin/env python3
"""Track the first-party vendor table from OpenRouter's catalog (SV-VENDOR-RATES).

Runs inside refresh_provider_rates.main(), in the same hourly run: one
report, one PRICING_VERSION bump when anything moved, one commit. The
tracked set is configured, not enumerated: `openrouter.vendor.prefixes`
lists the vendor namespaces (the same list the loaders validate and the
bare-id path resolves through), and every catalog id under a listed prefix
joins `openrouter.models` — a model the vendor adds under its prefix is
picked up on the next run, no code change.

A catalog id under a listed prefix whose derived key is not yet tracked is
ADDED to openrouter.models as ``{"id": <catalog id>, "vendor_host":
<providerName of the selected endpoint>}`` — the same entry shape the
issue-851 migration gave every tracked vendor row. The vendor pass WRITES
NO RATES: the provider pass carries the (key, vendor_host) row from the
NEXT hourly run, so a newly added model prices nothing for one run — the
one-run pickup delay is the design, and resolve()'s bare path falls
through until the row exists. An already-tracked key is never re-added or
rewritten: its listing is still fetched and selected, for the membership
fold below, and the entry stands byte for byte.

The vendor's own endpoint is selected refusing rather than guessing:

- the BARE tag (the namespace with no suffix) is preferred: Anthropic lists
  ``anthropic`` beside ``anthropic/fast`` (a throughput tier at double the
  price), OpenAI lists ``openai`` beside ``openai/fast`` and ``openai/flex``
  (service tiers) — the base tag is the first-party price;
- no bare tag: every vendor-prefix endpoint must agree on one price —
  Moonshot lists ``moonshotai/mxfp4`` (one endpoint), Z.ai mostly
  ``z-ai/fp8`` — and two prices refuse; a {"tag": ...} pin recorded in
  openrouter.vendor.resolve.<derived key> (with a why) takes that endpoint
  and narrows first;
- no vendor-prefix endpoint at all: NOTICE + skip — the model is offered
  only through third-party hosts; there is no first-party selection to
  make, and any tracked entry's own state stands untouched.

An unmodelled listed shape is a NOTICE — "not tracked: <reason>": no entry
is created, no existing entry is touched, and no membership folds. Red is
reserved for ambiguity a human must resolve (multi-price without a pin, a
stale or malformed pin), and for broken or unrecognised fetches — each
clearing on a human action or a retry:

- a pricing key outside PRICED at a nonzero price, unless it is a
  RECORDED_FEE (web_search); a fee is the provider row's provenance, not
  the tracked entry's — the pass writes nothing for it here — while a fee
  value the table could not parse stays a not-tracked notice;
- a WEEKLY schedule: the price the pass compared is a window price at
  fetch time, the same reason the provider pass refuses a first-seen
  scheduled host inside one; the model is not tracked until the pass can
  select an actual default;
- a long-context band (a min_prompt_tokens override) whose threshold or
  multipliers depart from the meter — pricing.LONG_CONTEXT_THRESHOLD, the
  input side at LONG_CONTEXT_INPUT_MULT, output at LONG_CONTEXT_OUTPUT_MULT.
  The table models exactly one band shape, the Codex meter's, by folding
  the key into long_context_models; the band's own rates never enter
  anything the pass writes. A departing shape is not tracked: the 200k-band
  Claude models are the live case.

Membership is data the listing governs, for vendor-tracked keys: a banded
model's key joins long_context_models (a member requires its tracked entry,
which the same run adds), an unbanded vendor-tracked key leaves it, and
every non-vendor key stands untouched. Membership-only changes move the
version: the reprice pass re-derives records.long_context from the
membership, and a new entry moves the rate fingerprint the reprice
consults — so a quiet run (no add, no fold) still writes nothing.

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
from refresh_prices import (MIN_PROMPT_TOKENS as _MIN_PROMPT,  # noqa: E402
                            PRICED, RECORDED_FEES, RefreshError, Untracked,
                            is_zero, metered_band, rates_of)
from refresh_report import vendor_report  # noqa: E402
import refresh_pricelog  # noqa: E402
from backend import pricing  # noqa: E402


PRICING_JSON = REPO_ROOT / "src" / "pricing.json"


@dataclass
class VendorMove:
    """One tracked-table move: a new entry (an auto-add), a membership
    fold, or a meter-threshold write — the pass writes no rates."""
    id: str
    key: str
    added: bool = False
    membership: str = ""  # "+" joined long_context_models, "-" left, "" untouched
    meter: int | None = None  # the threshold written or moved, None untouched


@dataclass
class VendorOutcome:
    """One vendor pass: the moves, and what a human must read."""
    moves: list[VendorMove] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)


def derive_key(catalog_id: str) -> str:
    """The tracked-table key a catalog id derives: the slug, dot-folded —
    ``openai/gpt-5.5`` → ``gpt-5-5``, the same normalisation resolve()
    applies to a transcript naming the bare first-party id (the dots are
    the vendor's spelling). Prefix parity is a bare-id claim: a transcript
    spelling the vendor prefix is OpenRouter provider-row traffic, priced
    by that table by design."""
    return catalog_id.partition("/")[2].replace(".", "-").lower()


def catalog_ids(catalog: object, doc: dict) -> list[str]:
    """The catalog's vendor-prefixed, non-variant ids, sorted. The prefix
    list is config — ``openrouter.vendor.prefixes`` — which the loaders
    validate; a malformed one here refuses the would-be file."""
    prefixes = ((doc.get("openrouter") or {}).get("vendor") or {}).get("prefixes")
    if not isinstance(prefixes, list) or not all(isinstance(p, str) for p in prefixes):
        raise RefreshError(
            "openrouter.vendor.prefixes is not a list of namespace strings")
    data = catalog.get("data") if isinstance(catalog, dict) else None
    if not isinstance(data, list):
        raise RefreshError("models response has no data list")
    return sorted(item["id"] for item in data
                  if (isinstance(item, dict) and isinstance(item.get("id"), str)
                      and ":" not in item["id"]
                      and item["id"].partition("/")[0] in prefixes))


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
    """A price's overrides as (band overrides, weekly windows). An unknown
    kind, or a band mixed with utc fields, is untracked."""
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


def _listing(model_id: str, endpoint: object) -> tuple[dict, bool, int | None, str]:
    """One endpoint as (five rates, metered?, the band's own threshold,
    provider_name) — the rates only to tell two same-namespace endpoints'
    prices apart, never written.
    Refusing an unmodelled shape: the add or fold the endpoint would have
    carried does not happen; a not-tracked notice lands instead."""
    if not (isinstance(endpoint, dict) and isinstance(endpoint.get("tag"), str)
            and isinstance(endpoint.get("pricing"), dict)):
        raise RefreshError(f"{model_id}: unrecognised endpoint shape")
    provider = endpoint.get("provider_name")
    if not (isinstance(provider, str) and provider):
        raise RefreshError(f"{model_id}: endpoint names no provider")
    where = f"{model_id} via {endpoint['tag']!r}"
    price = endpoint["pricing"]
    for key, value in price.items():
        if (key not in (*PRICED, *RECORDED_FEES, "discount", "overrides")
                and not is_zero(value)):
            raise Untracked(f"{where}: pricing {key} {value!r} is not modelled")
    discount = price.get("discount", 0)
    if (not isinstance(discount, (int, float)) or isinstance(discount, bool)
            or not 0 <= discount < 1):
        raise RefreshError(f"{where}: discount {discount!r} is not a fraction")
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
    rates = rates_of(price, where)
    bands, weekly = _split_overrides(price, where)
    if weekly:
        raise Untracked(f"{where}: a weekly schedule: not tracked until the "
                        "pass can select a default outside every window")
    metered, threshold = metered_band(price, where, bands)
    return rates, metered, threshold, provider


def _choose(model_id: str, endpoints: list, pin: object
            ) -> tuple[dict, bool, int | None, str] | None:
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


def _fold_meter(meters: dict, key: str, metered: bool,
                threshold: int | None) -> int | None:
    """Write or drop the model's meter threshold per the listing, and
    name the written threshold for the move (None: untouched). The drop
    keeps the loaders' rule — a meters key names a member."""
    if metered and threshold is not None:
        if meters.get(key) != {"threshold": threshold}:
            meters[key] = {"threshold": threshold}
            return threshold
        return None
    if not metered and key in meters:
        del meters[key]
    return None


def _one_model(doc: dict, members: list, meters: dict, model_id: str,
               fetch_endpoints, outcome: VendorOutcome) -> None:
    """One catalog id's selection, auto-add or fold; findings land in
    `outcome`. An Untracked shape notices; red stays for ambiguity, pins and
    broken fetches. A tracked key is never re-added or rewritten."""
    key = derive_key(model_id)
    selected = _select(model_id, fetch_endpoints, doc, key, outcome)
    if selected is None:
        return
    _rates, metered, threshold, host = selected
    # setdefault, never `or {}`: an empty tracked table is falsy, and a
    # fresh dict here would drop the auto-add's write on the floor.
    tracked = (doc.get("openrouter") or {}).setdefault("models", {})
    if key in tracked:
        membership = _fold_membership(members, key, metered)
        meter = _fold_meter(meters, key, metered, threshold)
        if membership or meter is not None:
            outcome.moves.append(VendorMove(model_id, key,
                                            membership=membership, meter=meter))
        return
    tracked[key] = {"id": model_id, "vendor_host": host}
    membership = _fold_membership(members, key, metered)
    meter = _fold_meter(meters, key, metered, threshold)
    outcome.moves.append(VendorMove(model_id, key, added=True,
                                    membership=membership, meter=meter))


def vendor_pass(doc: dict, fetch_models, fetch_endpoints) -> VendorOutcome:
    """One vendor pass over `doc` (mutated in place: tracked entries and
    long_context_models), returning the moves and what a human must read.
    An unmodelled shape is a NOTICE — "not tracked: <reason>": no entry is
    created and no existing entry is touched. Red is reserved for ambiguity
    a human must resolve (multi-price without a pin, a stale or malformed
    pin), and for broken or unrecognised fetches — each clearing on a human
    action or a retry."""
    outcome = VendorOutcome()
    try:
        ids = catalog_ids(fetch_models(), doc)
    except Exception as exc:  # a catalog the pass cannot read skips the pass
        outcome.notices.append(
            f"vendor pass skipped: the catalog is unreadable "
            f"({str(exc) or type(exc).__name__})")
        return outcome
    members: list = doc.setdefault("long_context_models", [])
    meters: dict = doc.setdefault("long_context_meters", {})
    for model_id in ids:
        _one_model(doc, members, meters, model_id, fetch_endpoints, outcome)
    members.sort()
    # The loaders' rules, run on what would be written: among them, a member
    # naming no models-table or tracked key, and a tracked entry without its id.
    try:
        pricing.load_tables(doc)
    except ValueError as exc:
        raise RefreshError(f"the refreshed file would not load: {exc}") from exc
    return outcome


def main(argv: list[str] | None = None) -> int:
    """Standalone dry-run: report what a run would move; write nothing."""
    parser = argparse.ArgumentParser(
        description="Report tracked-table vendor moves (dry-run).")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would move; write nothing (the "
                             "standalone entry never writes)")
    args = parser.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        doc = json.loads(PRICING_JSON.read_text(encoding="utf-8"))
        vendor = vendor_pass(doc, refresh_pricelog.fetch_models,
                             refresh_pricelog.fetch_endpoints)
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
