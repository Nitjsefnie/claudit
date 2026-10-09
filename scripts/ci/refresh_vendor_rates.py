#!/usr/bin/env python3
"""Track the first-party vendor table from OpenRouter's catalog (SV-VENDOR-RATES).

Runs inside refresh_provider_rates.main(), in the same hourly run: one
report, one PRICING_VERSION bump when anything moved, one commit. The
tracked set is configured, not enumerated: `openrouter.vendor.prefixes`
lists the vendor namespaces (the same list the loaders validate and the
bare-id path resolves through), and catalog ids under a listed prefix join
`openrouter.models` only when `architecture.output_modalities` is exactly
`["text"]`. Entries without that declaration, and entries with any other
output modality, are dropped silently before selection and auto-add.

A catalog id under a listed prefix whose derived key is not yet tracked is
ADDED to openrouter.models as ``{"id": <catalog id>, "vendor_host":
<providerName of the selected endpoint>}`` — the same entry shape the
issue-851 migration gave every tracked vendor row. The vendor pass WRITES
NO RATES: the provider pass carries the (key, vendor_host) row from the
NEXT hourly run, so a newly added model prices nothing for one run — the
one-run pickup delay is the design, and resolve()'s bare path falls
through until the row exists. An already-tracked key is never re-added or
rewritten: its tracked table entry stands byte for byte. Its listing still
controls meter membership and the per-model threshold/factors.

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

An unmodelled listed shape is a refusal: no entry is created, no existing
entry is touched, and no membership folds. The vendor pass refuses
first-party data the meter cannot represent because an operator can act on
that listing and it would otherwise stay unpriced every hour. The two
notices remain a catalog the pass cannot read and a model with no
first-party endpoint. Ambiguous prices without a pin, stale or malformed
pins, and broken or unrecognised fetches also refuse:

- a pricing key outside PRICED at a nonzero price, or a web_search value
  that is not a finite non-negative USD-per-search decimal string;
- a WEEKLY schedule: the price the pass compared is a window price at
  fetch time, and the pass cannot select an actual default;
- a long-context band with multiple bands, a threshold that is not positive,
  missing input/output prices, an unsupported band kind, or UTC fields.
  A min_prompt_tokens band yields its factors from the listing, folds its
  threshold/factors into long_context_meters, and contributes no rates.

Membership and meter data are listing-governed for vendor-tracked keys: a
banded model's key joins long_context_models and its threshold/factors are
stored together (a member requires its tracked entry, which the same run
adds); an unbanded vendor-tracked key leaves membership and its meter, and
every non-vendor key stands untouched. These changes move the version: the
reprice pass re-derives records.long_context from membership, and a meter
entry moves the rate fingerprint the reprice consults — so a quiet run (no
add, no fold) still writes nothing.

    python3 scripts/ci/refresh_vendor_rates.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# pylint: disable=wrong-import-position
sys.path.insert(0, str(Path(__file__).resolve().parent))
from refresh_prices import (MIN_PROMPT_TOKENS as _MIN_PROMPT,  # noqa: E402
                            PRICED, TOKEN_PRICED, RefreshError, Untracked,
                            is_zero, metered_band, rates_of)
from refresh_report import vendor_report  # noqa: E402
import refresh_pricelog  # noqa: E402
from backend import pricing  # noqa: E402


PRICING_JSON = REPO_ROOT / "src" / "pricing.json"


@dataclass
class VendorMove:
    """One tracked-table move: a new entry (an auto-add), a membership
    fold, or a meter write — the pass writes no rates."""
    id: str
    key: str
    added: bool = False
    membership: str = ""  # "+" joined long_context_models, "-" left, "" untouched
    meter: dict | None = None  # full effective meter when the move changes it


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
    """The sorted vendor-prefixed ids that declare exactly text output.

    The prefix list is config — ``openrouter.vendor.prefixes`` — which the
    loaders validate; a malformed one here refuses the would-be file.
    """
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
                      and item["id"].partition("/")[0] in prefixes
                      and isinstance(item.get("architecture"), dict)
                      and item["architecture"].get("output_modalities") == ["text"]))


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
    kind, or a band mixed with utc fields, refuses the vendor listing."""
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
        unknown = set(override) - {
            "utc_days", "utc_start", "utc_end", *TOKEN_PRICED}
        if unknown:
            raise Untracked(f"{where}: override kind not modelled: {sorted(unknown)}")
        weekly.append(override)
    return bands, weekly


def _listing(model_id: str, endpoint: object
             ) -> tuple[dict, dict | None, str]:
    """One endpoint as (five rates, meter entry or None, provider_name).
    The rates only distinguish two same-namespace endpoints; they are
    never written. An unmodelled shape refuses the add or fold."""
    if not (isinstance(endpoint, dict) and isinstance(endpoint.get("tag"), str)
            and isinstance(endpoint.get("pricing"), dict)):
        raise RefreshError(f"{model_id}: unrecognised endpoint shape")
    provider = endpoint.get("provider_name")
    if not (isinstance(provider, str) and provider):
        raise RefreshError(f"{model_id}: endpoint names no provider")
    where = f"{model_id} via {endpoint['tag']!r}"
    price = endpoint["pricing"]
    for key, value in price.items():
        if (key not in (*PRICED, "discount", "overrides")
                and not is_zero(value)):
            raise Untracked(f"{where}: pricing {key} {value!r} is not modelled")
    discount = price.get("discount", 0)
    if (not isinstance(discount, (int, float)) or isinstance(discount, bool)
            or not 0 <= discount < 1):
        raise RefreshError(f"{where}: discount {discount!r} is not a fraction")
    rates = rates_of(price, where)
    bands, weekly = _split_overrides(price, where)
    if weekly:
        raise Untracked(f"{where}: a weekly schedule: the pass cannot select "
                        "a default outside every window")
    meter = metered_band(price, where, bands)
    return rates, meter, provider


def _choose(model_id: str, endpoints: list, pin: object
            ) -> tuple[dict, dict | None, str] | None:
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
    (no first-party endpoint or refusal) land in outcome."""
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
        outcome.refusals.append(str(exc))
    except RefreshError as exc:
        outcome.refusals.append(str(exc))
    return None


def _fold_meter(meters: dict, key: str, meter: dict | None) -> bool:
    """Write or drop the listing's threshold/factors; return whether it
    changed. Global factors stay omitted from pricing.json."""
    if meter is not None:
        stored = {"threshold": meter["threshold"]}
        for field, default in (
                ("input_mult", pricing.LONG_CONTEXT_INPUT_MULT),
                ("output_mult", pricing.LONG_CONTEXT_OUTPUT_MULT)):
            if meter[field] != default:
                stored[field] = meter[field]
        if meters.get(key) == stored:
            return False
        meters[key] = stored
        return True
    if key in meters:
        del meters[key]
        return True
    return False


def _one_model(doc: dict, members: list, meters: dict, model_id: str,
               fetch_endpoints, outcome: VendorOutcome) -> None:
    """One catalog id's selection, auto-add or fold; findings land in
    outcome. An unmodelled listing refuses the run. A tracked key is never
    re-added or rewritten."""
    key = derive_key(model_id)
    selected = _select(model_id, fetch_endpoints, doc, key, outcome)
    if selected is None:
        return
    _rates, meter, host = selected
    metered = meter is not None
    # setdefault, never `or {}`: an empty tracked table is falsy, and a
    # fresh dict here would drop the auto-add's write on the floor.
    tracked = (doc.get("openrouter") or {}).setdefault("models", {})
    if key in tracked:
        membership = _fold_membership(members, key, metered)
        meter_changed = _fold_meter(meters, key, meter)
        if membership or meter_changed:
            outcome.moves.append(VendorMove(model_id, key,
                                            membership=membership, meter=meter))
        return
    tracked[key] = {"id": model_id, "vendor_host": host}
    membership = _fold_membership(members, key, metered)
    _fold_meter(meters, key, meter)
    outcome.moves.append(VendorMove(model_id, key, added=True,
                                    membership=membership, meter=meter))


def vendor_pass(doc: dict, fetch_models, fetch_endpoints) -> VendorOutcome:
    """One vendor pass over `doc` (mutated in place: tracked entries and
    long_context_models and long_context_meters), returning the moves and
    what a human must read. Only entries declaring exactly text output reach
    selection and auto-add. An Untracked first-party shape refuses the run.
    The catalog-unreadable and no-first-party-endpoint findings remain
    notices."""
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
