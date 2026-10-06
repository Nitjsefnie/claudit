"""The agent-type panel's bounded row cap, driven through node.

Issue #651: Cost by Agent Type grew one full-width row per distinct
agent role with no bound -- 1860px tall at 25 roles. The fix caps each
list at MAX_VISIBLE rows: the roles past the fold fold into one
aggregate `other` row, and the header toggle expands the full list.
The cap arithmetic is plain JS -- src/agent-list-caps.js -- because
node cannot parse JSX, so the fold boundary, the ordering and the
aggregate's sums are asserted here for real; the panel and guard
wiring is pinned at source level in tests/test_panel_interactions_js.py.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CAPS = ROOT / "src" / "agent-list-caps.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _node(body: str):
    """Run `body` against the real src/agent-list-caps.js in node."""
    script = f"""
      global.window = {{}};
      require({str(CAPS)!r});
      {body}
    """
    proc = subprocess.run(
        ["node"], input=script, capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def _rows(n):
    """n roles, values n..1 (already biggest-first), one request each."""
    return [{"label": f"role-{i:02d}", "value": n - i, "requests": 1}
            for i in range(n)]


def test_eight_roles_render_whole_nine_create_the_aggregate():
    """The fold boundary from both sides: 8 roles render as-is (8 is the
    cap, not the cap minus one), and the 9th role creates the `other`
    row -- 7 kept + the aggregate. A boundary that only held above would
    fold lists the cap never needed to touch."""
    out = _node(f"""
      const cap = window.agentListCaps.capAgentRows;
      const r8 = cap({json.dumps(_rows(8))}, 8);
      const r9 = cap({json.dumps(_rows(9))}, 8);
      console.log(JSON.stringify({{
        r8: [r8.rows.length, r8.hiddenCount,
             r8.rows.some(r => r.isOther)],
        r9: [r9.rows.length, r9.hiddenCount,
             r9.rows[7].label, r9.rows[7].value, r9.rows[7].requests,
             r9.rows[7].isOther === true],
        r9kept: r9.rows.slice(0, 7).map(r => r.label),
      }}));
    """)
    assert out["r8"] == [8, 0, False], out
    assert out["r9"] == [8, 2, "other (2 roles)", 3, 2, True], out
    assert out["r9kept"] == [f"role-{i:02d}" for i in range(7)], out


def test_thirty_roles_collapse_to_seven_plus_other():
    """#651's subject: 30 roles render 7 kept + one aggregate, whose
    value and request count are the hidden tail's sums computed from the
    same inputs -- a mis-folded aggregate over- or under-counts the very
    roles it stands for."""
    rows = _rows(30)
    expected_value = sum(r["value"] for r in rows[7:])
    expected_requests = sum(r["requests"] for r in rows[7:])
    out = _node(f"""
      const r = window.agentListCaps.capAgentRows({json.dumps(rows)}, 8);
      console.log(JSON.stringify({{
        n: r.rows.length, hidden: r.hiddenCount, other: r.rows[7],
        keptValues: r.rows.slice(0, 7).map(r => r.value),
      }}));
    """)
    assert out["n"] == 8 and out["hidden"] == 23, out
    assert out["other"]["label"] == "other (23 roles)", out
    assert out["other"]["value"] == expected_value, out
    assert out["other"]["requests"] == expected_requests, out
    assert out["keptValues"] == [30 - i for i in range(7)], out


def test_unsorted_input_is_sorted_biggest_first_before_the_fold():
    """The cap owns the ordering: an arbitrarily ordered input still
    folds away the true tail, never the first-arriving rows."""
    rows = [{"label": "a", "value": 1, "requests": 1},
            {"label": "b", "value": 50, "requests": 2},
            {"label": "c", "value": 25, "requests": 1},
            {"label": "d", "value": 3, "requests": 1},
            {"label": "e", "value": 30, "requests": 1}]
    out = _node(f"""
      const r = window.agentListCaps.capAgentRows({json.dumps(rows)}, 3);
      console.log(JSON.stringify({{
        labels: r.rows.map(r => r.label),
        other: [r.rows[2].value, r.rows[2].label],
      }}));
    """)
    assert out["labels"] == ["b", "e", "other (3 roles)"], out
    assert out["other"] == [29, "other (3 roles)"], out  # a(1) + d(3) + c(25)


def test_expanded_cap_passes_every_role_through():
    """The toggle's expanded state: an Infinity cap returns every role,
    no aggregate, nothing hidden."""
    out = _node(f"""
      const r = window.agentListCaps.capAgentRows(
        {json.dumps(_rows(30))}, Infinity);
      console.log(JSON.stringify({{
        n: r.rows.length, hidden: r.hiddenCount,
        anyOther: r.rows.some(r => r.isOther),
        first: r.rows[0].label, last: r.rows[29].label,
      }}));
    """)
    assert out == {"n": 30, "hidden": 0, "anyOther": False,
                   "first": "role-00", "last": "role-29"}, out


def test_empty_list_and_zero_value_rows():
    """No roles -> an empty list; zero-value rows are roles too (a free
    lane costs nothing but dispatched) and fold away like any other
    tail, with their requests carried into the aggregate."""
    rows = [{"label": "a", "value": 0, "requests": 4},
            {"label": "b", "value": 0, "requests": 1},
            {"label": "c", "value": 0, "requests": 1},
            {"label": "d", "value": 0, "requests": 1},
            {"label": "e", "value": 0, "requests": 1}]
    out = _node(f"""
      const cap = window.agentListCaps.capAgentRows;
      const e = cap([], 3);
      const z = cap({json.dumps(rows)}, 3);
      console.log(JSON.stringify({{
        e: [e.rows.length, e.hiddenCount],
        z: [z.rows.length, z.hiddenCount, z.rows[2].label,
            z.rows[2].value, z.rows[2].requests],
        zkept: z.rows.slice(0, 2).map(r => r.label),
      }}));
    """)
    assert out["e"] == [0, 0], out
    assert out["z"] == [3, 3, "other (3 roles)", 0, 3], out
    assert out["zkept"] == ["a", "b"], out
