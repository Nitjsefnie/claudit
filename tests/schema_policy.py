"""The normal scratch schema uses the production compiler with all ON."""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from backend.flags import ALL_COLLECTIONS_ON
from backend.schema_indexes import compile_schema


def fixture_ddl(schema_paths: Iterable[Path]) -> str:
    static_sql = "\n".join(path.read_text(encoding="utf-8") for path in schema_paths)
    return compile_schema(static_sql, ALL_COLLECTIONS_ON)
