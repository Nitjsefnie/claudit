"""Shared constants that would otherwise create import cycles.

Kept dependency-free: any module may import this one, and it imports no
other backend module.
"""
from __future__ import annotations

from pathlib import Path

# Display bucket widths /api/reply-latency can ask for, from
# api._bucket_seconds. 300 (the 24h view) is deliberately absent: a row
# per 5 minutes of all history to serve one day is not worth it, and that
# range stays on the live path.
LATENCY_BUCKETS = (3600, 21600, 43200, 86400)

# Context-size buckets for `ctx_cost_rollup` / the Cost by Context panel.
# The per-call window is fresh + cache_creation + cache_read, bucketed to
# CTX_BUCKET_WIDTH; everything at or above CTX_BUCKET_MAX folds into one
# open-ended overflow bucket keyed by CTX_BUCKET_MAX itself.
#
# Fixed width rather than logarithmic, and that is a measurement not a
# taste: over the live corpus, cost is near-flat across the window --
# 7.8% of all spend in 100-150k, still 5.2% in 700-750k, ~60% above
# 300k. Log buckets would compress precisely the region carrying the
# money into a handful of fat bars.
#
# These edges are BAKED INTO STORED ROWS, exactly like LATENCY_BUCKETS:
# a read cannot re-bucket to a different width, so changing either
# constant requires rebuilding the rollup (bump nothing else -- the
# rollup derives from stored `records` columns, so no reparse).
CTX_BUCKET_WIDTH = 50_000
CTX_BUCKET_MAX = 1_000_000

# Above this, a record's context is not a request -- it is a cumulative
# counter. Set at twice the largest published window (the [1m] variants),
# so a real request can never reach it while a whole-session total always
# will. Used only to keep such a row out of the ctx_turns TRACE, whose
# y-axis is scaled off the maximum: one 115.8M row in the `zai` bucket,
# written by another harness under the all-zeros sentinel session id,
# flattened every real trace on glmmeter to a hairline. The record itself
# is still parsed, stored and priced -- this is a plausibility bound on
# one derived series, not a data-layer filter.
MAX_PLAUSIBLE_CTX = 2 * CTX_BUCKET_MAX


def ctx_bucket(ctx_tokens: int) -> int:
    """Lower edge of the bucket `ctx_tokens` falls in.

    Mirrored by the SQL in ingest.rebuild_ctx_cost_rollup(); the two must
    agree, and test_ctx_cost_rollup.py pins this side of it.
    """
    if ctx_tokens >= CTX_BUCKET_MAX:
        return CTX_BUCKET_MAX
    if ctx_tokens <= 0:
        return 0
    return (ctx_tokens // CTX_BUCKET_WIDTH) * CTX_BUCKET_WIDTH


def _read_version() -> str:
    """Repo version from the root VERSION file, or "unknown".

    The file is the single source of truth: `.github/workflows/release.yml`
    tags a release when it changes, and `speed.yml` compares against the
    release it names. Read once at import — it cannot change under a
    running process without a redeploy.

    A deploy that omits the file (a tarball, a partial checkout) gets
    "unknown" rather than a crash: /health reporting an unknown version is
    strictly better than /health not answering at all.
    """
    try:
        text = (Path(__file__).resolve().parent.parent / "VERSION").read_text(
            encoding="utf-8"
        )
    except OSError:
        return "unknown"
    return text.strip() or "unknown"


VERSION = _read_version()


# Parse-output schema/semantics version. Ingest reparses every file whose
# stored `parser_version` differs from this, so it is the ONLY switch that
# forces a full reparse.
#
# It lives HERE, in code, and not in the environment: a parser change and the
# reparse it requires must travel in the same commit. When this was read from
# .env, shipping a parser change without an operator also editing that file
# left every stored row at the old semantics with nothing to detect it, and a
# deploy that never set the variable at all sat forever on the "1" default.
#
# BUMP THIS in the same commit as any change to parse.py semantics, to a
# rate in src/pricing.json that reprices stored records, or to the set of
# columns parse_file() emits.
# Sequence continues the values previously carried in .env (last: 26);
# 27 is the first code-owned value and forces the reparse that fills the
# tool_uses failure/dispatch columns added alongside it. 28 adds the two
# dispatch briefing-shape columns to the set parse_file() emits. 29 adds
# result_chars, read_kind, read_targets, write_targets and is_reread --
# what each call put into the context window and whether it had already
# been put there. 30 widens what Bash command text yields: `sed -i` and
# python bodies as write targets, same-command `$VAR` expansion,
# sequentially rebound / helper-mediated python literals as churn, a
# heredoc write surviving a later stage's failure, and `Exit code N`
# classified as tool_error rather than failed. 31 recognises a python
# interpreter given by path (`.venv/bin/python -`). 32 recovers literal
# printf/sed append payloads, explicit copy/move/Perl targets, and bounded
# Python path loops and string concatenation.
# 34 invalidates deployed results for the stream/operand and binding-provenance
# corrections. 35 adds literal brace words and refuses brace expansion while
# retaining those corrections, invalidating files already parsed at 34.
# 36 counts explicit quoted exit-status echo markers reaching a file.
# 37 estimates unknown Bash writes and recovers literal echo/sed substitutions.
# 38 resolves Windows targets lexically and matches equivalent reread keys.
# 43 reads prompt_snapshot diffs, session_context and the turn-start cwd
# into turn_flags.
# 44 prices bonsai-2-27b (local llama.cpp lane) at zero.
# 45 prices claude-opus-5-5.
# 46 diffs Edit and python replace() payloads instead of counting both whole.
# 47 multiplies a heredoc's churn by its enclosing literal `for` loops.
# 48 prices a cache write with no declared TTL at the 1h rate, not 5m.
# 49 adds the Codex and Kimi rate tables and the long-context meter.
# 50 routes every transcript through the format dispatcher: Codex and
# Kimi transcripts parse instead of landing in the claude parser.
# 51 persists the Codex long-context meter on records.long_context.
# 52 keys a lane project by the Claude slug of the directory its
# project.json marker names, so one directory is ONE project across
# buckets; the bump reparses every file and re-derives project ids
# (codex/kimi deploys re-key their projects on that run).
# 53 case-folds a Windows project slug (a drive letter followed by '--')
# to lowercase, so a Windows directory whose sessions were ingested
# under differently-cased paths is ONE project; the bump reparses every
# file and merges Windows projects that differed only in case.
# 54 prices OpenRouter's :free-suffix and stealth/-prefix model ids at
# zero (matched on id shape, on the raw id and its normalised form, so a
# spelling variant cannot dodge it); the bump reparses every file so
# stored cost_usd stops carrying the Opus-list estimate those records
# previously resolved to.
# 56 merges a Claude transcript's requestId-less lines on message.id
# (Z.ai writes no requestId, and repeats a message's full usage on
# every content-block line); the bump reparses every file so stored
# zai/llama rows stop counting one response once per line.
# 57 reads the agent role off Codex and kimi-code transcripts (Codex
# session_meta agent_role, kimi-code config.update profileName) instead
# of filing every lane file under the default type, records what a lane
# dispatch asked for (Codex spawn_agent, Kimi Agent) on tool_uses,
# stores each tool call's own model on tool_uses.model, and takes a
# role-less subagent's agent type from its meta.json sidecar; the bump
# reparses every file so stored rows pick all of these up.
# 58 ends lane reply latency at the first assistant output and anchors
# a kimi-code steer at its delivery.
# 59 stores an OpenRouter record's serving host (message.provider) on
# records.provider and prices it from pricing.PROVIDER_RATES; the bump
# reparses every file so stored rows carry the provider and its price.
# 60 stops storing a named teammate's name as its agent type: its
# sidecar's name goes to files.teammate_name, the dispatch's `name` to
# tool_uses.dispatch_name, and the role is joined from the lead's
# dispatch; the bump reparses every file so both columns are filled.
# 61 prices DeepSeek's and Alibaba's OpenRouter rows by their weekly UTC
# schedules (pricing.overrides): their seeded rows held the off-peak price
# for every hour; the bump reparses every file so peak-hour records take
# the peak price.
# 62 appends the OpenRouter provider rates detected at 2026-09-25T05:39:26Z;
# the bump reparses every file so a record from then on that was
# ingested before this commit reached the deploy takes the new rate.
PARSER_VERSION = "62"

#: How ingest._fetch_marker turns a project.json body into a path. Each
#: lane_markers row records the version it was read under, and a row from
#: another version is re-fetched, so bump this whenever that reading
#: changes.
MARKER_READER_VERSION = "1"

#: What a file is attributed to when the transcript records no role at
#: all. It is the roster's own fallback dispatch type, and it is also
#: where every unattributable file lands — parse.resolve_agent_type for
#: Claude transcripts, parse_lanes.lane_agent_type for lane ones.
DEFAULT_AGENT_TYPE = "general-purpose"

# The text Claude Code writes when the user cuts a reply off.
INTERRUPT_MARKER = "[Request interrupted by user"
