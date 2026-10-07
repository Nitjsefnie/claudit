"""src/per-turn-stats.js: the nearest-rank rule the context panel draws.

Pins the rule verbatim (issue #644 extracted the function verbatim; the
pins keep it verbatim): median `arr[floor(n / 2)]`, percentile
`arr[floor(n * q)]` over the per-turn values of every trace's seq, and
`null` + count 0 for a turn no trace reaches.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATS_JS = Path(__file__).resolve().parents[1] / "src" / "per-turn-stats.js"


def _run(body: str) -> dict:
    script = f"""
      global.window = {{}};
      require({str(STATS_JS)!r});
      {body}
    """
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_empty_and_missing_turns():
    out = _run("""
      const noInput = window.perTurnStats(null);
      const noSessions = window.perTurnStats([]);
      const noTurns = window.perTurnStats([{ seq: [] }]);
      const gapped = window.perTurnStats([
        { seq: [{ t: 0, ctx: 0 }, { t: 1, ctx: 100 }] },
        { seq: [{ t: 0, ctx: 0 }, { t: 3, ctx: 300 }] },
      ]);
      console.log(JSON.stringify({
        noInput: [noInput.turns.length, noInput.maxT],
        noSessions: JSON.stringify(noSessions) === JSON.stringify(noInput),
        noTurns: JSON.stringify(noTurns) === JSON.stringify(noInput),
        gapped: gapped.count,
      }));
    """)
    assert out == {
        "noInput": [0, 0],
        "noSessions": True,
        "noTurns": True,
        # t0 has both origins, t1 one trace, t2 none (nulls), t3 one.
        "gapped": [2, 1, 0, 1],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_nearest_rank_rule_pinned():
    """Four values at one turn: median arr[floor(4/2)] = the 3rd, p25
    arr[1] = the 2nd, p75 arr[3] = the 4th, p90 pick(3.6) = the 4th."""
    out = _run("""
      const sessions = [10, 20, 30, 40].map(ctx => ({
        seq: [{ t: 0, ctx: 0 }, { t: 1, ctx }],
      }));
      const stats = window.perTurnStats(sessions);
      console.log(JSON.stringify({
        t1: [stats.median[1], stats.p25[1], stats.p75[1], stats.p90[1]],
        count: stats.count,
        maxT: stats.maxT,
        t0: [stats.median[0], stats.p90[0], stats.count[0]],
      }));
    """)
    assert out == {
        # median arr[2]=30 (NOT the interpolated 25), p25 arr[1]=20,
        # p75 arr[3]=40, p90 pick(0.9*4=3.6 -> 3) = 40.
        "t1": [30, 20, 40, 40],
        "count": [4, 4],
        "maxT": 1,
        # turn 0 carries only the {t: 0, ctx: 0} origins.
        "t0": [0, 0, 4],
    }
