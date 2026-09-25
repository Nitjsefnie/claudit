"""Password auth helpers.

Operates on a plain dict (the user's `config` JSONB column from the
auth DB) — no ORM or external user-state dependencies. Passwords are
PBKDF2-SHA256 with a per-user hex salt.

The stored hash (`config` key "web_password_hash") has one of two
formats:

- A BARE HEX DIGEST is the legacy format: the iteration count is
  implicit — always PBKDF2_ITERATIONS (200,000) — and the salt is the
  "web_password_salt" value. These verify exactly as they always have,
  so a hash written by the external user-management process keeps
  verifying byte-for-byte.
- ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>`` is the
  versioned format: the string carries its own iteration count and
  salt, so a writer can raise the cost without a code change here.
  New writes use it at PBKDF2_WRITE_ITERATIONS (600,000, current
  OWASP guidance); the salt keys stay populated with the same salt so
  `has_web_password` keeps meaning the same thing.

An external writer may produce either format.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

WEB_PASSWORD_HASH_KEY = "web_password_hash"
WEB_PASSWORD_SALT_KEY = "web_password_salt"
#: Legacy verification count — implicit for a bare-hex stored hash.
PBKDF2_ITERATIONS = 200_000
#: Iteration count for NEW writes; the versioned format carries it.
PBKDF2_WRITE_ITERATIONS = 600_000
_HASH_SCHEME = "pbkdf2_sha256"


def has_web_password(config: dict) -> bool:
    return bool(
        config.get(WEB_PASSWORD_HASH_KEY)
        and config.get(WEB_PASSWORD_SALT_KEY)
    )


def pbkdf2(password: str, salt_hex: str, iterations: int) -> str:
    """Hex digest of `password` under PBKDF2-SHA256 at `iterations`."""
    salt = bytes.fromhex(salt_hex)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return digest.hex()


def set_web_password(config: dict, password: str) -> None:
    """Write a versioned hash at PBKDF2_WRITE_ITERATIONS.

    The versioned string embeds the salt and count, so verification
    never has to guess them; the bare keys stay populated (the salt
    key with the same salt) for `has_web_password`.
    """
    salt_hex = secrets.token_hex(16)
    digest_hex = pbkdf2(password, salt_hex, PBKDF2_WRITE_ITERATIONS)
    config[WEB_PASSWORD_SALT_KEY] = salt_hex
    config[WEB_PASSWORD_HASH_KEY] = _format_versioned(
        PBKDF2_WRITE_ITERATIONS, salt_hex, digest_hex
    )


def _format_versioned(iterations: int, salt_hex: str, hash_hex: str) -> str:
    return f"{_HASH_SCHEME}${iterations}${salt_hex}${hash_hex}"


def _parse_versioned(stored: str) -> tuple[int, str, str] | None:
    """Parse ``<scheme>$<iterations>$<salt_hex>$<hash_hex>``.

    Returns (iterations, salt_hex, hash_hex), or None when the string
    is malformed — wrong scheme, a count that is not a positive
    integer, non-hex or empty salt/hash parts. A malformed versioned
    string is a verification failure, never an exception.
    """
    parts = stored.split("$")
    if len(parts) != 4:
        return None
    scheme, count_text, salt_hex, hash_hex = parts
    if scheme != _HASH_SCHEME:
        return None
    try:
        iterations = int(count_text)
        bytes.fromhex(salt_hex)
        bytes.fromhex(hash_hex)
    except ValueError:
        return None
    if iterations <= 0 or not salt_hex or not hash_hex:
        return None
    return iterations, salt_hex, hash_hex


def verify_web_password(config: dict, password: str) -> bool:
    """Verify `password` against the stored hash in either format.

    Bare hex runs at the legacy PBKDF2_ITERATIONS; a versioned string
    runs at the count it carries. Malformed versioned strings fail
    verification. The comparison is constant-time either way.
    """
    stored_hash = config.get(WEB_PASSWORD_HASH_KEY)
    stored_salt = config.get(WEB_PASSWORD_SALT_KEY)
    if not stored_hash or not stored_salt:
        return False
    if stored_hash.startswith(_HASH_SCHEME + "$"):
        parsed = _parse_versioned(stored_hash)
        if parsed is None:
            return False
        iterations, salt_hex, expected = parsed
    else:
        iterations, salt_hex, expected = (
            PBKDF2_ITERATIONS, stored_salt, stored_hash,
        )
    candidate = pbkdf2(password, salt_hex, iterations)
    return hmac.compare_digest(candidate, expected)


def stored_verification_iterations(config: dict) -> int:
    """The iteration count `verify_web_password` would spend on this
    stored config: the count a versioned string carries, the legacy
    count for bare hex, and 0 when nothing would run at all (a missing
    hash/salt pair, or a malformed versioned string, which fails
    before any PBKDF2). Mirrors the format branch of
    `verify_web_password` — the login flow passes this count back as
    what a failed real verification already spent.
    """
    stored_hash = config.get(WEB_PASSWORD_HASH_KEY)
    stored_salt = config.get(WEB_PASSWORD_SALT_KEY)
    if not stored_hash or not stored_salt:
        return 0
    if stored_hash.startswith(_HASH_SCHEME + "$"):
        parsed = _parse_versioned(stored_hash)
        return parsed[0] if parsed else 0
    return PBKDF2_ITERATIONS


# Fixed verification target for the login flow's timing flattening
# (issue #109): every credential failure costs about one PBKDF2 run at
# PBKDF2_WRITE_ITERATIONS — the real verification where it can run,
# plus a dummy remainder run where it cannot, or would run cheaper (a
# legacy 200k hash, a malformed stored hash, no stored hash at all).
# The dummy compares a candidate for the submitted password against a
# reference hash of a fixed password at the same count; references are
# computed lazily and memoized per distinct count (the target itself
# is seeded below at import), so steady state pays one candidate run
# per failed login. Side effect: every failed login attempt burns this
# much CPU, which slows brute force — intended.
_DUMMY_PASSWORD = "claudit dummy verification target"
_DUMMY_SALT_HEX = "9f1c3b7e2a5d48069be4c1f0a7d3e5b2"
_DUMMY_REFERENCE_HASHES: dict[int, str] = {
    PBKDF2_WRITE_ITERATIONS: pbkdf2(
        _DUMMY_PASSWORD, _DUMMY_SALT_HEX, PBKDF2_WRITE_ITERATIONS
    ),
}


def _reference_hash(iterations: int) -> str:
    """The dummy target's reference hash at `iterations`, computed once
    per distinct count and memoized module-level."""
    cached = _DUMMY_REFERENCE_HASHES.get(iterations)
    if cached is None:
        cached = pbkdf2(_DUMMY_PASSWORD, _DUMMY_SALT_HEX, iterations)
        _DUMMY_REFERENCE_HASHES[iterations] = cached
    return cached


def normalize_verification_timing(password: str, spent_iterations: int) -> None:
    """Bring a failed login's verification cost to about one PBKDF2 run
    at the target count (PBKDF2_WRITE_ITERATIONS).

    Runs a dummy verification of `password` for the remainder — target
    minus what the real verification already spent — against the
    memoized reference hash at that same count, and discards both.
    Skipped when nothing remains, so a hash already stored at the
    target count pays the real run only. The login flow calls this on
    every credential-failure path, so a stored hash's format cannot be
    read off the response timing (issue #109). One documented residual:
    a hash versioned ABOVE the target count still costs longer.
    """
    remaining = PBKDF2_WRITE_ITERATIONS - spent_iterations
    if remaining <= 0:
        return
    candidate = pbkdf2(password, _DUMMY_SALT_HEX, remaining)
    hmac.compare_digest(candidate, _reference_hash(remaining))
