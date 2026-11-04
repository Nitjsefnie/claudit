"""Cross-run artifact downloads name their triggering run."""
from __future__ import annotations

from tests.workflow_checks import WORKFLOWS, workflow_by_name as _load


def test_every_cross_run_download_names_the_triggering_run():
    # Without run-id a download step reads THIS workflow's run, never the
    # triggering run's: every workflow_run download takes the event's id.
    swept = 0
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = _load(path.name)
        if "workflow_run" not in (doc.get("on") or {}):
            continue
        for job_id, job in (doc.get("jobs") or {}).items():
            for step in (job or {}).get("steps") or []:
                if not (step.get("uses") or "").startswith(
                        "actions/download-artifact"):
                    continue
                swept += 1
                assert (step.get("with") or {}).get("run-id") == (
                    "${{ github.event.workflow_run.id }}"), (path.name, job_id)
    assert swept, "no download-artifact step under any workflow_run trigger"
