"""Narrow decoded JSON values to the container shape a parser needs."""
from __future__ import annotations


def as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def dict_list(value: object) -> list[dict]:
    return [item for item in as_list(value) if isinstance(item, dict)]
