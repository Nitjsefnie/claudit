"""Call-time, fail-closed configuration for guest login."""
from __future__ import annotations

from backend.env_config import getenv

GUEST_LOGIN_ENV = "CLAUDIT_GUEST_LOGIN"


def guest_login_enabled() -> bool:
    """Default to enabled; only empty values and explicit opt-ins enable it."""
    value = getenv(GUEST_LOGIN_ENV, "").strip().lower()
    return value in {"", "1", "true", "yes", "on"}
