"""The identity rows `_persist` writes per file (issue #959).

Both statements here used to be one `INSERT ... ON CONFLICT ... DO
UPDATE` each. That shape is wrong for an id: a tuple's DEFAULT nextval
(`projects.proj_id`, `files.file_id`) fires while the tuple is formed,
BEFORE the unique index reports the conflict, so re-persisting a file
the store already held spent a project id, a file id and — through the
same pattern in `dims` — a dim id per value, on every run. The id space
is a surrogate (Task 7 narrows it to SMALLINT: 32767 values), so an
allocation must spend one value only on a row it actually inserts, and
`DO UPDATE` must not lock a row the writer was not changing.

The shape is therefore: read whether the row exists; insert only if it
does not (`ON CONFLICT DO NOTHING` for the race against a concurrent
writer of the same key); merge the stored metadata with an UPDATE in
every other case — the same expressions the DO UPDATE body carried, so
the stored row ends up identical either way.
"""
from __future__ import annotations

from typing import Any

_PROJECT_MERGE = """
    UPDATE projects SET
      display_name = CASE
        WHEN %(display_name_set)s THEN %(display_name)s
        WHEN projects.display_name = projects.project_id AND
             %(display_name)s <> projects.project_id
          THEN %(display_name)s
        ELSE projects.display_name END,
      first_seen_at = LEAST(projects.first_seen_at, %(first_seen_at)s),
      last_seen_at = GREATEST(projects.last_seen_at, %(last_seen_at)s)
    WHERE project_id = %(project_id)s
"""

_FILE_INSERT = """
    INSERT INTO files (
      file_key,
      project_id,
      session_id,
      is_main,
      r2_etag,
      r2_size_bytes,
      r2_last_modified,
      parsed_at,
      parser_version,
      collection_flags,
      ctx_turns,
      turn_count,
      prompt_count,
      prompt_ts,
      rate_limit_hits,
      agent_type,
      models,
      teammate_name)
    VALUES (
      %(file_key)s,
      %(project_id)s,
      %(session_id)s,
      %(is_main)s,
      %(r2_etag)s,
      %(r2_size_bytes)s,
      %(r2_last_modified)s,
      %(parsed_at)s,
      %(parser_version)s,
      %(collection_flags)s,
      %(ctx_turns)s::jsonb,
      %(turn_count)s,
      %(prompt_count)s,
      %(prompt_ts)s::jsonb,
      %(rate_limit_hits)s::jsonb,
      %(agent_type)s,
      %(models)s,
      %(teammate_name)s)
    ON CONFLICT (file_key) DO NOTHING
    RETURNING file_id
"""

_FILE_MERGE = """
    UPDATE files SET
      project_id = %(project_id)s,
      session_id = %(session_id)s,
      is_main = %(is_main)s,
      r2_etag = %(r2_etag)s,
      r2_size_bytes = %(r2_size_bytes)s,
      r2_last_modified = %(r2_last_modified)s,
      parsed_at = %(parsed_at)s,
      parser_version = %(parser_version)s,
      collection_flags = %(collection_flags)s,
      ctx_turns = %(ctx_turns)s::jsonb,
      turn_count = %(turn_count)s,
      prompt_count = %(prompt_count)s,
      prompt_ts = %(prompt_ts)s::jsonb,
      rate_limit_hits = %(rate_limit_hits)s::jsonb,
      agent_type = %(agent_type)s,
      models = %(models)s,
      teammate_name = %(teammate_name)s
    WHERE file_key = %(file_key)s
"""

_FILE_ID = "SELECT file_id FROM files WHERE file_key = %(file_key)s"


def upsert_project(cur, proj: dict[str, Any]) -> None:
    """Insert the project row when absent, else merge its metadata.

    first_seen_at uses LEAST so a later ingest seeing an older file
    drags it backward. display_name is overwritten only when THIS run
    actually read a marker, or when the stored display is the bare
    project id and this run's walk recovered a better-cased form (the
    Windows case-fold: the folded id must not outshout a known original
    casing): a transient marker-fetch failure must not reset a stored
    display path to the bare id until some later reparse repairs it. The
    flag defaults False so a caller handing _persist a plain
    {project_id, display_name, ...} dict (tests do) preserves.
    """
    present = cur.execute(
        "SELECT 1 FROM projects WHERE project_id = %(project_id)s", proj
    ).fetchone()
    if present is None:
        inserted = cur.execute(
            "INSERT INTO projects (project_id, display_name, "
            "first_seen_at, last_seen_at) "
            "VALUES (%(project_id)s, %(display_name)s, "
            "%(first_seen_at)s, %(last_seen_at)s) "
            "ON CONFLICT (project_id) DO NOTHING",
            proj,
        )
        if inserted.rowcount:
            return  # our own row already carries the incoming metadata
    # Stored row (or a concurrent writer's, which we lost the insert to):
    # merge into it exactly as the old DO UPDATE body did.
    cur.execute(_PROJECT_MERGE, proj)


def upsert_file(cur, payload: dict[str, Any]) -> int:
    """Insert the file row when absent, else refresh its metadata.

    Returns the row's `file_id` — the fact tables' FK and the reason the
    old statement carried RETURNING. The metadata columns (etag, parse
    time, ctx turns, models ...) are refreshed on every persist of a
    stored key, which is what a reparse means; only the id allocation
    moves out of that statement's way.
    """
    row = cur.execute(_FILE_ID, payload).fetchone()
    if row is None:
        inserted = cur.execute(_FILE_INSERT, payload).fetchone()
        if inserted is not None:
            return inserted[0]
        # Lost the race to a concurrent writer of this key: adopt its id
        # and merge our metadata, which the insert above declined to do.
        cur.execute(_FILE_MERGE, payload)
        row = cur.execute(_FILE_ID, payload).fetchone()
        assert row is not None, f"files row vanished mid-insert: {payload['file_key']!r}"
        return row[0]
    cur.execute(_FILE_MERGE, payload)
    return row[0]
