"""Signal setup imported only in the parse forkserver's preload list.

Exec preserves ignored signals. A service ignoring SIGTERM must not
make its forkserver survive a control-group stop; CPython resets SIGINT
but leaves SIGTERM alone. No threads or clients are started here.
"""
from __future__ import annotations

import signal

signal.signal(signal.SIGTERM, signal.SIG_DFL)
