"""Prefixed configuration and deprecated names retained for one release."""
from __future__ import annotations

import logging
import os
from typing import overload

# Only names read by the v0.4.0 release belong here. Remove these fallbacks
# after the first release shipping the prefixed names.
DEPRECATED_ENV_FALLBACKS = {
    "ADMIN_TOKEN": "CLAUDIT_ADMIN_TOKEN",
    "COOKIE_SECURE": "CLAUDIT_COOKIE_SECURE",
    "DATABASE_URL_VIZ": "CLAUDIT_DATABASE_URL_VIZ",
    "DATABASE_URL_AUTH": "CLAUDIT_DATABASE_URL_AUTH",
    "EXPORT_PYTHON": "CLAUDIT_EXPORT_PYTHON",
    "INGEST_WORKERS": "CLAUDIT_INGEST_WORKERS",
    "INGEST_PARSE_PROCESSES": "CLAUDIT_INGEST_PARSE_PROCESSES",
    "INGEST_PERSIST_THREADS": "CLAUDIT_INGEST_PERSIST_THREADS",
    "R2_ENDPOINT": "CLAUDIT_R2_ENDPOINT",
    "R2_BUCKET": "CLAUDIT_R2_BUCKET",
    "R2_ACCESS_KEY_ID": "CLAUDIT_R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY": "CLAUDIT_R2_SECRET_ACCESS_KEY",
    "APP_NAME": "CLAUDIT_APP_NAME",
    "APP_TITLE": "CLAUDIT_APP_TITLE",
    "APP_DESCRIPTION": "CLAUDIT_APP_DESCRIPTION",
    "APP_PRIVACY_NOTICE_URL": "CLAUDIT_APP_PRIVACY_NOTICE_URL",
}
_LEGACY_NAMES = {new: old for old, new in DEPRECATED_ENV_FALLBACKS.items()}
_used_deprecated: set[str] = set()


@overload
def getenv(name: str, default: str) -> str: ...


@overload
def getenv(name: str, default: None = None) -> str | None: ...


def getenv(name: str, default: str | None = None) -> str | None:
    """Prefer the prefixed name, including empty values; record fallback use."""
    if not name.startswith("CLAUDIT_"):
        raise ValueError(f"Configuration name must start with CLAUDIT_: {name}")
    if name in os.environ:
        return os.environ[name]
    legacy = _LEGACY_NAMES.get(name)
    if legacy is not None and legacy in os.environ:
        _used_deprecated.add(legacy)
        return os.environ[legacy]
    return default


def require_env(name: str) -> str:
    """Preserve required-setting reads' KeyError instead of a driver default."""
    value = getenv(name)
    if value is None:
        raise KeyError(name)
    return value


def warn_deprecated_env(logger: logging.Logger) -> None:
    """Called once per startup; report active fallbacks, never their values.

    Include lazy settings not yet read so their deployment sees the warning
    at startup too. Recorded reads cover import-time snapshots. Old names
    shadowed by prefixed names are not reported.
    """
    active = {old for old, new in DEPRECATED_ENV_FALLBACKS.items()
              if old in os.environ and new not in os.environ}
    active.update(old for old in _used_deprecated
                  if DEPRECATED_ENV_FALLBACKS[old] not in os.environ)
    _used_deprecated.clear()
    if active:
        logger.warning(
            "Deprecated configuration names (supported for one release): %s; "
            "rename to %s",
            ", ".join(sorted(active)),
            ", ".join(DEPRECATED_ENV_FALLBACKS[old] for old in sorted(active)),
        )


def getenv_with_dotenv(name: str, path: str) -> str | None:
    """Prefer the new spelling across sources; empty old env falls through."""
    if not name.startswith("CLAUDIT_"):
        raise ValueError(f"Configuration name must start with CLAUDIT_: {name}")
    if name in os.environ:
        return getenv(name)
    try:
        values = read_dotenv(path, strip_quotes=True)
    except OSError:
        values = {}
    if name in values:
        return values[name]
    legacy = _LEGACY_NAMES.get(name)
    if legacy is not None:
        value = os.environ.get(legacy) or values.get(legacy)
        if value is not None:
            _used_deprecated.add(legacy)
            return value
    return None


def load_dotenv(path: str = ".env", *, strip_quotes: bool = False) -> None:
    """Tiny dotenv loader. Avoids the python-dotenv dependency.

    Constraints (intentional simplicity — keep values plain):
    - Values are literal by default: CLAUDIT_ADMIN_TOKEN="abc" stores the
      quotes. strip_quotes=True keeps the plot CLI's quote-stripping.
    - The 'export ' prefix is NOT supported (the line key would become
      'export CLAUDIT_ADMIN_TOKEN', not 'CLAUDIT_ADMIN_TOKEN').
    - The first '=' splits key from value, so values may contain '=' freely.
    - Existing env vars are NEVER overwritten (uses os.environ.setdefault).
    - Comment lines (starting with '#') and blank lines are skipped.
    """
    for name, value in read_dotenv(path, strip_quotes=strip_quotes).items():
        os.environ.setdefault(name, value)


def read_dotenv(path: str, *, strip_quotes: bool = False) -> dict[str, str]:
    """Read literal first-key-wins values without changing process settings."""
    values: dict[str, str] = {}
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            value = v.strip()
            if strip_quotes:
                value = value.strip('"').strip("'")
            values.setdefault(k.strip(), value)
    return values
