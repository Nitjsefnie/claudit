"""The CLAUDIT_HIDE_RATE_LIMITS display flag (issue #943).

Read ONCE at import — backend startup — the way timing.py reads
CLAUDIT_TIMING: env is deploy config, not per-request state. It gates
no tracking: the dashboard still queries and folds rate_limit_hits into
every payload; whether the UI SHOWS those hits is the frontend's
decision, keyed off the `hide_rate_limits` payload field.
"""
from __future__ import annotations

import os

HIDE_RATE_LIMITS = os.environ.get(
    "CLAUDIT_HIDE_RATE_LIMITS", "").lower() not in ("", "0", "false", "no")
