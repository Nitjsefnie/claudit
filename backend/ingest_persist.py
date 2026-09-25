"""One file per transaction: ingest's `_persist`.

Holds `_persist` alone, split from ingest so its INSERT statements can
be laid out one column per line with the VALUES list one placeholder
per line, same order — a column/value misalignment is then visible,
where packed lists hide it (ingest.py packed these to stay under
pylint's module-size limit, issue #86). `ingest` re-exports
`_persist`, so `ingest._persist(...)` keeps resolving for callers.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from backend import constants, db, key_layout, parse, r2


def _persist(obj, proj, parsed, parser_version) -> None:
    """One file, one transaction — identical to the pre-pool behaviour.

    session_id and is_main come from the same key_layout.classify() the
    walk used, applied to the object-key part of the bucket-qualified
    file key, so a lane wire lands under the session its key names and a
    subagent wire is never main. project_id comes from the walk's
    seen_projects entry (`proj`), which resolved the lane-hash→slug
    identity BEFORE the walk queued this file — re-deriving it here from
    the key alone would re-split a merged lane project on any run whose
    marker read failed.
    """
    info = key_layout.classify(r2.split_key(obj.key)[1])
    if info is None:
        raise ValueError(f"not a transcript key: {obj.key}")
    session_id, is_main = info.session_id, info.is_main
    project_id = proj["project_id"]
    with db.viz_conn() as c, c.cursor() as cur:
        # Project upsert. first_seen_at uses LEAST so a later
        # ingest seeing an older file drags it backward. display_name is
        # overwritten only when THIS run actually read a marker, or when
        # the stored display is the bare project id and this run's walk
        # recovered a better-cased form (the Windows case-fold: the
        # folded id must not outshout a known original casing): a
        # transient marker-fetch failure must not reset a stored display
        # path to the bare id until some later reparse repairs it. The
        # flag defaults False so a caller handing _persist a plain
        # {project_id, display_name, ...} dict (tests do) preserves.
        proj = {k: v for k, v in proj.items() if k != "case_counts"}
        proj["display_name_set"] = bool(proj.get("display_name_set"))
        cur.execute(
            "INSERT INTO projects (project_id, display_name, "
            "first_seen_at, last_seen_at) "
            "VALUES (%(project_id)s, %(display_name)s, "
            "%(first_seen_at)s, %(last_seen_at)s) "
            "ON CONFLICT (project_id) DO UPDATE SET "
            "  display_name = CASE "
            "    WHEN %(display_name_set)s THEN EXCLUDED.display_name "
            "    WHEN projects.display_name = projects.project_id AND "
            "         EXCLUDED.display_name <> projects.project_id "
            "      THEN EXCLUDED.display_name "
            "    ELSE projects.display_name END, "
            "  first_seen_at = LEAST(projects.first_seen_at, "
            "                        EXCLUDED.first_seen_at), "
            "  last_seen_at = GREATEST(projects.last_seen_at, "
            "                          EXCLUDED.last_seen_at)",
            proj,
        )
        # Wipe existing records for this file (we use UPSERT on
        # files so records need an explicit DELETE before the
        # bulk INSERT below).
        cur.execute(
            "DELETE FROM records WHERE file_key = %s", (obj.key,)
        )
        cur.execute(
            """
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
              ctx_turns,
              turn_count,
              prompt_count,
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
              %(ctx_turns)s::jsonb,
              %(turn_count)s,
              %(prompt_count)s,
              %(rate_limit_hits)s::jsonb,
              %(agent_type)s,
              %(models)s,
              %(teammate_name)s)
            ON CONFLICT (file_key) DO UPDATE SET
              project_id = EXCLUDED.project_id,
              session_id = EXCLUDED.session_id,
              is_main = EXCLUDED.is_main,
              r2_etag = EXCLUDED.r2_etag,
              r2_size_bytes = EXCLUDED.r2_size_bytes,
              r2_last_modified = EXCLUDED.r2_last_modified,
              parsed_at = EXCLUDED.parsed_at,
              parser_version = EXCLUDED.parser_version,
              ctx_turns = EXCLUDED.ctx_turns,
              turn_count = EXCLUDED.turn_count,
              prompt_count = EXCLUDED.prompt_count,
              rate_limit_hits = EXCLUDED.rate_limit_hits,
              agent_type = EXCLUDED.agent_type,
              models = EXCLUDED.models,
              teammate_name = EXCLUDED.teammate_name
            """,
            {
                "file_key": obj.key,
                "project_id": project_id,
                "session_id": session_id,
                "is_main": is_main,
                "r2_etag": obj.etag,
                "r2_size_bytes": obj.size,
                "r2_last_modified": obj.last_modified,
                "parsed_at": datetime.now(timezone.utc),
                "parser_version": parser_version,
                "ctx_turns": json.dumps(parsed["ctx_turns"], default=str),
                "turn_count": parsed["turn_count"],
                "prompt_count": parsed["prompt_count"],
                "rate_limit_hits": json.dumps(
                    parsed.get("rate_limit_hits", []), default=str
                ),
                "agent_type": parsed.get("agent_type", parse.DEFAULT_AGENT_TYPE),
                "models": parsed.get("models", []),
                "teammate_name": parsed.get("teammate_name"),
            },
        )
        # tool_uses cascades from files; explicit DELETE so a
        # reparse doesn't leave stale rows behind.
        cur.execute(
            "DELETE FROM tool_uses WHERE file_key = %s", (obj.key,)
        )
        if parsed.get("tool_uses"):
            cur.executemany(
                """
                INSERT INTO tool_uses (
                  file_key,
                  line_num,
                  idx,
                  ts,
                  tool_name,
                  model,
                  tool_use_id,
                  is_error,
                  error_kind,
                  error_text,
                  lines_added,
                  lines_deleted,
                  agent_type,
                  agent_model,
                  dispatch_prompt_chars,
                  dispatch_brief_ref,
                  dispatch_name,
                  result_chars,
                  read_kind,
                  read_targets,
                  write_targets,
                  is_reread)
                VALUES (
                  %(file_key)s,
                  %(line_num)s,
                  %(idx)s,
                  %(ts)s,
                  %(tool_name)s,
                  %(model)s,
                  %(tool_use_id)s,
                  %(is_error)s,
                  %(error_kind)s,
                  %(error_text)s,
                  %(lines_added)s,
                  %(lines_deleted)s,
                  %(agent_type)s,
                  %(agent_model)s,
                  %(dispatch_prompt_chars)s,
                  %(dispatch_brief_ref)s,
                  %(dispatch_name)s,
                  %(result_chars)s,
                  %(read_kind)s,
                  %(read_targets)s,
                  %(write_targets)s,
                  %(is_reread)s)
                """,
                parsed["tool_uses"],
            )
        if parsed["records"]:
            # long_context is lane-only (parse_common._append_usage_record),
            # provider Claude-only (parse._provider); a record lacking the
            # key stores NULL, and readers COALESCE long_context to FALSE.
            # pricing_version is NOT a record field: it is stamped here, at
            # persist time, from constants.PRICING_VERSION, so every reparse
            # re-stamps the version its freshly computed cost_usd was
            # priced under (issue #193).
            for rec in parsed["records"]:
                rec.update({k: rec.get(k) for k in ("long_context", "provider")})
            cur.executemany(
                """
                INSERT INTO records (
                  file_key,
                  line_num,
                  uuid,
                  request_id,
                  ts,
                  model,
                  fresh_tokens,
                  cache_creation_tokens,
                  cache_read_tokens,
                  output_tokens,
                  eph5_tokens,
                  eph1h_tokens,
                  cost_usd,
                  text_chars,
                  reply_latency_s,
                  stop_reason,
                  effort,
                  thinking_tokens,
                  cli_version,
                  turn_flags,
                  turn_tool_results,
                  long_context,
                  provider,
                  pricing_version)
                VALUES (
                  %(file_key)s,
                  %(line_num)s,
                  %(uuid)s,
                  %(request_id)s,
                  %(ts)s,
                  %(model)s,
                  %(fresh_tokens)s,
                  %(cache_creation_tokens)s,
                  %(cache_read_tokens)s,
                  %(output_tokens)s,
                  %(eph5_tokens)s,
                  %(eph1h_tokens)s,
                  %(cost_usd)s,
                  %(text_chars)s,
                  %(reply_latency_s)s,
                  %(stop_reason)s,
                  %(effort)s,
                  %(thinking_tokens)s,
                  %(cli_version)s,
                  %(turn_flags)s,
                  %(turn_tool_results)s,
                  %(long_context)s,
                  %(provider)s,
                  %(pricing_version)s)
                """,
                (
                    {**rec, "pricing_version": constants.PRICING_VERSION}
                    for rec in parsed["records"]
                ),
            )
        c.commit()
