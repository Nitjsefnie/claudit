"""Narrow decoded JSON values to the container shape a parser needs."""


def as_dict(value: object) -> dict:
    """Return a decoded object, or an empty object for any other shape."""
    return value if isinstance(value, dict) else {}


def as_list(value: object) -> list:
    """Return a decoded array, or an empty array for any other shape."""
    return value if isinstance(value, list) else []


def dict_list(value: object) -> list[dict]:
    """Return only object entries from a decoded array-shaped value."""
    return [item for item in as_list(value) if isinstance(item, dict)]
