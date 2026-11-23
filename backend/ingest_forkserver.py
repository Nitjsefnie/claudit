"""Setup imported only in the parse forkserver's preload list.

Exec preserves an ignored SIGTERM, which must not make the forkserver
survive a control-group stop; CPython resets only SIGINT. Workers
inherit the rate tables. No threads or clients are started here.
"""
from __future__ import annotations

import signal

from backend import pricing

signal.signal(signal.SIGTERM, signal.SIG_DFL)
pricing.bind_tables()
