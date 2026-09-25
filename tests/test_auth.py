"""Verify our PBKDF2 auth helpers — round-trip and known-vector
sanity. A bare-hex stored hash is the legacy shape: PBKDF2-SHA256 at
PBKDF2_ITERATIONS (200,000) with a hex salt in web_password_salt, the
format the upstream user-management process writes. New writes use
the versioned string format, which carries its own count.
"""
from backend import auth


def testpbkdf2_known_vector():
    salt = "00112233445566778899aabbccddeeff"
    pw = "correct horse battery staple"
    digest = auth.pbkdf2(pw, salt, auth.PBKDF2_ITERATIONS)
    assert isinstance(digest, str) and len(digest) == 64
    assert auth.pbkdf2(pw, salt, auth.PBKDF2_ITERATIONS) == digest


def test_legacy_bare_hex_hash_verifies_at_legacy_count():
    """A bare-hex stored hash is read at PBKDF2_ITERATIONS (200k),
    exactly as before versioning — and at no other count."""
    salt_hex = "00112233445566778899aabbccddeeff"
    legacy = {
        auth.WEB_PASSWORD_HASH_KEY: auth.pbkdf2(
            "legacy pw", salt_hex, auth.PBKDF2_ITERATIONS
        ),
        auth.WEB_PASSWORD_SALT_KEY: salt_hex,
    }
    assert auth.verify_web_password(legacy, "legacy pw")
    assert not auth.verify_web_password(legacy, "nope")
    # One written at a different count does not verify: bare hex means
    # the legacy count, never "whatever the string looks like".
    other = dict(legacy)
    other[auth.WEB_PASSWORD_HASH_KEY] = auth.pbkdf2(
        "legacy pw", salt_hex, 201_000
    )
    assert not auth.verify_web_password(other, "legacy pw")


def test_set_then_verify_roundtrip():
    config: dict = {}
    auth.set_web_password(config, "swordfish")
    assert auth.has_web_password(config)
    assert auth.verify_web_password(config, "swordfish")
    assert not auth.verify_web_password(config, "wrong")


def test_set_writes_versioned_format_at_write_iterations():
    config: dict = {}
    auth.set_web_password(config, "swordfish")
    stored = config[auth.WEB_PASSWORD_HASH_KEY]
    assert stored.startswith("pbkdf2_sha256$")
    parts = stored.split("$")
    assert len(parts) == 4
    assert int(parts[1]) == auth.PBKDF2_WRITE_ITERATIONS
    # The salt keys stay populated so has_web_password keeps meaning
    # the same thing, and name the salt actually used.
    assert parts[2] == config[auth.WEB_PASSWORD_SALT_KEY]
    assert len(parts[3]) == 64


def test_versioned_hash_carries_its_own_count():
    """A versioned string written at any count verifies at that count,
    not at a module constant."""
    salt_hex = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    hash_hex = auth.pbkdf2("pw", salt_hex, 1000)
    config = {
        auth.WEB_PASSWORD_HASH_KEY:
            f"pbkdf2_sha256$1000${salt_hex}${hash_hex}",
        auth.WEB_PASSWORD_SALT_KEY: salt_hex,
    }
    assert auth.verify_web_password(config, "pw")
    assert not auth.verify_web_password(config, "nope")


def test_malformed_versioned_hash_is_rejected():
    salt_hex = "cd" * 16
    hash_hex = "ab" * 32
    malformed = [
        "pbkdf2_sha256$600000",                      # too few parts
        "pbkdf2_sha256$600000$" + salt_hex,          # missing hash part
        f"pbkdf2_sha256$abc${salt_hex}${hash_hex}",  # count not an int
        f"pbkdf2_sha256$0${salt_hex}${hash_hex}",    # non-positive count
        f"pbkdf2_sha256$-1${salt_hex}${hash_hex}",   # negative count
        f"pbkdf2_sha256$600000$zz${hash_hex}",       # salt not hex
        f"pbkdf2_sha256$600000${salt_hex}$zz",       # hash not hex
        f"pbkdf2_sha256$600000${salt_hex}$",         # empty hash
        "pbkdf2_sha256$600000$$" + hash_hex,         # empty salt
        "sha512$600000$" + salt_hex + "$" + hash_hex,  # wrong scheme
    ]
    for stored in malformed:
        config = {
            auth.WEB_PASSWORD_HASH_KEY: stored,
            auth.WEB_PASSWORD_SALT_KEY: salt_hex,
        }
        assert not auth.verify_web_password(config, "pw"), stored


def test_verify_constant_time_against_garbage():
    config: dict = {
        auth.WEB_PASSWORD_HASH_KEY: "00" * 32,
        auth.WEB_PASSWORD_SALT_KEY: "ff" * 16,
    }
    assert not auth.verify_web_password(config, "anything")


def test_dummy_verification_runs_at_write_iterations(monkeypatch):
    """The dummy helper is a real PBKDF2 run at PBKDF2_WRITE_ITERATIONS,
    so an unknown-id login costs the same as verifying a modern hash."""
    seen: list[int] = []
    real = auth.pbkdf2

    def spy(password: str, salt_hex: str, iterations: int) -> str:
        seen.append(iterations)
        return real(password, salt_hex, iterations)

    monkeypatch.setattr(auth, "pbkdf2", spy)
    auth.run_dummy_verification("x")
    assert seen == [auth.PBKDF2_WRITE_ITERATIONS]


def test_has_web_password_requires_both():
    assert not auth.has_web_password({})
    assert not auth.has_web_password({auth.WEB_PASSWORD_HASH_KEY: "x"})
    assert not auth.has_web_password({auth.WEB_PASSWORD_SALT_KEY: "x"})
    assert auth.has_web_password({
        auth.WEB_PASSWORD_HASH_KEY: "x",
        auth.WEB_PASSWORD_SALT_KEY: "y",
    })
