"""The stamp belt's unit tests, split from test_perturb_test_data.py to
keep that module under its size ceiling — relocation only, no assertion
changed."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "perturb_test_data.py"


def _load():
    spec = importlib.util.spec_from_file_location("perturb_test_data", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["perturb_test_data"] = module
    spec.loader.exec_module(module)
    return module


perturb_module = _load()


def test_the_stamp_belt_clamps_a_candidate_at_or_before_the_predecessor():
    """`_stamp_after`'s clamp, unchanged by the decoupling: a candidate
    whose second is at or before the row's predecessor's — a real stamp
    newer than the counter, which perturb_pricing's
    read-the-document-once structure rules out in-process —
    spells the predecessor's second + 1s. The comparison is second
    precision, the precision the stamp itself carries, so a
    microsecond-carrying candidate never spells the predecessor's own
    second."""
    predecessor = "2026-10-01T12:00:00Z"
    clamp = "2026-10-01T12:00:01Z"
    belt = perturb_module._stamp_after  # pylint: disable=protected-access
    microsecond_carrying = datetime(
        2026, 10, 1, 12, 0, 0, 400000, tzinfo=timezone.utc)
    assert belt(predecessor, microsecond_carrying) == clamp
    assert belt(predecessor, datetime(1970, 1, 1, tzinfo=timezone.utc)) == clamp
    # No predecessor: the candidate passes through at second precision.
    assert belt(None, microsecond_carrying) == "2026-10-01T12:00:00Z"


def test_the_stamp_belt_passes_a_later_candidate_through():
    """A candidate strictly after the row's predecessor spells the
    counter value itself."""
    # pylint: disable-next=protected-access
    belt = perturb_module._stamp_after
    assert belt("2026-10-01T12:00:00Z",
                datetime(2026, 10, 1, 12, 0, 5, tzinfo=timezone.utc)) == \
        "2026-10-01T12:00:05Z"
