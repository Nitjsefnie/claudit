-- claudit schema. Bump PARSER_VERSION env var to invalidate all rows.

CREATE TABLE IF NOT EXISTS projects (
  project_id    TEXT PRIMARY KEY,
  display_name  TEXT NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL,
  last_seen_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  file_key            TEXT PRIMARY KEY,
  project_id          TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  session_id          TEXT NOT NULL,
  is_main             BOOLEAN NOT NULL,
  r2_etag             TEXT NOT NULL,
  r2_size_bytes       BIGINT NOT NULL,
  r2_last_modified    TIMESTAMPTZ NOT NULL,
  parsed_at           TIMESTAMPTZ NOT NULL,
  parser_version      TEXT NOT NULL,
  ctx_turns           JSONB NOT NULL DEFAULT '[]'::jsonb,
  turn_count          INT   NOT NULL DEFAULT 0,
  prompt_count        INT   NOT NULL DEFAULT 0,
  rate_limit_hits     JSONB NOT NULL DEFAULT '[]'::jsonb
);

ALTER TABLE files ADD COLUMN IF NOT EXISTS
  rate_limit_hits JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE files ADD COLUMN IF NOT EXISTS
  prompt_count INT NOT NULL DEFAULT 0;
-- Which agent role this transcript ran as -- parse.resolve_agent_type.
-- Per-file, not per-record: a transcript is homogeneous (a file carrying
-- isSidechain records carries nothing else). The DEFAULT matches
-- parse.DEFAULT_AGENT_TYPE so a migrated-but-not-yet-reparsed DB reads
-- as one honest "unattributed" bar rather than a NULL every consumer
-- has to special-case.
ALTER TABLE files ADD COLUMN IF NOT EXISTS
  agent_type TEXT NOT NULL DEFAULT 'general-purpose';

CREATE INDEX IF NOT EXISTS files_project_idx ON files (project_id);
CREATE INDEX IF NOT EXISTS files_session_idx ON files (session_id);
CREATE INDEX IF NOT EXISTS files_modified_idx ON files (r2_last_modified);

CREATE TABLE IF NOT EXISTS records (
  file_key                TEXT NOT NULL REFERENCES files(file_key) ON DELETE CASCADE,
  line_num                INT  NOT NULL,
  uuid                    TEXT,
  request_id              TEXT NOT NULL DEFAULT '',
  ts                      TIMESTAMPTZ,
  model                   TEXT NOT NULL,
  fresh_tokens            BIGINT NOT NULL DEFAULT 0,
  cache_creation_tokens   BIGINT NOT NULL DEFAULT 0,
  cache_read_tokens       BIGINT NOT NULL DEFAULT 0,
  output_tokens           BIGINT NOT NULL DEFAULT 0,
  eph5_tokens             BIGINT NOT NULL DEFAULT 0,
  eph1h_tokens            BIGINT NOT NULL DEFAULT 0,
  cost_usd                NUMERIC(12,6) NOT NULL DEFAULT 0,
  text_chars              BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (file_key, line_num)
);

-- Idempotent migration for existing DBs.
ALTER TABLE records ADD COLUMN IF NOT EXISTS
  text_chars BIGINT NOT NULL DEFAULT 0;
ALTER TABLE records ADD COLUMN IF NOT EXISTS
  reply_latency_s NUMERIC(10,3);

-- Cross-file uuid dedup, resolved at INGEST instead of on every read.
-- `records` is immutable between ingests, but every read endpoint was
-- re-running DISTINCT ON (uuid) over the whole table -- a 296k-row sort
-- to drop ~3.5% duplicates, per request, per range, per project. This
-- flag marks the row that DISTINCT ON (uuid) ORDER BY uuid, file_key,
-- line_num would have kept; readers filter on it instead of sorting.
-- Recomputed by ingest.recompute_canonical() after every ingest, since
-- adding or removing a FILE can change which row wins for a uuid.
--
-- Defaults to TRUE so a freshly-migrated DB over-counts nothing before
-- the first recompute -- it behaves exactly like the pre-dedup state
-- (every row kept) rather than silently dropping rows.
ALTER TABLE records ADD COLUMN IF NOT EXISTS
  is_canonical BOOLEAN NOT NULL DEFAULT TRUE;

-- Pre-aggregated usage, rebuilt at ingest (ingest.rebuild_rollup).
--
-- `records` only changes when an ingest runs, but every dashboard request
-- was re-deriving the entire history from raw rows: five separate full
-- aggregate passes over ~285k canonical records, per request, per range,
-- per project. This holds the same numbers already summed.
--
-- Grain is (session_id, hour, model) deliberately:
--   * keyed by HOUR, not by session, so a range filter sums only the
--     in-range hours -- a session straddling the boundary would otherwise
--     be counted whole. Sums, counts and min/max ts all compose exactly.
--   * carrying MODEL means a session's dominant model is argmax(requests)
--     over the in-range rows -- exact, not an approximation of MODE().
-- What does NOT compose is PERCENTILE_CONT, so response-size p50/p90 is
-- still computed live from records (see /api/dashboard).
CREATE TABLE IF NOT EXISTS usage_rollup (
  session_id          TEXT        NOT NULL,
  project_id          TEXT        NOT NULL,
  hour                TIMESTAMPTZ NOT NULL,
  model               TEXT        NOT NULL,
  is_main             BOOLEAN     NOT NULL DEFAULT TRUE,
  first_ts            TIMESTAMPTZ NOT NULL,
  last_ts             TIMESTAMPTZ NOT NULL,
  requests            BIGINT      NOT NULL DEFAULT 0,
  fresh_tokens        BIGINT      NOT NULL DEFAULT 0,
  output_tokens       BIGINT      NOT NULL DEFAULT 0,
  cache_creation_tokens BIGINT    NOT NULL DEFAULT 0,
  cache_read_tokens   BIGINT      NOT NULL DEFAULT 0,
  eph5_tokens         BIGINT      NOT NULL DEFAULT 0,
  eph1h_tokens        BIGINT      NOT NULL DEFAULT 0,
  cost_usd            NUMERIC(18,8) NOT NULL DEFAULT 0,
  PRIMARY KEY (session_id, hour, model, is_main)
);
CREATE INDEX IF NOT EXISTS usage_rollup_hour_idx ON usage_rollup (hour);
CREATE INDEX IF NOT EXISTS usage_rollup_project_idx ON usage_rollup (project_id, hour);

-- Pre-aggregated tool calls, rebuilt at ingest alongside usage_rollup.
-- Serves /api/tool-usage, /api/tool-error-rate, and the dashboard line-churn
-- series without scanning raw tool_uses on hourly-or-coarser views.
--
-- The two endpoints count DIFFERENT populations, so both are stored:
--   n_total  every tool_use in the group           -> /api/tool-usage
--   n_rated  those with is_error NOT NULL AND a matching records row
--            (tool_error_rate INNER JOINs records) -> denominator
--   n_error  of those, the ones that errored       -> numerator
--   lines_*  additive Edit/Write churn             -> /api/dashboard
-- `model` is '' when no records row matched; tool_error_rate excludes
-- those, which is what its inner join did.
CREATE TABLE IF NOT EXISTS tool_rollup (
  hour        TIMESTAMPTZ NOT NULL,
  project_id  TEXT        NOT NULL,
  model       TEXT        NOT NULL,
  tool_name   TEXT        NOT NULL,
  n_total     BIGINT      NOT NULL DEFAULT 0,
  n_rated     BIGINT      NOT NULL DEFAULT 0,
  n_error     BIGINT      NOT NULL DEFAULT 0,
  lines_added BIGINT      NOT NULL DEFAULT 0,
  lines_deleted BIGINT    NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, project_id, model, tool_name)
);
ALTER TABLE tool_rollup ADD COLUMN IF NOT EXISTS
  lines_added BIGINT NOT NULL DEFAULT 0;
ALTER TABLE tool_rollup ADD COLUMN IF NOT EXISTS
  lines_deleted BIGINT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS tool_rollup_hour_idx ON tool_rollup (hour);
CREATE INDEX IF NOT EXISTS tool_rollup_project_idx ON tool_rollup (project_id, hour);

-- Pre-aggregated failure causes, grain
-- (hour, project_id, model, tool_name, error_kind).
--
-- tool_rollup already carries n_error, which answers "how many failed"
-- but not "why". This carries the split. Only errored calls produce a
-- row, and error_kind has three values, so it stays a small fraction of
-- tool_rollup's size.
--
-- `n` is a pure count, so it composes: summing it across hours,
-- projects, models or tools is valid exactly as tool_rollup's counters
-- are. error_kind is IN the grain rather than a set of columns so a new
-- kind does not need a migration.
--
-- The free-text drill-down (tool_uses.error_text) deliberately does NOT
-- appear here: unbounded cardinality has no place in a rollup grain, and
-- errored rows are few enough to query raw behind the partial index.
CREATE TABLE IF NOT EXISTS tool_error_rollup (
  hour        TIMESTAMPTZ NOT NULL,
  project_id  TEXT        NOT NULL,
  model       TEXT        NOT NULL,
  tool_name   TEXT        NOT NULL,
  error_kind  TEXT        NOT NULL,
  n           BIGINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, project_id, model, tool_name, error_kind)
);
CREATE INDEX IF NOT EXISTS tool_error_rollup_hour_idx
  ON tool_error_rollup (hour);

-- Pre-aggregated subagent dispatches, grain
-- (hour, project_id, agent_type, agent_model).
--
-- Counted from the CALL, so a dispatch appears here whether or not the
-- subagent ever wrote a JSONL. `n` is a pure count and composes.
--
-- An absence is the signal this table exists to make cheap: an agent
-- type that stops being dispatched leaves rows that simply stop, which
-- a window-over-window comparison of totals will not show.
CREATE TABLE IF NOT EXISTS dispatch_rollup (
  hour        TIMESTAMPTZ NOT NULL,
  project_id  TEXT        NOT NULL,
  agent_type  TEXT        NOT NULL,
  agent_model TEXT        NOT NULL,
  n           BIGINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, project_id, agent_type, agent_model)
);
CREATE INDEX IF NOT EXISTS dispatch_rollup_hour_idx
  ON dispatch_rollup (hour);

-- Pre-aggregated dispatch BRIEFING SHAPE, grain
-- (hour, project_id, agent_type, brief_ref).
--
-- dispatch_rollup answers what was dispatched. This answers how it was
-- briefed: `brief_ref` is true when the call's opening directive points
-- at a written brief file, false when the call carries its instructions
-- inline. The distinction is whether the brief outlives the dispatch --
-- an inline brief is re-authored per call and cannot be reused, and the
-- scratch-directory paths the referenced ones point at mostly no longer
-- exist either.
--
-- `n` is a pure count and composes. `prompt_chars` is a SUM, so it
-- composes the same way; divide by `n` for a mean at any bucket width.
-- Neither the prompt text nor a fingerprint of it is stored: unbounded
-- cardinality has no place in a rollup grain, and the text is the most
-- sensitive thing a transcript holds.
CREATE TABLE IF NOT EXISTS dispatch_brief_rollup (
  hour         TIMESTAMPTZ NOT NULL,
  project_id   TEXT        NOT NULL,
  agent_type   TEXT        NOT NULL,
  brief_ref    BOOLEAN     NOT NULL,
  n            BIGINT      NOT NULL DEFAULT 0,
  prompt_chars BIGINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, project_id, agent_type, brief_ref)
);
CREATE INDEX IF NOT EXISTS dispatch_brief_rollup_hour_idx
  ON dispatch_brief_rollup (hour);

-- Pre-aggregated cost-by-context-size for /api/cost-by-context.
--
-- `usage_rollup` cannot serve this panel: its grain sums fresh/create/
-- read across a whole (session, hour, model), which destroys the
-- PER-CALL window size the x-axis is made of. Bucketing by that window
-- at ingest keeps it, and what is stored are pure sums, so they compose
-- across hours, projects and models exactly like tool_rollup's do.
--
-- ctx_bucket is the bucket's LOWER EDGE in tokens (0, 50000, ...,
-- 1000000), from constants.ctx_bucket(); the top bucket is open-ended
-- and holds every call at or above CTX_BUCKET_MAX, which is not
-- hypothetical -- the [1m] model variants exceed it.
--
-- Derived from stored `records` columns (fresh + cache_creation +
-- cache_read), so widening the buckets needs a rollup rebuild but NOT a
-- PARSER_VERSION bump and NOT a reparse.
CREATE TABLE IF NOT EXISTS ctx_cost_rollup (
  hour        TIMESTAMPTZ NOT NULL,
  project_id  TEXT        NOT NULL,
  model       TEXT        NOT NULL,
  ctx_bucket  BIGINT      NOT NULL,
  requests    BIGINT      NOT NULL DEFAULT 0,
  cost_usd    NUMERIC(18,8) NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, project_id, model, ctx_bucket)
);
CREATE INDEX IF NOT EXISTS ctx_cost_rollup_hour_idx
  ON ctx_cost_rollup (hour);
CREATE INDEX IF NOT EXISTS ctx_cost_rollup_project_idx
  ON ctx_cost_rollup (project_id, hour);

-- Pre-aggregated cost-by-agent-type for /api/cost-by-agent.
--
-- `usage_rollup` cannot serve this one either: agent_type lives on
-- `files` and that grain has already summed across every file in a
-- (session, hour, model). A session's main transcript and its subagent
-- sidecars share a session_id, so folding them together is exactly the
-- distinction this panel exists to draw.
--
-- Stored columns are pure sums, so they compose across hours, projects
-- and models like the other two composable rollups. Derived from
-- `files.agent_type`, so re-attributing a file needs a reparse (bump
-- PARSER_VERSION) but re-aggregating does not.
CREATE TABLE IF NOT EXISTS agent_rollup (
  hour        TIMESTAMPTZ NOT NULL,
  project_id  TEXT        NOT NULL,
  model       TEXT        NOT NULL,
  agent_type  TEXT        NOT NULL,
  requests    BIGINT      NOT NULL DEFAULT 0,
  output_tokens BIGINT    NOT NULL DEFAULT 0,
  cost_usd    NUMERIC(18,8) NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, project_id, model, agent_type)
);
CREATE INDEX IF NOT EXISTS agent_rollup_hour_idx
  ON agent_rollup (hour);
CREATE INDEX IF NOT EXISTS agent_rollup_project_idx
  ON agent_rollup (project_id, hour);

-- Pre-aggregated reply-latency bands + outlier dots for /api/reply-latency.
--
-- Percentiles do NOT compose across buckets, so unlike usage_rollup this
-- cannot be stored at one fine grain and summed up. What makes it
-- precomputable anyway is that the display buckets are epoch-aligned and
-- deterministic — floor(epoch / bucket_s) * bucket_s does not move with
-- `now` — and the UI only ever uses five widths (300/3600/21600/43200/
-- 86400). So the percentiles are computed once PER bucket_s and a range
-- filter merely selects which buckets to return. Exact, not approximate.
-- 300s (the 24h view) is excluded: it would need a row per 5 minutes of
-- all history to serve one day, so that range keeps the live path.
--
-- project_id = '' is the ALL-PROJECTS row. It has to be stored
-- separately because a filter changes the population inside each
-- (bucket, model) group, and p50 over all projects is not derivable from
-- the per-project p50s. The model filter needs no such treatment — the
-- rows are already grouped by model, so filtering selects whole groups.
--
-- `outliers` holds the top/bottom 1% dots the panel draws, as
-- [{ts, latency_s, file_key, line_num, kind}], only for buckets with
-- n >= 100 (1% of fewer would just be the min/max).
CREATE TABLE IF NOT EXISTS latency_rollup (
  bucket_s    INTEGER     NOT NULL,
  bucket      TIMESTAMPTZ NOT NULL,
  project_id  TEXT        NOT NULL,
  model       TEXT        NOT NULL,
  n           BIGINT      NOT NULL,
  p10         DOUBLE PRECISION,
  p50         DOUBLE PRECISION,
  p90         DOUBLE PRECISION,
  outliers    JSONB       NOT NULL DEFAULT '[]'::jsonb,
  PRIMARY KEY (bucket_s, bucket, project_id, model)
);
CREATE INDEX IF NOT EXISTS latency_rollup_lookup_idx
  ON latency_rollup (bucket_s, project_id, bucket);

-- Only ~19% of records carry a reply_latency_s; a partial covering index
-- keeps the live path (and the rollup build) off the other 81%.
CREATE INDEX IF NOT EXISTS records_latency_idx ON records (ts)
  INCLUDE (model, reply_latency_s, file_key, line_num)
  WHERE reply_latency_s IS NOT NULL;

CREATE INDEX IF NOT EXISTS records_uuid_idx ON records (uuid) WHERE uuid IS NOT NULL;
CREATE INDEX IF NOT EXISTS records_ts_idx ON records (ts);
-- Every read endpoint filters `is_canonical AND ts >= ...`.
CREATE INDEX IF NOT EXISTS records_canonical_ts_idx
  ON records (ts) WHERE is_canonical;
CREATE INDEX IF NOT EXISTS records_model_idx ON records (model);
CREATE INDEX IF NOT EXISTS records_request_idx ON records (request_id) WHERE request_id <> '';

CREATE TABLE IF NOT EXISTS tool_uses (
  file_key   TEXT NOT NULL REFERENCES files(file_key) ON DELETE CASCADE,
  line_num   INT  NOT NULL,
  idx        INT  NOT NULL,            -- index within the assistant msg's content[]
  ts         TIMESTAMPTZ,
  tool_name  TEXT NOT NULL,
  PRIMARY KEY (file_key, line_num, idx)
);

CREATE INDEX IF NOT EXISTS tool_uses_ts_idx   ON tool_uses (ts);
CREATE INDEX IF NOT EXISTS tool_uses_tool_idx ON tool_uses (tool_name);

-- 2026-05-08: tool_uses.is_error filled at parse time by matching
-- assistant tool_use.id to user tool_result.tool_use_id within the
-- same JSONL file. NULL = unmatched (no later tool_result block in
-- the file). Read endpoints WHERE is_error IS NOT NULL to compute
-- the error rate over settled calls only.
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS is_error BOOLEAN;

-- 2026-07-29 (issue #10): per-call line churn for edit/write tools,
-- derived from the tool CALL arguments at parse time (see
-- parse._tool_churn). Two separate POSITIVE series — deletions are
-- not negative additions. Errored calls are zeroed at parse. Both
-- default 0, so historical rows read as "no churn known" until a
-- PARSER_VERSION bump reparses them.
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS
  lines_added BIGINT NOT NULL DEFAULT 0;
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS
  lines_deleted BIGINT NOT NULL DEFAULT 0;

-- 2026-09-07: what each call put INTO the context, and whether it had
-- already been put there.
--
-- `result_chars` is the size of the tool_result, images included: an
-- image block's base64 payload is the largest thing a result can carry
-- and is 92% of all duplicated read bytes over the live corpus, so a
-- text-only measure would report the cheapest half and call it a total.
-- Recorded for errored calls too — a failure occupies the window
-- exactly as a success does.
--
-- `read_targets` / `write_targets` are ARRAYS because one call can name
-- several files (`cat a b`, `diff a b`), and `read_kind` is 'whole' or
-- 'slice'. That split is load-bearing, not decorative: `grep -n x big.py`
-- and `cat big.py` both name one file and only the second put it in
-- context, so scoring them alike ranks grep-then-narrow as more wasteful
-- than one indiscriminate cat.
--
-- `is_reread` is resolved at PARSE time (parse._resolve_rereads), not
-- derivable at read time without an ordered self-join per file, and NULL
-- for anything that is not a settled whole-file read. Targets come from
-- tool arguments for Read/Edit/Write and from COMMAND TEXT for Bash
-- (backend/bash_reads.py) — under bypass permissions Bash is ~79% of the
-- read surface, so an argument-only reader would see almost none of it.
--
-- Deliberately NOT rolled up: read_targets is unbounded cardinality,
-- the same reason error_text stays out of tool_error_rollup. These are
-- for querying raw.
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS result_chars BIGINT;
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS read_kind TEXT;
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS read_targets TEXT[];
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS write_targets TEXT[];
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS is_reread BOOLEAN;

-- Partial: only settled whole-file reads carry the flag, so the index
-- stays a small fraction of the table.
CREATE INDEX IF NOT EXISTS tool_uses_reread_idx
  ON tool_uses (ts) WHERE is_reread;
-- GIN over the target arrays so `WHERE '<path>' = ANY(read_targets)`
-- and the containment operators do not sequential-scan 473k rows.
CREATE INDEX IF NOT EXISTS tool_uses_read_targets_idx
  ON tool_uses USING GIN (read_targets);

-- 2026-09-07: why a settled tool call failed.
--
-- `is_error` says THAT a call failed and nothing about why, so every
-- question about failure causes meant leaving the DB for the raw
-- transcripts. Two columns, both NULL unless is_error is true:
--
--   error_kind  coarse and HARNESS-GENERIC -- 'rejected' (user or
--               permission denial), 'tool_error' (a <tool_use_error>
--               wrapper from the harness), 'failed' (everything else).
--               Bounded cardinality, so it can carry a rollup grain.
--   error_text  the leading parse.ERROR_TEXT_MAX characters of the
--               failed result, for GROUP BY drill-down.
--
-- A PreToolUse hook denial lands in 'failed' on purpose: its wording is
-- the deploy's, not Claude Code's, so classifying it in the parser would
-- bake one operator's hook set into a general tool. Grouping on
-- error_text separates those without that coupling.
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS error_kind TEXT;
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS error_text TEXT;

-- 2026-09-07: what a subagent dispatch ASKED for, read off the Agent/Task
-- call arguments. `files.agent_type` records what actually ran and so
-- exists only when the subagent wrote a JSONL; these two exist for every
-- dispatch, including ones that never produced a file. NULL on every
-- non-dispatch tool, and on a dispatch that named neither field.
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS agent_type TEXT;
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS agent_model TEXT;

-- 2026-09-07: how a dispatch was BRIEFED, from the same call arguments.
-- dispatch_prompt_chars is the prompt's length; dispatch_brief_ref is
-- true when its opening directive points at a written brief file rather
-- than carrying the instructions inline. The prompt text itself is not
-- stored -- it is unbounded and it is the most sensitive thing in a
-- transcript, and neither question needs it. NULL on every non-dispatch
-- tool and on a dispatch carrying no prompt argument.
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS dispatch_prompt_chars INT;
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS dispatch_brief_ref BOOLEAN;

-- Errored rows are a small minority, so a partial index keeps the
-- drill-down cheap without carrying the whole table.
CREATE INDEX IF NOT EXISTS tool_uses_error_kind_idx
  ON tool_uses (error_kind, ts) WHERE error_kind IS NOT NULL;
CREATE INDEX IF NOT EXISTS tool_uses_agent_type_idx
  ON tool_uses (agent_type, ts) WHERE agent_type IS NOT NULL;

CREATE TABLE IF NOT EXISTS ingest_runs (
  id              BIGSERIAL PRIMARY KEY,
  started_at      TIMESTAMPTZ NOT NULL,
  finished_at     TIMESTAMPTZ,
  trigger         TEXT NOT NULL,
  r2_listed       INT,
  reparsed        INT,
  inserted        INT,
  deleted         INT,
  error           TEXT
);

-- 2026-09-01: models whose records must not count toward any panel.
--
-- Claude Code writes every session under ~/.claude/projects/ regardless
-- of which endpoint served it, so resuming a session on the other lane
-- (Anthropic vs the z.ai GLM endpoint) interleaves that provider's
-- assistant entries into a transcript the archiver has already filed
-- under this deploy's bucket. The rows are real usage, just not OUR
-- usage: pricing them against the Anthropic table invents a cost.
--
-- Rows are patterns matched with `model ILIKE pattern`, so one
-- 'glm-%' covers a whole family and a bare model id still matches
-- exactly. Ships EMPTY on purpose -- this same codebase is deployed
-- over the zai bucket as glmmeter, where 'glm-%' would suppress
-- everything. Populate per deploy:
--
--   INSERT INTO suppressed_models (pattern, note)
--        VALUES ('glm-%', 'z.ai lane; see glmmeter');
--
-- Enforced by ingest.purge_suppressed(), which runs before the
-- canonical pass on every ingest: matching `records` and their
-- `tool_uses` are deleted, so every read path and rollup excludes
-- them without a filter of its own. Removing a pattern brings the
-- rows back only on a reparse (bump PARSER_VERSION).
CREATE TABLE IF NOT EXISTS suppressed_models (
  pattern   TEXT PRIMARY KEY,
  note      TEXT,
  added_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
