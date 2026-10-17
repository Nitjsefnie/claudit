"""Pure model-id normalization and suffix/free-shape grammar (SV-RATE-DATA).

Pricing resolvers share these spellings and pricing re-exports them.
Read no rate tables here."""
from __future__ import annotations

import re
from functools import lru_cache

# The grammar ``pricing._latest`` matches: the highest-versioned key of a
# family. ``claude-opus-5-5`` is version (5, 5); legacy ``claude-3-opus-``
# keys do not match.
_VERSIONED_KEY = re.compile(r"^claude-([a-z]+)-(\d+(?:-\d+)*)$")

# A YYYYMMDD snapshot suffix preserves model identity; a short version
# suffix (-9) or mode suffix (-fast) names a different model.
_SNAPSHOT_SUFFIX = re.compile(r"^-?\d{6,8}$")

# An OpenRouter permaslug's trailing YYYYMMDD names the same model as
# its short MMDD slug; normalize the year away only for that lookup.
_PERMASLUG_DATE = re.compile(r"-20\d{2}(\d{4})$")

# OpenRouter's variant suffix (":nitro", ":floor") names a service tier,
# not a price: the tiered id is the bare model at the bare model's price.
# Only ":free" changes price (zero), and resolve() prices it before any
# provider lookup — the guard here keeps a direct caller honest too.
_VARIANT_SUFFIX = re.compile(r":([^:]*)$")


def _normalise(model: str | None) -> str:
    """Strip provider/region prefixes and normalise version separators.

    ``anthropic/claude-opus-4.8`` and ``us.anthropic.claude-opus-4-8``
    both denote the same model as ``claude-opus-4-8``.
    """
    return _normalise_id((model or "").strip())


@lru_cache(maxsize=1024)
def _normalise_id(m: str) -> str:
    """Fold case, routing and separators for one stripped id."""
    m = m.lower()
    if not m:
        return ""
    # Everything before the first "claude" is provider/region routing.
    i = m.find("claude")
    if i > 0:
        m = m[i:]
    return m.replace(".", "-")


def _is_free(model: str | None, norm: str) -> bool:
    """True for an OpenRouter free model: an id ending in ``:free`` or
    starting with ``stealth/``, case-insensitively.

    Checked on the raw id as well as its normalised form because
    ``_normalise`` strips everything before ``claude`` — a
    ``stealth/claude-…`` id loses that prefix in ``norm`` and only the
    raw check still sees it. (The ``:free`` suffix survives every
    normalisation step; the raw check covers it symmetrically.)
    """
    raw = (model or "").strip().lower()
    return (norm.endswith(":free") or norm.startswith("stealth/")
            or raw.endswith(":free") or raw.startswith("stealth/"))


def _variant_folded(norm: str) -> str:
    """`norm` without ONE trailing ":<suffix>", when that suffix is not
    "free" (case-insensitively); `norm` itself otherwise. Mirrored by
    parser.js's _providerModelKey."""
    m = _VARIANT_SUFFIX.search(norm)
    if m is None or m.group(1).lower() == "free":
        return norm
    return norm[: m.start()]
