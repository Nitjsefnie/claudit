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
SCHEMA_PATHS = (SCHEMA_PATH, _HERE / "schema_web_metrics.sql",
                _HERE / "schema_request_fee.sql",
                _HERE / "schema_replay.sql")


def read_schema() -> str:
    """Every schema file's DDL, in order, as one script.

    Concatenated rather than executed file by file so the whole schema —
    including the trailing file — is one transaction, one advisory lock and
    one stamp. A separate transaction per file would let a crash between them
    leave a database stamped as current with a table missing, which is the
    exact half-applied state SV-SCHEMA-AUTOAPPLY exists to prevent.
    """
    return "\n".join(path.read_text(encoding="utf-8") for path in SCHEMA_PATHS)
