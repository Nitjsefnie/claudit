"""Agent-sidecar classification: what a transcript's meta.json sidecar means.

Split out of backend/parse.py, which sat exactly at pylint's 1000-line
gate -- the same seam ingest_rollups.py was split along. Nothing here
parses a transcript: the module owns sidecar classification -- the role
a sidecar names (sidecar_agent_role), whether it marks a named teammate,
and the teammate_name apply_agent_sidecar records for the ingest-time
join (ingest_rollups.resolve_teammate_agent_types) to resolve against
the lead's dispatch.
"""
from __future__ import annotations

from orjson import JSONDecodeError, loads

from backend import key_layout
from backend.parse_lanes import lane_sidecar_agent_type


def _sidecar_meta(sidecar: bytes) -> dict | None:
    try:
        meta = loads(sidecar)
    except JSONDecodeError:
        return None
    return meta if isinstance(meta, dict) else None


def _is_teammate(meta: dict) -> bool:
    """Whether agentType may be a teammate NAME: Claude Code marks a named
    teammate ``taskKind: in_process_teammate``, and an agentType equal to
    ``name`` is that case unmarked -- unless a ``toolUseId`` (which no
    teammate sidecar carries) shows a plain subagent named after its role."""
    name = meta.get("name")
    return (meta.get("taskKind") == "in_process_teammate"
            or (isinstance(name, str) and bool(name)
                and "toolUseId" not in meta and meta.get("agentType") == name))


def _teammate_candidate(meta: dict) -> bool:
    """An older release wrote a teammate's sidecar as ONLY {"agentType": X}
    -- no name, no taskKind, no toolUseId. Not a verdict: the same shape
    could be a plain subagent's role marker, so the parsed role STANDS and
    the ingest-time join (resolve_teammate_agent_types) decides whether the
    session's lead dispatched an Agent call named X. A toolUseId (a plain
    subagent named after its dispatch), any taskKind (the writer marks the
    kinds it knows), and a fork's isFork keep an ordinary role marker."""
    return ("name" not in meta and "toolUseId" not in meta
            and "taskKind" not in meta and not meta.get("isFork")
            and isinstance(meta.get("agentType"), str)
            and bool(meta["agentType"]))


def _meta_role(meta: dict) -> str | None:
    name = meta.get("name")
    if _is_teammate(meta) and not (isinstance(name, str) and name
                                   and meta.get("agentType") != name):
        return None
    spec = meta.get("launch_spec")
    for role in (meta.get("agentType"), meta.get("subagent_type"),
                 spec.get("subagent_type") if isinstance(spec, dict)
                 else None):
        if isinstance(role, str) and role:
            return role
    return None


def sidecar_agent_role(sidecar: bytes) -> str | None:
    """The role a subagent's meta.json sidecar names, or None.

    Claude Code writes ``{"agentType": ...}``; kimi-cli writes
    ``{"subagent_type": ..., "launch_spec": {"subagent_type": ...}}``,
    the top-level value first. Anything else — undecodable bytes, a
    top level that is not an object, an empty or non-string value, a
    teammate's agentType that is its name (_is_teammate) — is no role.
    """
    return _meta_role(_sidecar_meta(sidecar) or {})


def apply_agent_sidecar(parsed: dict, sidecar: bytes, key: str) -> dict:
    """Fill a parse's agent_type from its transcript's meta.json sidecar.

    Precedence is in-band role > sidecar role > DEFAULT_AGENT_TYPE: a
    transcript that named its own role (``attributionAgent``,
    ``agent-setting``, a lane's session_meta / profileName) keeps it,
    and an unusable sidecar changes nothing. `key` is the transcript's
    OBJECT key (no bucket): which normalisation the role takes follows
    the key layout, not the sniffed format -- a sidecar in the lane tree
    goes through the lane's (lane_sidecar_agent_type), so its name for
    the default profile is DEFAULT_AGENT_TYPE here too; a Claude one is
    stored verbatim, like ``attributionAgent``. A teammate's sidecar also
    sets ``teammate_name``, which ingest joins to the lead's dispatch; the
    role stored here stands only when no dispatch joins. An agentType-only
    sidecar (an older release's teammate) is stored the same way, as a
    candidate the join decides, its role still standing meanwhile. Mutates
    and returns `parsed`.
    """
    if parsed.get("agent_type_in_band"):
        return parsed
    meta = _sidecar_meta(sidecar) or {}
    if _is_teammate(meta):
        name = meta.get("name") or meta.get("agentType")
        parsed["teammate_name"] = name if isinstance(name, str) else None
    elif _teammate_candidate(meta):
        parsed["teammate_name"] = meta["agentType"]
    role = _meta_role(meta)
    if role is None:
        return parsed
    parsed["agent_type"] = (lane_sidecar_agent_type(role)
                            if key_layout.in_lane_tree(key) else role)
    return parsed
