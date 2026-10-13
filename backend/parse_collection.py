"""Optional server collection: bind producers once and mask registered storage."""
from __future__ import annotations

from dataclasses import dataclass
from operator import itemgetter

from backend.flags import CollectionPolicy


@dataclass(frozen=True)
class ParseCollection:
    """Per-file producer choices; identities and billing are always retained."""

    turn_context: bool = True
    request_metadata: bool = True
    context_intake: bool = True
    dispatch_analytics: bool = True
    file_extras: bool = True
    error_text: bool = True


ALL_PARSE_COLLECTION = ParseCollection()
_MODEL = itemgetter('model')


def parse_collection(collection: CollectionPolicy) -> ParseCollection:
    """Bind the independent producers once, with a shared all-ON fast path."""
    disabled = collection.disabled
    if not disabled:
        return ALL_PARSE_COLLECTION
    return ParseCollection(
        turn_context='turn_context' not in disabled,
        request_metadata='request_metadata' not in disabled,
        context_intake='context_intake' not in disabled,
        dispatch_analytics='dispatch_analytics' not in disabled,
        file_extras='file_extras' not in disabled,
        error_text='error_text' not in disabled,
    )


def collected_models(records: list[dict]) -> list[str]:
    """All answering models, before ingest suppression; omit missing labels."""
    # Equivalent to {r['model'] for r in records}, but project the keys
    # in C: this helper runs once per file on every transcript format.
    return sorted(filter(None, set(map(_MODEL, records))))


def mask_collected_fields(parsed: dict, collection: CollectionPolicy) -> dict:
    """Set only declared disabled fields to None after retained decisions settle."""
    if not collection.disabled:
        return parsed
    record_fields, tool_fields, file_fields = [], [], []
    for spec in collection.specs:
        if spec.key in collection.disabled:
            record_fields.extend(spec.record_fields)
            tool_fields.extend(spec.tool_fields)
            file_fields.extend(spec.file_fields)
    for rows, fields in ((parsed['records'], record_fields),
                         (parsed['tool_uses'], tool_fields), ([parsed], file_fields)):
        if fields:
            for row in rows:
                for name in fields:
                    row[name] = None
    return parsed
