"""The model-id spellings the pricing family shares (SV-RATE-DATA).

Normalisation, the free/stealth shape, and the suffix grammars the
resolvers read: one id's spelling means one model across the family
(``backend/pricing.py`` re-exports every name here, so existing
``pricing._normalise``-style readers keep working). Pure functions over
the id text — no rate table is read here.
"""
from __future__ import annotations

import re

# The grammar ``pricing._latest`` matches: the highest-versioned key of a
# family. ``claude-opus-5-5`` is version (5, 5); legacy ``claude-3-opus-``
# keys do not match.
_VERSIONED_KEY = re.compile(r"^claude-([a-z]+)-(\d+(?:-\d+)*)$")

# A dated snapshot suffix ("-20250514") is the same model; a short version
# suffix ("-9") or a mode suffix ("-fast") is a DIFFERENT model.
_SNAPSHOT_SUFFIX = re.compile(r"^-?\d{6,8}$")

# OpenRouter's dated permaslug ("deepseek/deepseek-v4-flash-20260731") names
# the same model as its short slug ("deepseek/deepseek-v4-flash-0731").
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
    m = (model or "").strip().lower()
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
