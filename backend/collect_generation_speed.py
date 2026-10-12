"""The CLAUDIT_COLLECT_GENERATION_SPEED collection flag (issue #955).

Read ONCE at import — backend startup. Unset or empty enables collection;
0/false/no disables it. Disabled parses store NULL for both generation
columns without changing any other parsed value.
"""
from __future__ import annotations

import os

COLLECT_GENERATION_SPEED = os.environ.get(
    "CLAUDIT_COLLECT_GENERATION_SPEED", "").lower() not in ("0", "false", "no")
