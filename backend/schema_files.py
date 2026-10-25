"""Ordered schema files applied by db at startup under one stamp and advisory lock.

SCHEMA_PATH remains exported by db for existing startup callers.
"""
from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve().parent

# backend/schema.sql, resolved next to this module so the working directory
# the service was started from does not matter.
SCHEMA_PATH = _HERE / "schema.sql"

# Apply ordered schema files as one script, lock set and content stamp.
#
# The size-ratchet action requires additions in separate files; schema.sql
# stays first because later ALTERs depend on it. Run additive files before
# schema_dims' guarded fact/rollup rebuild, so all columns exist on the
# shape being converted. schema_types then converges surrogate column
# types; schema_records_price handles either in-row or split price storage.
# schema_timelines follows identity conversions and converges file timelines
# and latency outlier arrays. Ordering does not weaken transaction atomicity.
SCHEMA_PATHS = (SCHEMA_PATH, _HERE / "schema_web_metrics.sql",
                _HERE / "schema_request_fee.sql",
                _HERE / "schema_replay.sql",
                _HERE / "schema_web_search.sql",
                _HERE / "schema_cache_write.sql",
                _HERE / "schema_collection.sql",
                # schema_widths.sql drops a derived
                # table whose signature is not canonical — it must run
                # before schema_dims.sql's CREATE rebuilds it, and after
                # every file that adds columns to the old shape.
                _HERE / "schema_widths.sql",
                _HERE / "schema_dims.sql",
                _HERE / "schema_types.sql",
                _HERE / "schema_records_price.sql",
                _HERE / "schema_timelines.sql")


def read_schema() -> str:
    """Every schema file's DDL, in order, as one script.

    Concatenated rather than executed file by file so the whole schema —
    including the trailing file — is one transaction, one advisory lock and
    one stamp. A separate transaction per file would let a crash between them
    leave a database stamped as current with a table missing, which is the
    exact half-applied state SV-SCHEMA-AUTOAPPLY exists to prevent.
    """
    return "\n".join(path.read_text(encoding="utf-8") for path in SCHEMA_PATHS)
