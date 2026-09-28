"""Project identity helpers used by the ingest listing and planning walk."""
from __future__ import annotations

from backend import key_layout, lane_projects, r2


def _track_project(seen_projects: dict[str, dict], project_id: str,
                   last_modified, project_path: str | None,
                   original_id: str | None = None) -> None:
    """Accumulate first/last seen mtimes for one project.

    `project_id` is the id this run resolved for the file — the slug of
    the project's marker path when the run read one, else the stored or
    bare-hash id (lane_projects.resolve_lane_project); `original_id` is
    the project id the OBJECT KEY carried before that resolution — the
    pre-fold slug for a Claude-layout file, the lane hash otherwise.
    display_name comes from that
    marker path where one was read; every other project — and a lane
    project whose marker was missing or malformed — displays the
    original-case id holding the MOST files this walk (ties: the first
    seen) — which for a Windows project is the one session's shell's
    casing, never the blindly-lowercased folded id.
    `display_name_set` records which case this run is, so _persist's
    upsert can PRESERVE a stored display_name on a run that read no
    marker instead of resetting it to the bare id; a stored display that
    IS the bare id gets upgraded to this run's cased form in SQL (the
    upsert's second CASE branch). The path is applied
    even when the entry already exists (a Claude-layout file of the same
    directory created it first): the merge is what the slug id is for.
    """
    proj = seen_projects.setdefault(project_id, {
        "project_id": project_id,
        "display_name": project_path or original_id or project_id,
        "display_name_set": bool(project_path),
        "first_seen_at": last_modified,
        "last_seen_at": last_modified,
        "case_counts": {},
    })
    if project_path:
        if not proj["display_name_set"]:
            proj["display_name"] = project_path
            proj["display_name_set"] = True
    else:
        original = original_id or project_id
        counts: dict[str, int] = proj["case_counts"]
        counts[original] = counts.get(original, 0) + 1
        if not proj["display_name_set"]:
            # max() keeps the FIRST maximal pair, so an equal count
            # leaves the display with the first-seen slug.
            proj["display_name"] = max(
                counts.items(), key=lambda item: item[1])[0]
    if last_modified < proj["first_seen_at"]:
        proj["first_seen_at"] = last_modified
    if last_modified > proj["last_seen_at"]:
        proj["last_seen_at"] = last_modified


def _track_walked_project(seen_projects: dict[str, dict], info, obj,
                          project_paths: dict[str, str],
                          stored_lane: dict[str, str]) -> dict:
    """Resolve one walked file's project id and accumulate its mtimes.

    Returns the seen_projects entry (which _persist keys its project_id
    off), so the walk and every persist of the run share one identity —
    marker slug, stored mapping, or bare hash (lane_projects.resolve_lane_project).
    """
    # The PRE-canonical project id the key carried: classify() folds a
    # Windows slug on the Claude layout, and the walk needs the raw form
    # to choose display_name from (never the folded id itself). A lane
    # key's project is the hash segment; classify leaves it untouched.
    parts = r2.split_key(obj.key)[1].split("/")
    original_id = (parts[1] if parts[0] == key_layout.LANE_ROOT
                   else parts[0])
    marker_path = project_paths.get(info.project_id)
    project_id = lane_projects.resolve_lane_project(
        info.project_id, marker_path, stored_lane)
    _track_project(seen_projects, project_id,
                   obj.last_modified, marker_path, original_id=original_id)
    return seen_projects[project_id]


def _stored_version_is_newer(stored, parser_version: str) -> bool:
    """Whether a stored files row was written by a NEWER parser version.

    A rollback must not let the older binary's ingest rewrite rows it
    cannot write whole: _persist DELETEs and re-INSERTs each file's rows
    with its own column list, silently NULLing every column it does not
    know (issue #118). A stored value that does not parse as an int
    cannot be shown newer, so the ordinary reparse decision applies.
    """
    if stored is None:
        return False
    try:
        return int(stored[1]) > int(parser_version)
    except (TypeError, ValueError):
        return False
