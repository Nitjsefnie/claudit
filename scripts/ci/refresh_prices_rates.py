"""OpenRouter listing normalization."""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation

from backend import pricing


# Token prices and the per-search price this script models. Any other
# pricing key at a nonzero price refuses its host.
TOKEN_PRICED = ("prompt", "completion", "input_cache_read",
                "input_cache_write", "input_cache_write_1h")
PRICED = (*TOKEN_PRICED, "web_search")
_OVERRIDE_KEYS = frozenset({"utc_days", "utc_start", "utc_end", *TOKEN_PRICED})
_UTC_KEYS = frozenset({"utc_days", "utc_start", "utc_end"})

MIN_PROMPT_TOKENS = "min_prompt_tokens"


def meter_shape_ok(base: dict, band_rates: dict,
                   where: str = "long-context band",
                   band: dict | None = None) -> tuple[float, float]:
    """Ten-decimal ratios; fresh factor covers both writes and reads; valid nondefaults feed vendor factors."""
    if base["fresh"] == 0:
        if band_rates["fresh"] != 0:
            raise Untracked(
                f"{where}: input factor cannot be inferred from a zero base rate")
        input_mult = pricing.LONG_CONTEXT_INPUT_MULT
    else:
        input_mult = round(band_rates["fresh"] / base["fresh"], 10)
    if not math.isfinite(input_mult) or input_mult <= 0:
        raise Untracked(f"{where}: input multiplier is not positive and finite")

    # The meter applies one input factor to fresh tokens, cache writes, and
    # cache reads. Preserve every cache price explicitly listed by the band
    # only when that same factor represents it at the comparison precision.
    cache_fields = (
        ("input_cache_read", "read", "read"),
        ("input_cache_write", "create_5m", "5m write"),
        ("input_cache_write_1h", "create_1h", "1h write"),
    )
    if band is not None:
        for key, field, label in cache_fields:
            if key not in band:
                continue
            base_rate = base[field]
            band_rate = band_rates[field]
            expected = round(base_rate * input_mult, 10)
            if round(band_rate, 10) == expected:
                continue
            implied = (f"x{round(band_rate / base_rate, 10):g}"
                       if base_rate else "undefined (zero base rate)")
            raise Untracked(
                f"{where}: cache {label} is not representable by one input "
                f"factor: base {base_rate:g}, band {band_rate:g}, implied "
                f"factor {implied} (meter factor x{input_mult:g})")

    if base["output"] == 0:
        if band_rates["output"] != 0:
            raise Untracked(
                f"{where}: output factor cannot be inferred from a zero base rate")
        output_mult = pricing.LONG_CONTEXT_OUTPUT_MULT
    else:
        output_mult = round(band_rates["output"] / base["output"], 10)
    if not math.isfinite(output_mult) or output_mult <= 0:
        raise Untracked(f"{where}: output multiplier is not positive and finite")
    return input_mult, output_mult


def band_threshold(band: dict, where: str) -> int:
    """The band threshold: a positive integer."""
    value = band[MIN_PROMPT_TOKENS]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise Untracked(f"{where}: band threshold {value!r} is not a "
                        "positive integer")
    return value


def metered_band(price: dict, where: str,
                 bands: list[dict]) -> dict | None:
    """Fold one representable min_prompt_tokens band to its meter entry."""
    if not bands:
        return None
    if len(bands) > 1:
        raise Untracked(f"{where}: {len(bands)} long-context bands are not modelled")
    band = bands[0]
    if set(band) & _UTC_KEYS:
        raise Untracked(f"{where}: a band and utc fields on one override is not modelled")
    unknown = set(band) - {*TOKEN_PRICED, MIN_PROMPT_TOKENS}
    if unknown:
        raise Untracked(f"{where}: band kind not modelled: {sorted(unknown)}")
    if not (isinstance(band.get("prompt"), str)
            and isinstance(band.get("completion"), str)):
        raise Untracked(f"{where}: the band does not restate input and output: not modelled")
    threshold = band_threshold(band, where)
    input_mult, output_mult = meter_shape_ok(
        rates_of(price, where), rates_of(band, where), where, band)
    return {"threshold": threshold, "input_mult": input_mult,
            "output_mult": output_mult}


class RefreshError(Exception):
    """Review required; appends nothing."""


class Untracked(RefreshError):
    """Unmodelled shape: provider host notice; actionable first-party shape refuses the vendor run."""


def _per_million(price: object, where: str) -> float:
    """Decimal USD/token to USD/million: 0.0000001275→0.1275."""
    try:
        value = Decimal(price) if isinstance(price, str) else None
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite() or value < 0:
        raise RefreshError(f"{where}: price {price!r} is not a non-negative decimal string")
    return float(value.scaleb(6))


def is_zero(value: object) -> bool:
    try:
        return not isinstance(value, bool) and Decimal(str(value)) == 0
    except InvalidOperation:
        return False


def _per_search(value: object, where: str) -> float:
    """USD per search."""
    if value is None:
        return 0.0
    try:
        if isinstance(value, str):
            amount = Decimal(value)
        elif (isinstance(value, (int, float))
              and not isinstance(value, bool)):
            amount = Decimal(str(value))
        else:
            amount = None
    except InvalidOperation:
        amount = None
    if amount is None or not amount.is_finite() or amount < 0:
        raise RefreshError(
            f"{where}: web_search {value!r} is not a nonnegative decimal string")
    return float(amount)


def rates_of(price: dict, where: str) -> dict:
    """Token/search rates: absent read=fresh; nonzero writes, else 5m=fresh and 1h=5m."""
    fresh = _per_million(price.get("prompt"), where)
    output = _per_million(price.get("completion"), where)
    read = _per_million(price["input_cache_read"], where) if "input_cache_read" in price else fresh
    write = (_per_million(price["input_cache_write"], where)
             if "input_cache_write" in price else 0.0)
    create = write or fresh
    write_1h = (_per_million(price["input_cache_write_1h"], where)
                if "input_cache_write_1h" in price else 0.0)
    rates = {"fresh": fresh, "create_5m": create,
             "create_1h": write_1h or create,
             "read": read, "output": output}
    search = _per_search(price.get("web_search"), where)
    if search:
        rates["web_search"] = search
    return rates


def token_rates_of(price: dict, where: str) -> dict:
    """Five price-log token rates."""
    rates = rates_of(price, where)
    return {field: rates[field] for field in (
        "fresh", "create_5m", "create_1h", "read", "output")}


def as_listed(rates: dict) -> dict:
    """rates_of inverse: five token rates round-trip."""
    price = {key: format(Decimal(repr(rates[f])).scaleb(-6).normalize(), "f")
             for key, f in (("prompt", "fresh"), ("completion", "output"),
                            ("input_cache_read", "read"))}
    if rates["create_5m"] != rates["fresh"]:
        price["input_cache_write"] = format(
            Decimal(repr(rates["create_5m"])).scaleb(-6).normalize(), "f")
    if rates["create_1h"] != rates["create_5m"]:
        price["input_cache_write_1h"] = format(
            Decimal(repr(rates["create_1h"])).scaleb(-6).normalize(), "f")
    return price


def entry_schedule(price: dict, where: str) -> list | None:
    """Weekly UTC overrides (utc_days, HHMM utc_start/end) inherit unnamed endpoint rates; bands add no provider windows/rates and independently set vendor threshold/factors."""
    overrides = price.get("overrides")
    if overrides is None or overrides == []:
        return None
    if not isinstance(overrides, list) or not all(isinstance(o, dict) for o in overrides):
        raise RefreshError(f"{where}: pricing.overrides is not a list of windows")
    schedule, bands = [], []
    for override in overrides:
        if MIN_PROMPT_TOKENS in override:
            bands.append(override)
            continue
        unknown = set(override) - _OVERRIDE_KEYS
        if unknown:
            raise RefreshError(f"{where}: override kind not modelled: {sorted(unknown)}")
        window_price = {**{k: price[k] for k in TOKEN_PRICED if k in price},
                        **{k: override[k] for k in TOKEN_PRICED if k in override}}
        window: dict = {"rates": token_rates_of(window_price, where)}
        if override.get("utc_days") is not None:
            window["days"] = override["utc_days"]
        if (override.get("utc_start") is not None
                or override.get("utc_end") is not None):
            window["start"] = override.get("utc_start")
            window["end"] = override.get("utc_end")
        schedule.append(window)
    metered_band(price, where, bands)
    if not schedule:
        return None
    try:
        pricing._schedule(schedule, where)  # pylint: disable=protected-access
    except ValueError as exc:
        raise RefreshError(f"{where}: pricing.overrides: {exc}") from exc
    return schedule
