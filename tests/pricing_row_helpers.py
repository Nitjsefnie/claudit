from __future__ import annotations

from backend.pricing_rows import decode_pricing_doc


def decoded_pricing_doc(doc: dict) -> dict:
    return decode_pricing_doc(doc) if "_format" in doc else doc
