"""Pure halves of the rendered project-picker acceptance rules."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "ci" / "panel_layout_rules.mjs"
PROJECTS_FIXTURE = ROOT / "fixtures" / "layout" / "projects.json"


def _node(body: str):
    url = MODULE.as_uri()
    script = f"const mod = await import({json.dumps(url)});\n{body}\n"
    proc = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True,
        text=True, timeout=60, check=False, encoding="utf-8", errors="strict",
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def test_picker_layout_fixture_has_wider_later_projects_and_long_id():
    projects = json.loads(
        PROJECTS_FIXTURE.read_text(encoding="utf-8"))["projects"]
    assert len(projects) >= 12
    first_width = max(len(p["display_name"]) for p in projects[:4])
    assert all(len(p["display_name"]) > first_width for p in projects[4:])
    assert len(projects[-1]["project_id"]) >= 60


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_picker_walk_checks_identity_completion_and_nonempty_pages():
    out = _node("""
      const check = mod.pickerWalkViolations;
      if (!check) { console.log('null'); }
      else {
        const p = (page, total, ids) => ({ page, total, ids,
          previousDisabled: page === 1, nextDisabled: page === total });
        const f = [p(1, 3, ['p1']), p(2, 3, ['p2']), p(3, 3, ['p3'])];
        const b = [...f].reverse();
        const kinds = (forward, backward, ids) =>
          check(forward, backward, ids).map(v => v.kind);
        const ids = ['p1', 'p2', 'p3'];
        const skippedNext = kinds([p(1, 3, ['p1']), p(3, 3, ['p2', 'p3'])], b, ids);
        const skippedPrev = kinds(f, [p(3, 3, ['p3']), p(1, 3, ['p1', 'p2'])], ids);
        const emptyLast = kinds([p(1, 2, ['p1', 'p2']), p(2, 2, [])],
          [p(2, 2, []), p(1, 2, ['p1', 'p2'])], ids);
        const duplicate = kinds([p(1, 2, ['p1', 'p1']), p(2, 2, ['p3'])],
          [p(2, 2, ['p3']), p(1, 2, ['p1', 'p1'])], ids);
        const stuckNext = kinds(f.map((page, i) => i === 2
          ? {...page, nextDisabled: false} : page), b, ids);
        const stuckPrevious = kinds(f, b.map((page, i) => i === 2
          ? {...page, previousDisabled: false} : page), ids);
        const single = kinds([p(1, 1, ['only'])], [p(1, 1, ['only'])], ['only']);
        const empty = kinds([p(1, 1, [])], [p(1, 1, [])], []);
        const manyIds = Array.from({length: 101}, (_, i) => `id-${i}`);
        const manyForward = manyIds.map((id, i) => p(i + 1, 101, [id]));
        const manyBackward = [...manyForward].reverse();
        console.log(JSON.stringify({
          skippedNext, skippedPrev, emptyLast, duplicate,
          stuckNext, stuckPrevious, single, empty,
          many: kinds(manyForward, manyBackward, manyIds),
        }));
      }
    """)
    assert out is not None, "pickerWalkViolations is missing"
    assert "sequence" in out["skippedNext"]
    assert "sequence" in out["skippedPrev"]
    assert "empty-page" in out["emptyLast"]
    assert "project-order" in out["duplicate"]
    assert "completion" in out["stuckNext"]
    assert "completion" in out["stuckPrevious"]
    assert out["single"] == []
    assert out["empty"] == []
    assert out["many"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_picker_fit_accepts_single_page_without_a_pager():
    out = _node("""
      const check = mod.pickerPageFitFindings;
      if (!check) { console.log('null'); }
      else console.log(JSON.stringify(check([{
        page: 1, total: 1, left: 0, right: 120, overflow: false,
        chipRight: 100, pagerLeft: null, pagerRight: null,
        pagerWidth: null, pagerVisible: false,
      }])));
    """)
    assert out is not None, "pickerPageFitFindings is missing"
    assert out == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_picker_page_geometry_reads_height_position_and_seat():
    out = _node("""
      const check = mod.pickerPageGeometryViolations;
      if (!check) { console.log('null'); }
      else {
        const first = { top: 52, height: 47, seat: 6, below: true };
        console.log(JSON.stringify({
          stable: check([first, {...first}]).map(v => v.kind),
          height: check([first, {...first, height: 90}]).map(v => v.kind),
          position: check([first, {...first, top: 72, seat: 6}]).map(v => v.kind),
          seat: check([first, {...first, top: 52, seat: 26}]).map(v => v.kind),
        }));
      }
    """)
    assert out is not None, "pickerPageGeometryViolations is missing"
    assert out["stable"] == []
    assert "height" in out["height"]
    assert "position" in out["position"]
    assert "seat" in out["seat"]
