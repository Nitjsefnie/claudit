"""Rate-change instants for one model/provider pricing resolution."""
from __future__ import annotations

from datetime import datetime, timezone

from backend import pricing


def _as_utc(value: datetime) -> datetime:
    """Normalize a stored instant before ordering or returning it."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def rate_boundaries(model: str, provider: str | None) -> list[datetime]:
    """Return the UTC rate transitions for this model/provider pair.

    The resolver is authoritative for free ids, model-key matching and the
    ordered provider-id folds. When a provider row has a start, model-only
    dated windows still matter before it; after that start the provider row
    takes precedence. Weekly schedules repeat and therefore add no boundary.
    """
    norm = pricing._normalise(model)  # pylint: disable=protected-access
    if pricing._is_free(model, norm):  # pylint: disable=protected-access
        return []

    provider_key = (pricing._provider_key(  # pylint: disable=protected-access
        norm, provider, None) if provider else None)
    if provider_key is not None:
        boundaries = {
            _as_utc(end)
            for end, _ in pricing.PROVIDER_DATED_RATES.get(provider_key, ())
        }
        start = pricing.PROVIDER_STARTS.get(provider_key)
        if start is not None:
            start_utc = _as_utc(start)
            boundaries.add(start_utc)
            model_key = pricing._match_key(norm)  # pylint: disable=protected-access
            if model_key is not None:
                boundaries.update(
                    _as_utc(end)
                    for end, _ in pricing.DATED_RATES.get(model_key, ())
                    if _as_utc(end) < start_utc
                )
        return sorted(boundaries)

    model_key = pricing._match_key(norm)  # pylint: disable=protected-access
    if model_key is None:
        return []
    return sorted({
        _as_utc(end) for end, _ in pricing.DATED_RATES.get(model_key, ())
    })
