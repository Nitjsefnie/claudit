"""Per-file identity writes without spending surrogate ids on existing rows.

DEFAULT nextval runs before conflict detection; DO UPDATE also locks unchanged
rows. Read first, insert absent rows with ON CONFLICT DO NOTHING for races,
and merge existing metadata by UPDATE.
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
      AND (
        projects.display_name,
        projects.first_seen_at,
        projects.last_seen_at
      ) IS DISTINCT FROM (
        CASE
          WHEN %(display_name_set)s THEN %(display_name)s
          WHEN projects.display_name = projects.project_id AND
               %(display_name)s <> projects.project_id
            THEN %(display_name)s
          ELSE projects.display_name END,
        LEAST(projects.first_seen_at, %(first_seen_at)s),
        GREATEST(projects.last_seen_at, %(last_seen_at)s)
      )
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
      ctx_ts,
      ctx_line,
      ctx_input,
      ctx_output,
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
      %(ctx_ts)s::timestamptz[],
      %(ctx_line)s::integer[],
      %(ctx_input)s::integer[],
      %(ctx_output)s::integer[],
      %(turn_count)s,
      %(prompt_count)s,
      %(prompt_ts)s::timestamptz[],
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
      ctx_ts = %(ctx_ts)s::timestamptz[],
      ctx_line = %(ctx_line)s::integer[],
      ctx_input = %(ctx_input)s::integer[],
      ctx_output = %(ctx_output)s::integer[],
      turn_count = %(turn_count)s,
      prompt_count = %(prompt_count)s,
      prompt_ts = %(prompt_ts)s::timestamptz[],
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
            return  # Our row already carries incoming metadata.
    # Merge incoming metadata into an existing row or a concurrent insert winner.
    cur.execute(_PROJECT_MERGE, proj)


def upsert_file(cur, payload: dict[str, Any]) -> int:
    """Insert an absent file or refresh stored metadata, returning its file_id.

    Facts need the surrogate FK. Reparse refreshes etag, parse time,
    context turns and models on every persist; allocate ids only for
    missing rows to avoid spending sequence values on conflicts."""
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
