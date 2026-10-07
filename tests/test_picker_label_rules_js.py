"""The picker-underfill and panel-label classifiers, driven through node.

Issues #808 and #826: the picker must render every chip that fits (the
fixed-small-page regression) and every marked panel label must stay
inside its panel's box. The rendered rules live in
scripts/ci/panel_layout_rules.mjs; this file pins their pure halves.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "ci" / "panel_layout_rules.mjs"


def _node(body: str):
    """Import the harness in node and evaluate `body` against its exports."""
    url = MODULE.as_uri()
    script = (
        f"const mod = await import({json.dumps(url)});\n"
        f"{body}\n"
    )
    proc = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True,
        text=True, timeout=60, check=False, encoding="utf-8", errors="strict",
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
class TestUnderfillViolation:
    """The picker-underfill classifier (#808): fewer chips rendered
    than the measure row holds, with room for the next off-page chip,
    is the fixed-small-page regression. The fit is order-preserving,
    so the chip being denied a slot is the NEXT one in list order.
    Boundary: room net of the gap equal to that chip's width IS
    underfill (the chip fits); just under is conforming."""

    @staticmethod
    def _violations(free_room, next_off_page, rendered, measured, gap=6):
        return _node(
            "console.log(JSON.stringify(mod.underfillViolation(..."
            + json.dumps([free_room, next_off_page, rendered, measured, gap])
            + ")));"
        )

    def test_room_for_the_next_chip_fires(self):
        assert self._violations(220, 100, 2, 6) == 220

    def test_room_just_under_the_next_chip_passes(self):
        # 105.9 - 6 gap = 99.9, under the chip's 100px width: no slot
        # is being denied.
        assert self._violations(105.9, 100, 2, 6, gap=6) is None

    def test_exact_fit_is_underfill(self):
        # room - gap == exactly the chip width: the chip fits.
        assert self._violations(106, 100, 2, 6, gap=6) == 106

    def test_everything_rendered_checks_nothing(self):
        assert self._violations(500, 100, 6, 6) is None

    def test_no_next_chip_checks_nothing(self):
        assert self._violations(500, None, 2, 6) is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
class TestLabelOverflowViolations:
    """The panel-label rule's classifier (#826): a marked label text
    whose box escapes its panel's box on either side is an overflow.
    The exact-fit and 1px-noise cases pass."""

    @staticmethod
    def _violations(labels):
        return _node(
            "console.log(JSON.stringify(mod.labelOverflowViolations("
            + json.dumps(labels) + ")));"
        )

    def test_label_escaping_left_fires(self):
        out = self._violations([{
            "panel": "Tokens by Model",
            "label": {"x": -120, "y": 10, "w": 260, "h": 14},
            "box": {"x": 0, "y": 0, "w": 340, "h": 200},
        }])
        assert len(out) == 1
        assert out[0]["panel"] == "Tokens by Model"
        assert out[0]["side"] == "left"

    def test_label_escaping_right_fires(self):
        out = self._violations([{
            "panel": "P",
            "label": {"x": 200, "y": 10, "w": 200, "h": 14},
            "box": {"x": 0, "y": 0, "w": 340, "h": 200},
        }])
        assert len(out) == 1
        assert out[0]["side"] == "right"

    def test_label_inside_passes(self):
        out = self._violations([{
            "panel": "P",
            "label": {"x": 2, "y": 10, "w": 336, "h": 14},
            "box": {"x": 0, "y": 0, "w": 340, "h": 200},
        }])
        assert out == []

    def test_one_px_noise_passes(self):
        out = self._violations([{
            "panel": "P",
            "label": {"x": -1, "y": 10, "w": 340, "h": 14},
            "box": {"x": 0, "y": 0, "w": 340, "h": 200},
        }])
        assert out == []
