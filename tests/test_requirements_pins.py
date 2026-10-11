"""Transitive security floors pinned in backend/requirements.txt (issue #938).

urllib3 and anyio are TRANSITIVE here — urllib3 arrives under botocore,
anyio under starlette / httpx / httpx2 — so an advisory against either is
invisible to the repo's watchers: audit.yml's pip-audit and Dependabot's
pip updates both read ``backend/requirements.txt``, and a package named
nowhere in it is never bumped. Pinning the advisory fix floor in that
file brings both under the same watch as every direct dependency.

The floors are the fix versions of the advisories the served venv
carried (issue #938):

- urllib3 2.6.3 — PYSEC-2026-141, PYSEC-2026-142 (fixed 2.7.0),
  PYSEC-2026-4175, PYSEC-2026-4176, PYSEC-2026-4177 (fixed 2.8.0);
  the binding floor is therefore 2.8.0.
- anyio 4.13.0 — PYSEC-2026-4024, PYSEC-2026-4025 (fixed 4.14.2).

Specifiers are parsed as PEP 440 (``packaging``), never substring-matched:
the assertion is about the lowest version a specifier ADMITS, which a
substring cannot see — ``<3`` admits every old vulnerable release while
looking perfectly versioned.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_PATH = REPO_ROOT / "backend" / "requirements.txt"

# The lowest version clearing every listed advisory for the package.
FIXED_FLOORS: dict[str, Version] = {
    "urllib3": Version("2.8.0"),
    "anyio": Version("4.14.2"),
}

# Operators whose right-hand side bounds the specifier FROM BELOW.
# ``~=2.8.0`` means >=2.8.0,==2.* and ``==``/``===`` pin exactly, so all
# three establish a floor; a specifier built only from upper bounds
# (``<3``) or nothing at all admits every old vulnerable release.
_LOWER_BOUND_OPS = frozenset({">=", ">", "==", "===", "~="})


def _pins(path: Path) -> dict[str, Requirement]:
    """Requirement lines of a requirements file, keyed by canonical name.

    Line-based reader for this file's pinned shape and fails loudly on
    anything it cannot read: an include or option line (a floor cannot
    be audited through an ``-r`` hop) and an unparsable requirement both
    raise, never silently drop — a dropped line would turn the absence
    of a pin into a passing audit.
    """
    pins: dict[str, Requirement] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            raise AssertionError(
                f"{path.name}:{lineno}: options/includes hide pins from "
                f"this audit: {line!r}")
        try:
            req = Requirement(line)
        except InvalidRequirement as exc:
            raise AssertionError(
                f"{path.name}:{lineno}: unparsable requirement {line!r}: "
                f"{exc}") from exc
        pins[canonicalize_name(req.name)] = req
    return pins


def _floor(spec: SpecifierSet) -> Version | None:
    """The lowest version ``spec`` admits, or None with no lower bound.

    The floor is the MAXIMUM of the lower-bound operators' versions —
    ``>=2.7.0,<3`` floors at 2.7.0, whatever its ceiling. For an
    exclusive ``>`` the stated version is used as a conservative stand-in:
    it can only UNDERSTATE the floor, so a stale floor is reported stale,
    never mistaken for fixed.
    """
    floors = [Version(s.version) for s in spec if s.operator in _LOWER_BOUND_OPS]
    return max(floors) if floors else None


@pytest.mark.parametrize("package", sorted(FIXED_FLOORS))
def test_transitive_advisory_floor_is_pinned(package: str) -> None:
    pins = _pins(REQUIREMENTS_PATH)
    assert package in pins, (
        f"{package} must be pinned in backend/requirements.txt: it is "
        f"transitive, so audit.yml's pip-audit and Dependabot never see "
        f"it unless this file names it (issue #938)")

    spec = pins[package].specifier
    floor = _floor(spec)
    assert floor is not None, (
        f"{package}!={spec or 'any'} carries no lower-bound operator, so "
        f"every older vulnerable release still satisfies it")
    assert floor >= FIXED_FLOORS[package], (
        f"{package}!={spec} floors at {floor}, below the advisory fix "
        f"{FIXED_FLOORS[package]} (issue #938)")
