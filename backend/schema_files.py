"""The schema, as an ordered list of files (issue #436).

`backend/db.py` applies this at startup, under one content stamp and one
advisory lock. It lives here rather than there for the ordinary reason: the
module-size ratchet records `db.py` at 473 lines against a 500 production
ceiling, and the 28 lines this needs would cross it. The ratchet's own stated
remedy for growth is to relocate the code into a new module, and that is what
this is — the same remedy the schema split itself was.

`SCHEMA_PATH` stays exported from `db` as well, because it is the name the
existing startup tests reach for.
"""
from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve().parent

# backend/schema.sql, resolved next to this module so the working directory
# the service was started from does not matter.
SCHEMA_PATH = _HERE / "schema.sql"

# The schema as an ORDERED list, all read and concatenated into one script.
# Splitting is not a preference: the module-size ratchet records
# `schema.sql` at exactly its line count, a recorded size it never raises and
# never seeds again for an existing family, so a table added there fails the
# gate permanently. `web_metrics` went out first; `schema.sql` is otherwise
# untouched and stays FIRST, because its statements are the ones every later
# file's ALTERs sit on top of.
# schema_dims.sql (issue #959) and the type compaction that follows it
# follow the additive files: its guarded drop-and-recreate of records/tool_uses and every
# rollup has to see every earlier file's ADD COLUMN land on the old shape
# first, and it converges any retired text-dimension column a database
# still carries; schema_types.sql then converges
# the column types of a database already in the surrogate shape — after
# the shape file it converges; schema_records_price.sql follows those,
# so its guard sees records in whatever shape the earlier files left it
# (pre-split in-row, or already split) and moves/stamps accordingly.
# Order within one transaction does not change atomicity — the whole
# list is still one script, one lock set, one stamp.
# schema_timelines.sql follows the identity conversions and converges
# the file timeline columns and latency outlier arrays in place.
SCHEMA_PATHS = (SCHEMA_PATH, _HERE / "schema_web_metrics.sql",
                _HERE / "schema_request_fee.sql",
                _HERE / "schema_replay.sql",
                _HERE / "schema_web_search.sql",
                _HERE / "schema_cache_write.sql",
                _HERE / "schema_collection.sql",
                # schema_widths.sql (issue #959 task 7) drops a derived
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
