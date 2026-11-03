"""Panel requests wait for /api/dashboard to settle: the origin is one
worker. Node cannot parse JSX, so these checks read the sources."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

GATED = {
    ("src/activity-heatmap-panel.jsx", "/api/activity-heatmap"),
    ("src/cost-by-agent-panel.jsx", "/api/cost-by-agent"),
    ("src/dashboard-charts-extra.jsx", "/api/context-growth/traces"),
    ("src/dashboard-charts-extra.jsx", "/api/tool-usage"),
    ("src/dashboard-charts-extra.jsx", "/api/cost-by-context"),
    ("src/perf-panel.jsx", "/api/web-metrics"),
    ("src/reply-latency-panel.jsx", "/api/reply-latency"),
    ("src/tool-error-panel.jsx", "/api/tool-error-rate"),
}

# Requests outside the dashboard's panels: the app shell's own, a click,
# the beacon, and the cache route's views.
UNGATED = {
    ("src/app.jsx", "/api/sessions/"),
    ("src/app.jsx", "/api/me"),
    ("src/app.jsx", "/api/projects"),
    ("src/app.jsx", "/api/models"),
    ("src/app.jsx", "/api/dashboard"),
    ("src/app.jsx", "/api/events"),
    ("src/app-navigation.jsx", "/api/export"),
    ("src/perf.js", "/api/metrics"),
    ("src/views/cache-view.jsx", "/api/cache"),
    ("src/views/context-growth-view-v2.jsx", "/api/context-growth/agg"),
    ("src/views/context-growth-view-v2.jsx", "/api/context-growth/session/"),
}

GATE = re.compile(r"return\s+window\.dashboardFetch\.whenSettled\(\s*\(\)\s*=>\s*"
                  r"(?:window\.costByContext\.)?fetch\(\s*$")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)


def _api_sites() -> dict[tuple[str, str], list[str]]:
    files = subprocess.run(
        ["git", "ls-files", "src/*.js", "src/*.jsx"], cwd=ROOT,
        capture_output=True, text=True, check=True).stdout.split()
    sites: dict[tuple[str, str], list[str]] = {}
    for rel in files:
        src = _strip_comments((ROOT / rel).read_text(encoding="utf-8"))
        for m in re.finditer(r"[`'\"](/api/[\w/-]*)", src):
            sites.setdefault((rel, m.group(1)), []).append(src[:m.start()])
    return sites


def test_every_api_request_in_the_served_scripts_is_classified():
    assert set(_api_sites()) == GATED | UNGATED


def test_every_panel_request_starts_through_the_gate_and_cancels_with_its_effect():
    sites = _api_sites()
    assert all(len(sites[site]) == 1 for site in GATED), sites.keys()
    ungated = sorted(site for site in GATED if not GATE.search(sites[site][0]))
    assert not ungated, (
        f"requested beside the dashboard request, or without the cancel its "
        f"effect cleanup needs: {ungated}")
