"""One file per transaction: ingest's _persist, re-exported by ingest.

Keep INSERT columns and VALUES placeholders one per line in the same order,
so column/value misalignment stays visible.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from typing import Any

from backend.file_timelines import ctx_columns
from backend import (constants, db, dims, ingest_identity, key_layout,
                     parse, pricing, r2, rate_fingerprint, turn_flag_bits,
                     uuid_split)
from backend.flags import CollectionPolicy
from backend.flag_registry import COLLECTION_SPECS
from backend.parse_collection import mask_collected_fields


def _strip_nul(value: Any) -> Any:
    """Strip NUL recursively from every parsed string before SQL.

    PostgreSQL refuses NUL in text/jsonb and one bad value aborts the
    file's entire statement. This gate covers every transcript-derived
    text column; marker writes outside _persist apply it separately.
    Identity strings also pass through: object-key material cannot carry
    NUL, so stripping preserves their identities."""
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        for k in value:
            value[k] = _strip_nul(value[k])
        return value
    if isinstance(value, list):
        for i, v in enumerate(value):
            value[i] = _strip_nul(v)
        return value
    if isinstance(value, tuple):
        return tuple(_strip_nul(v) for v in value)
    return value


# Per-row measures stored INT4: the nine records
# token/text columns and tool_uses' per-call churn. Rollup TOTAL columns
# stay BIGINT and are never built from these lists — a corpus sum exceeds
# int4 by construction, a single row's value does not.
_RECORD_INT4 = (
    "fresh_tokens", "cache_creation_tokens", "cache_read_tokens",
    "cache_write_tokens", "output_tokens", "eph5_tokens", "eph1h_tokens",
    "thinking_tokens", "text_chars",
)
_TOOL_INT4 = ("lines_added", "lines_deleted", "result_chars")
_INT4_MIN = -(1 << 31)
_INT4_MAX = (1 << 31) - 1


def _int4(value, column: str):
    """Range-check one per-row measure before it reaches an INT4 column.

    The column would reject an out-of-range value too, but only as a
    DataError against an anonymous parameter; this fails first, in the
    persist path, naming the column and the value — loud, never a wrap
    and never a truncation. None passes through (nullable columns keep
    their NULL; a NOT NULL column rejects its own NULL as before).
    """
    if value is None:
        return None
    if (not isinstance(value, int) or isinstance(value, bool)
            or not _INT4_MIN <= value <= _INT4_MAX):
        raise ValueError(
            f"{column}={value!r} does not fit its INT4 column")
    return value


def _map_record_dims(cur, records, model_ids, provider_ids, stop_ids,
                     effort_ids, cli_ids) -> None:
    pricing_id = dims.dim_id(cur, "dim_pricing_version",
                             constants.PRICING_VERSION)
    fingerprints = [
        rate_fingerprint.pair_fingerprint(rec["model"], rec.get("provider"))
        for rec in records
    ]
    fp_ids = dims.dim_ids(cur, "dim_rate_fingerprint", fingerprints)
    for rec, fp in zip(records, fingerprints):
        rec["model_id"] = model_ids[rec["model"]]
        rec["provider_id"] = provider_ids.get(rec.get("provider"))
        rec["stop_reason_id"] = stop_ids.get(rec.get("stop_reason"))
        rec["effort_id"] = effort_ids.get(rec.get("effort"))
        rec["pricing_version_id"] = pricing_id
        rec["rate_fingerprint_id"] = fp_ids[fp]
        # Claude Code build as a dim: .get keeps an absent version NULL,
        # without allocating a reserved vocabulary row.
        rec["cli_version_id"] = cli_ids.get(rec.get("cli_version"))


def _map_tool_dims(tools, model_ids, tool_ids, error_ids, agent_type_ids,
                   agent_model_ids) -> None:
    for tu in tools:
        tu["tool_name_id"] = tool_ids[tu["tool_name"]]
        tu["model_id"] = model_ids.get(tu.get("model"))
        tu["error_kind_id"] = error_ids.get(tu.get("error_kind"))
        tu["agent_type_id"] = agent_type_ids.get(tu.get("agent_type"))
        tu["agent_model_id"] = agent_model_ids.get(tu.get("agent_model"))


def _resolve_dims(cur, parsed) -> None:
    """Resolve parse-output dim FKs in place.

    Nullable FKs preserve disabled fields. The pricing stamp records
    constants.PRICING_VERSION and the fingerprint of the rate data used
    for cost; both are resolved to dim ids."""
    records = parsed.get("records") or []
    tools = parsed.get("tool_uses") or []
    model_ids = dims.dim_ids(
        cur, "dim_model",
        [rec["model"] for rec in records]
        + [tu.get("model") for tu in tools])
    provider_ids = dims.dim_ids(
        cur, "dim_provider", [rec.get("provider") for rec in records])
    stop_ids = dims.dim_ids(
        cur, "dim_stop_reason", [rec.get("stop_reason") for rec in records])
    effort_ids = dims.dim_ids(
        cur, "dim_effort", [rec.get("effort") for rec in records])
    tool_ids = dims.dim_ids(
        cur, "dim_tool_name", [tu["tool_name"] for tu in tools])
    error_ids = dims.dim_ids(
        cur, "dim_error_kind", [tu.get("error_kind") for tu in tools])
    agent_type_ids = dims.dim_ids(
        cur, "dim_agent_type", [tu.get("agent_type") for tu in tools])
    agent_model_ids = dims.dim_ids(
        cur, "dim_agent_model", [tu.get("agent_model") for tu in tools])
    cli_ids = dims.dim_ids(
        cur, "dim_cli_version", [rec.get("cli_version") for rec in records])
    _map_record_dims(cur, records, model_ids, provider_ids, stop_ids,
                     effort_ids, cli_ids)
    _map_tool_dims(tools, model_ids, tool_ids, error_ids, agent_type_ids,
                   agent_model_ids)


def _write_facts(cur, parsed, file_id: int) -> None:
    if parsed.get("tool_uses"):
        # is_replay is Codex-only (codex_fork.mark_replay); every
        # other path's rows lack the key and store NULL, which the
        # winner rule ranks as an original.
        for tu in parsed["tool_uses"]:
            tu.setdefault("is_replay", None)
            for column in _TOOL_INT4:
                tu[column] = _int4(tu[column], f"tool_uses.{column}")
            # Empty target arrays store as NULL: every reader treats absent as
            # empty, so an empty array would add storage without information.
            tu["read_targets"] = tu["read_targets"] or None
            tu["write_targets"] = tu["write_targets"] or None
            # read_kind is two-valued by construction (bash_reads and
            # parse._tool_access emit only 'whole' / 'slice' / None), so
            # it stores as BOOLEAN: TRUE = the whole file went into
            # context. Anything else is a vocabulary drift and fails
            # here rather than silently storing False.
            kind = tu["read_kind"]
            if kind is not None and kind not in ("whole", "slice"):
                raise ValueError(f"read_kind={kind!r} is not two-valued")
            tu["read_kind"] = None if kind is None else kind == "whole"
            tu["file_id"] = file_id
        cur.executemany(
            """
            INSERT INTO tool_uses (
              file_id,
              line_num,
              idx,
              ts,
              tool_name_id,
              model_id,
              tool_use_id,
              is_error,
              error_kind_id,
              error_text,
              lines_added,
              lines_deleted,
              agent_type_id,
              agent_model_id,
              dispatch_prompt_chars,
              dispatch_brief_ref,
              dispatch_name,
              result_chars,
              read_kind,
              read_targets,
              write_targets,
              is_reread,
              is_replay)
            VALUES (
              %(file_id)s,
              %(line_num)s,
              %(idx)s,
              %(ts)s,
              %(tool_name_id)s,
              %(model_id)s,
              %(tool_use_id)s,
              %(is_error)s,
              %(error_kind_id)s,
              %(error_text)s,
              %(lines_added)s,
              %(lines_deleted)s,
              %(agent_type_id)s,
              %(agent_model_id)s,
              %(dispatch_prompt_chars)s,
              %(dispatch_brief_ref)s,
              %(dispatch_name)s,
              %(result_chars)s,
              %(read_kind)s,
              %(read_targets)s,
              %(write_targets)s,
              %(is_reread)s,
              %(is_replay)s)
            """,
            parsed["tool_uses"],
        )
    if parsed["records"]:
        # long_context is the lane or Claude meter-member decision; other
        # records store NULL, read as FALSE. Persist its effective multiplier
        # pair with cost so bucket folds preserve interrupted reprice eras.
        # Store web_search_requests only when the transcript carries a count.
        #
        # Stamp pricing_version and rate_fingerprint from the current constants
        # and pair data via _resolve_dims, rather than trusting parse fields.
        # Cost, long_context, multipliers and staleness markers go to the
        # records_price sidecar (fillfactor 50), one row per records row, so
        # repricing rewrites narrow sidecar pages instead of records.
        for rec in parsed["records"]:
            rec.update({k: rec.get(k) for k in (
                "long_context", "provider", "web_search_requests",
                "is_replay", "generation_s", "output_tokens_per_s")})
            factors = (pricing.long_context_factors(rec["model"])
                       if rec.get("long_context") else (None, None))
            rec["long_context_input_mult"], rec["long_context_output_mult"] = factors
            for column in _RECORD_INT4:
                rec[column] = _int4(rec[column], f"records.{column}")
            # The parser emits flag names; storage uses an INT4 mask. Disabled
            # turn_context arrives as None and must stay NULL to mark collection
            # off; flags_to_mask(None) would silently turn it into 0.
            rec["turn_flags"] = (
                None if rec["turn_flags"] is None
                else turn_flag_bits.flags_to_mask(rec["turn_flags"]))
            # Classify the NUL-stripped uuid text into its one storage slot;
            # reads rebuild it byte-identically. Parse output stays textual, so
            # this storage conversion does not affect parser/browser lockstep.
            parts = uuid_split.split_uuid(rec.get("uuid"))
            rec["uuid_col"] = (uuid.UUID(parts.uuid_col)
                               if parts.uuid_col is not None else None)
            rec["seq"] = parts.seq
            rec["fallback"] = parts.fallback
            rec["file_id"] = file_id
        cur.executemany(
            """
            INSERT INTO records (
              file_id,
              line_num,
              uuid_col,
              seq,
              fallback,
              request_id,
              ts,
              model_id,
              fresh_tokens,
              cache_creation_tokens,
              cache_read_tokens,
              cache_write_tokens,
              output_tokens,
              eph5_tokens,
              eph1h_tokens,
              web_search_requests,
              text_chars,
              reply_latency_s,
              generation_s,
              output_tokens_per_s,
              stop_reason_id,
              effort_id,
              thinking_tokens,
              cli_version_id,
              turn_flags,
              turn_tool_results,
              provider_id,
              is_replay)
            VALUES (
              %(file_id)s,
              %(line_num)s,
              %(uuid_col)s,
              %(seq)s,
              %(fallback)s,
              %(request_id)s,
              %(ts)s,
              %(model_id)s,
              %(fresh_tokens)s,
              %(cache_creation_tokens)s,
              %(cache_read_tokens)s,
              %(cache_write_tokens)s,
              %(output_tokens)s,
              %(eph5_tokens)s,
              %(eph1h_tokens)s,
              %(web_search_requests)s,
              %(text_chars)s,
              %(reply_latency_s)s,
              %(generation_s)s,
              %(output_tokens_per_s)s,
              %(stop_reason_id)s,
              %(effort_id)s,
              %(thinking_tokens)s,
              %(cli_version_id)s,
              %(turn_flags)s,
              %(turn_tool_results)s,
              %(provider_id)s,
              %(is_replay)s)
            """,
            parsed["records"],
        )
        # The sidecar row per records row: same keys, so one pass of
        # the parsed rows writes both halves of the pair (the FK on
        # records_price(file_id, line_num) enforces it from here on).
        cur.executemany(
            """
            INSERT INTO records_price (
              file_id,
              line_num,
              cost_usd,
              long_context,
              long_context_input_mult,
              long_context_output_mult,
              pricing_version_id,
              rate_fingerprint_id)
            VALUES (
              %(file_id)s,
              %(line_num)s,
              %(cost_usd)s,
              %(long_context)s,
              %(long_context_input_mult)s,
              %(long_context_output_mult)s,
              %(pricing_version_id)s,
              %(rate_fingerprint_id)s)
            """,
            parsed["records"],
        )


def _persist(obj, proj, parsed, parser_version) -> None:
    """Persist one file atomically in one transaction.

    Derive session_id/is_main from key_layout.classify on the object key,
    so subagents are never main. Use the walk's resolved project_id:
    re-deriving it from the key would split a merged lane project when
    its marker read fails.

    Facts reference files.file_id and resolved dimension ids. Public
    file_key text stays on files and in responses (SV-FILES-RECORDS)."""
    stamp = parsed.get('collection_flags')
    if stamp:
        # Final storage boundary; use the worker's actual stamp, never ambient CONFIG.
        mask_collected_fields(parsed, CollectionPolicy(frozenset(stamp), COLLECTION_SPECS))
    info = key_layout.classify(r2.split_key(obj.key)[1])
    if info is None:
        raise ValueError(f"not a transcript key: {obj.key}")
    session_id, is_main = info.session_id, info.is_main
    project_id = proj["project_id"]
    with db.viz_conn() as c, c.cursor() as cur:
        # NUL strip BEFORE any SQL: the one gate every transcript-derived
        # text value passes on its way into the three tables.
        # (The project row's display_name merge rules live with its
        # statement, backend/ingest_identity.py.)
        proj = {k: v for k, v in proj.items() if k != "case_counts"}
        proj["display_name_set"] = bool(proj.get("display_name_set"))
        proj = _strip_nul(proj)
        parsed = _strip_nul(parsed)
        ingest_identity.upsert_project(cur, proj)
        file_id = ingest_identity.upsert_file(cur, {
            "file_key": obj.key,
            "project_id": project_id,
            "session_id": session_id,
            "is_main": is_main,
            "r2_etag": obj.etag,
            "r2_size_bytes": obj.size,
            "r2_last_modified": obj.last_modified,
            "parsed_at": datetime.now(timezone.utc),
            "parser_version": parser_version,
            **ctx_columns(parsed["ctx_turns"]),
            "turn_count": parsed["turn_count"],
            "prompt_count": parsed["prompt_count"],
            "prompt_ts": parsed.get("prompt_ts", []),
            "rate_limit_hits": json.dumps(
                parsed.get("rate_limit_hits", []), default=str
            ),
            "agent_type": parsed.get("agent_type", parse.DEFAULT_AGENT_TYPE),
            "models": parsed.get("models", []),
            "teammate_name": parsed.get("teammate_name"),
            "collection_flags": (list(stamp)
                                 if stamp is not None else None),
        })
        # Wipe this file's facts by file_id before re-inserting them:
        # an upsert on `files` means records/tool_uses need an explicit
        # DELETE (tool_uses also cascades from files, but a reparse must
        # not leave stale rows behind either way).
        cur.execute("DELETE FROM records WHERE file_id = %s", (file_id,))
        cur.execute("DELETE FROM tool_uses WHERE file_id = %s", (file_id,))
        _resolve_dims(cur, parsed)
        _write_facts(cur, parsed, file_id)
        c.commit()
