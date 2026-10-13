"""Pure startup resolution for collection and feature switches."""
from __future__ import annotations

import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from backend.flag_registry import (
    COLLECTION_SPECS,
    FEATURE_SPECS,
    CollectionSpec,
    FeatureSpec,
)
from backend.hide_rate_limits import is_truthy


LEGACY_HIDE_RATE_LIMITS_ENV = "CLAUDIT_HIDE_RATE_LIMITS"
# The canonical tuple is shared by startup policies. Compare its keys in
# C rather than running a generator over every spec for each parsed file.
# Keep a tuple so membership retains equality semantics for unknown keys.
_COLLECTION_KEYS = tuple(spec.key for spec in COLLECTION_SPECS)


def _validate_specs(
    specs: tuple[CollectionSpec, ...] | tuple[FeatureSpec, ...],
    registry_name: str,
) -> None:
    keys: set[str] = set()
    envs: set[str] = set()
    for spec in specs:
        if spec.key in keys:
            raise ValueError(f"Duplicate {registry_name} key: {spec.key}")
        if spec.env in envs:
            raise ValueError(f"Duplicate {registry_name} env name: {spec.env}")
        keys.add(spec.key)
        envs.add(spec.env)


@dataclass(frozen=True)
class CollectionPolicy:
    """Immutable collection choices and their declarative field metadata."""

    disabled: frozenset[str]
    specs: tuple[CollectionSpec, ...]

    def __post_init__(self) -> None:
        specs = tuple(self.specs)
        disabled = frozenset(self.disabled)
        _validate_specs(specs, "collection")
        known = {spec.key for spec in specs}
        unknown = disabled - known
        if unknown:
            raise KeyError(f"Unknown collection keys: {', '.join(sorted(unknown))}")
        object.__setattr__(self, "specs", specs)
        object.__setattr__(self, "disabled", disabled)

    def enabled(self, key: str) -> bool:
        """Return whether a registered collection group is enabled."""
        known = (key in _COLLECTION_KEYS if self.specs is COLLECTION_SPECS else
                 any(spec.key == key for spec in self.specs))
        if not known:
            raise KeyError(f"Unknown collection key: {key}")
        return key not in self.disabled

    def stamp(self) -> tuple[str, ...]:
        """Return the stable, sorted disabled-key database stamp."""
        return tuple(sorted(self.disabled))


@dataclass(frozen=True)
class Config:
    """Effective process-start settings."""

    collection: CollectionPolicy
    features: Mapping[str, bool]
    legacy_hide_rate_limits: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", MappingProxyType(dict(self.features)))

    def disabled_features(self) -> tuple[str, ...]:
        """Return feature keys disabled by this process configuration."""
        return tuple(sorted(key for key, enabled in self.features.items() if not enabled))


ALL_COLLECTIONS_ON = CollectionPolicy(frozenset(), COLLECTION_SPECS)


def _is_enabled(env: Mapping[str, str], name: str, default: bool) -> bool:
    """Resolve an optional switch with the legacy display-flag grammar."""
    if name not in env:
        return default
    return is_truthy(env[name])


def resolve_config(
    env: Mapping[str, str],
    *,
    collection_specs: tuple[CollectionSpec, ...] = COLLECTION_SPECS,
    feature_specs: tuple[FeatureSpec, ...] = FEATURE_SPECS,
) -> Config:
    """Resolve one environment snapshot against the declarative registries."""
    collections = tuple(collection_specs)
    features = tuple(feature_specs)
    _validate_specs(collections, "collection")
    _validate_specs(features, "feature")

    disabled_collections = frozenset(
        spec.key for spec in collections
        if not _is_enabled(env, spec.env, True)
    )
    collection = CollectionPolicy(disabled_collections, collections)
    for spec in features:
        for collection_key in spec.collection_dependencies:
            collection.enabled(collection_key)

    legacy_hide = _is_enabled(env, LEGACY_HIDE_RATE_LIMITS_ENV, False)

    effective_features: dict[str, bool] = {}
    for spec in features:
        enabled = _is_enabled(env, spec.env, True)
        for collection_key in spec.collection_dependencies:
            enabled = enabled and collection.enabled(collection_key)
        if spec.key == "rate_limits" and legacy_hide:
            enabled = False
        effective_features[spec.key] = enabled

    return Config(collection, effective_features, legacy_hide)


CONFIG = resolve_config(os.environ)
