"""backend.dims' table-name allow-set.

`dim_ids` interpolates its table name into the upsert, so the name is
checked against the module's fixed allow-set first: a caller passing
anything but one of schema's dim lookup tables is refused in Python,
before a connection or SQL ever sees it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend import dims


def test_an_unknown_table_name_is_refused_before_any_sql():
    """The refusal is the module's own ValueError, not the database's
    UndefinedTable: the guard runs before the connection is touched, so
    even a bare object answers as the connection."""
    fake_conn: Any = object()
    with pytest.raises(ValueError, match="not_a_dim_table"):
        dims.dim_ids(fake_conn, "not_a_dim_table", ["x"])
    # ..and before the empty-values shortcut too: the NAME is what is
    # being validated, whatever the value list holds.
    with pytest.raises(ValueError, match="not_a_dim_table"):
        dims.dim_ids(fake_conn, "not_a_dim_table", [])


def test_the_allow_set_is_the_schema_s_dim_lookup_tables():
    """Drift tripwire: every dim lookup table schema_dims.sql creates is
    a legal argument, and nothing else is."""
    schema = Path(dims.__file__).with_name("schema_dims.sql").read_text(
        encoding="utf-8")
    created = {
        line.split()[5].rstrip(" (")
        for line in schema.splitlines()
        if line.startswith("CREATE TABLE IF NOT EXISTS dim_")
    }
    assert created == set(dims._DIM_TABLES), (  # pylint: disable=protected-access
        "the allow-set and the schema's dim tables have drifted apart")
