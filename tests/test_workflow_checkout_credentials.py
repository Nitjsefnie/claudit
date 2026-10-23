"""Checkout token isolation."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_every_checkout_drops_credentials():
    """Unused job tokens must not survive checkout; zizmor treats persistence as a credential hand-over."""
    paths = sorted((ROOT / ".github/workflows").glob("*.y*ml"))
    paths += sorted((ROOT / ".github/actions").glob("*/action.y*ml"))
    checkouts = []
    for path in paths:
        doc = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        groups = [job.get("steps", []) for job in doc.get("jobs", {}).values()]
        groups.append(doc.get("runs", {}).get("steps", []))
        for steps in groups:
            for step in steps:
                if step.get("uses", "").partition("@")[0] == "actions/checkout":
                    checkouts.append((str(path.relative_to(ROOT)), step))
    assert checkouts, "no checkout steps found"
    for path, step in checkouts:
        assert step.get("with", {}).get("persist-credentials") in ("false", False), (
            path, step)
