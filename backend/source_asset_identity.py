"""Source transform URL identity."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def source_transform_identity() -> str:
    """Hash the js_min*.py family."""
    digest = hashlib.sha256()
    for module in sorted(Path(__file__).parent.glob("js_min*.py")):
        digest.update(module.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(module.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
