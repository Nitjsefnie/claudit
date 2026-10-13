-- File and dimension surrogate ids (issue #959): the compaction DDL.
--
-- Applied near the end of schema's one stamped transaction
-- (backend/schema_files.py), followed by schema_types.sql and
-- schema_records_price.sql: every earlier file's ALTERs land on the OLD
-- text shape, in the same transaction (SV-SCHEMA-AUTOAPPLY). Mechanism
-- (R1): drop-and-recreate for the derived/fact tables (`records`,
-- `tool_uses`, every rollup) — schema_widths.sql, the file immediately
-- before this one, drops whichever of them does not carry the canonical
-- signature, so a fresh database, a pre-#959 deploy and a database
-- stamped by this branch at the earlier widths all reach THIS file in
-- the same shape: absent, or canonical. This file declares the
-- canonical shape and never drops; `projects`/`files`/`sessions` and
-- the dimension lookups migrate in place (PK swap and width narrowing).
-- PARSER_VERSION's bump forces the full reparse that refills the
-- dropped facts.
--
-- Storage widths (task 7, PM mandate): dimension ids and every FK
-- column referencing one are SMALLINT — BIGINT was WIDER than the text
-- it replaced for these low-cardinality vocabularies; the identity
-- surrogates file_id/proj_id/ses_id are INTEGER (2.1B ceiling); and
-- records/tool_uses DECLARE widest-first (8-byte, then 4-byte, then
-- 2-byte, then variable; booleans trail because they need no alignment
-- and nothing follows them), so alignment padding cannot eat the
-- saving.
--
-- `files.file_key` stays the stored identity (SV-FILES-RECORDS): UNIQUE
-- TEXT, children referencing it by integer `file_id`. The canonical
-- winner still orders by `files.file_key` (R2), never `file_id`
-- (first-seen order, not lexicographic). Dim FKs are nullable; where a
-- grain key was NOT NULL TEXT with '' for "none", the grain keeps a NOT
-- NULL dim id with the RESERVED id 0 standing for '' (R8), seeded here.
-- issue #958's five indexes are deliberately not recreated here, and
-- their schema.sql statements are gone: this file rebuilds the tables
-- they sat on, so leaving them out is what keeps them gone.

-- ---------------------------------------------------------------------------
-- 1. (moved) The guarded drop of a non-canonical fact/rollup table lives
--    in schema_widths.sql, which runs immediately before this file: a
--    drop must precede the CREATE it unblocks, and keeping both in one
--    file keeps the signature it compares beside the declaration it
--    compares against.
-- ---------------------------------------------------------------------------

-- 2. (moved) Rollup drops ride the same width guard.
-- file_key stays the UNIQUE stored identity (SV-FILES-RECORDS). Safe now:
-- both fact tables holding the old FK on files(file_key) are gone above.
-- The drop keys on the pkey's DEFINITION, not its name — after this block
-- the name is taken by the file_id pkey and must never be dropped again.
ALTER TABLE files ADD COLUMN IF NOT EXISTS file_id SERIAL;
-- (Width narrowing of files.file_id — and of projects/sessions/the dim
-- ids — lives in schema_types.sql: it runs after this file's ADDs and
-- CREATEs have made the columns at all.)
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'files'::regclass AND contype = 'p'
       AND pg_get_constraintdef(oid) = 'PRIMARY KEY (file_key)'
  ) THEN
    ALTER TABLE files DROP CONSTRAINT files_pkey;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'files'::regclass AND contype = 'p'
  ) THEN
    ALTER TABLE files ADD CONSTRAINT files_pkey PRIMARY KEY (file_id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'files'::regclass AND conname = 'files_file_key_uniq'
  ) THEN
    ALTER TABLE files ADD CONSTRAINT files_file_key_uniq UNIQUE (file_key);
  END IF;
END $$;

-- 4. projects: the proj_id surrogate, in place; project_id stays UNIQUE
-- (R3 keys operator-edited config by the human-readable id). The reserved
-- row (proj_id 0, project_id '') stands for the all-projects rollup
-- grain's '' (R8); no file can carry project_id '' (key_layout always
-- names one), so nothing else collides with it.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS proj_id SERIAL;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'projects'::regclass AND conname = 'projects_proj_id_uniq'
  ) THEN
    ALTER TABLE projects ADD CONSTRAINT projects_proj_id_uniq UNIQUE (proj_id);
  END IF;
END $$;
INSERT INTO projects (proj_id, project_id, display_name,
                      first_seen_at, last_seen_at)
VALUES (0, '', '', now(), now())
ON CONFLICT DO NOTHING;

-- 5. sessions: ses_id stands for (proj_id, session_id), the grain
-- usage_rollup keys on. Populated by ingest before any rollup row
-- references it (ingest_rollup_hourly.ensure_sessions). Both keys are
-- INTEGER (9k sessions today; proj_id matches projects.proj_id); a
-- database stamped at the earlier BIGINT pair narrows in
-- schema_types.sql.
CREATE TABLE IF NOT EXISTS sessions (
  ses_id      SERIAL    PRIMARY KEY,
  proj_id     INTEGER   NOT NULL REFERENCES projects(proj_id)
                         ON DELETE CASCADE,
  session_id  TEXT      NOT NULL,
  UNIQUE (proj_id, session_id)
);

-- 6. Dimension lookups, populated by ingest. id SMALLINT everywhere
-- (task 7): the id must store the vocabulary, and measured on live
-- pg_stats the TEXT values these replace average 4-13 bytes while a
-- BIGINT id costs 8 — the narrow type is smaller in every row of every
-- fact and rollup table that references it. Headroom: SMALLINT tops out
-- at 32,767 rows and the fastest grower (dim_pricing_version) adds one
-- per rate refresh, roughly weekly — centuries; dim_rate_fingerprint
-- is bounded by distinct (model, provider) pairs times rate eras (43
-- pairs today), so even thousands per decade stay an order of magnitude
-- clear. The '' vocabularies seed their reserved id 0 row (R8): a fold
-- that spelled "none" as '' resolves there, and an upsert of '' is that
-- same row by the UNIQUE constraint.
CREATE TABLE IF NOT EXISTS dim_model (
  id    SMALLSERIAL PRIMARY KEY,
  value TEXT        NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS dim_provider (
  id    SMALLSERIAL PRIMARY KEY,
  value TEXT        NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS dim_tool_name (
  id    SMALLSERIAL PRIMARY KEY,
  value TEXT        NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS dim_agent_type (
  id    SMALLSERIAL PRIMARY KEY,
  value TEXT        NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS dim_agent_model (
  id    SMALLSERIAL PRIMARY KEY,
  value TEXT        NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS dim_error_kind (
  id    SMALLSERIAL PRIMARY KEY,
  value TEXT        NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS dim_stop_reason (
  id    SMALLSERIAL PRIMARY KEY,
  value TEXT        NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS dim_effort (
  id    SMALLSERIAL PRIMARY KEY,
  value TEXT        NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS dim_rate_fingerprint (
  id    SMALLSERIAL PRIMARY KEY,
  value TEXT        NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS dim_pricing_version (
  id    SMALLSERIAL PRIMARY KEY,
  value TEXT        NOT NULL UNIQUE
);
-- The Claude Code build that sent the request (#951 analysis-only
-- column, now a dimension): 182 distinct values of ~7 bytes, NULL for a
-- lane or a transcript that carried no version. No reserved '' row —
-- NULL already stands for "absent", so the vocabulary needs no R8 id 0.
CREATE TABLE IF NOT EXISTS dim_cli_version (
  id    SMALLSERIAL PRIMARY KEY,
  value TEXT        NOT NULL UNIQUE
);
-- A dim table created by an earlier build at BIGSERIAL narrows in
-- schema_types.sql, with the other identity widths.
INSERT INTO dim_model (id, value) VALUES (0, '') ON CONFLICT DO NOTHING;
INSERT INTO dim_provider (id, value) VALUES (0, '') ON CONFLICT DO NOTHING;
INSERT INTO dim_agent_type (id, value) VALUES (0, '') ON CONFLICT DO NOTHING;
INSERT INTO dim_agent_model (id, value) VALUES (0, '') ON CONFLICT DO NOTHING;

-- 7. records: file_id FK + PK (file_id, line_num); dimension columns
-- become nullable dim FKs (model_id keeps the old NOT NULL). Per-row
-- token/text measures narrow to INT4 (R7) and turn_flags store the
-- R5 INT4 mask. records.uuid is the SPLIT set (task 8): uuid_col, the
-- seq counter and the verbatim fallback — classified at persist,
-- rebuilt byte-identically at every read (backend/uuid_split.py has
-- the full rationale and the equivalence proof). Same defaults. Column
-- order is WIDEST-FIRST (task 7): the 16-byte uuid first (typalign
-- 'c', no padding; its 16 bytes keep ts and the doubles 8-aligned),
-- then the 8-byte aligned columns, then the fixed 4-byte integers (the
-- PK parts, seq among them), then the 2-byte dimension ids, then the
-- variable-length 4-aligned values (text / numeric), then the 1-byte
-- booleans last — a boolean needs no alignment and nothing follows it,
-- so an early boolean could only push every later column off its own
-- alignment.
--
-- records_uuid_idx is NOT recreated here (#958-class, 140 MB measured
-- on the full corpus): its only readers were the two hourly equality
-- probes in ingest_rollup_state's incremental branches — 4 ms CPU with
-- the index vs ~86 ms without — while adopt_original_models' winners
-- CTE already seq-scans the whole table every incremental run anyway
-- and a window PARTITION BY never consults a b-tree index at all.
--
-- The reprice columns are created here as the PRE-SPLIT
-- shape (a database stamped by an earlier build has them, so both
-- states share one DDL) and moved to the records_price sidecar by
-- schema_records_price.sql, last in the chain, which stamps the
-- fillfactors (records 100, sidecar 50).
CREATE TABLE IF NOT EXISTS records (
  -- The split's widest column leads; the CHECKs below make one store
  -- per row and the stores' text disjointness a database-enforced
  -- invariant, which is what lets readers group by the parts.
  uuid_col                  UUID,
  ts                        TIMESTAMPTZ,
  long_context_input_mult   DOUBLE PRECISION,
  long_context_output_mult  DOUBLE PRECISION,
  file_id                   INTEGER    NOT NULL REFERENCES files(file_id)
                                        ON DELETE CASCADE,
  line_num                  INTEGER    NOT NULL,
  seq                       INTEGER,       -- the <uuid>:<n> counter slot
  fresh_tokens              INTEGER    NOT NULL DEFAULT 0,
  cache_creation_tokens     INTEGER    NOT NULL DEFAULT 0,
  cache_read_tokens         INTEGER    NOT NULL DEFAULT 0,
  cache_write_tokens        INTEGER    DEFAULT 0,
  output_tokens             INTEGER    NOT NULL DEFAULT 0,
  eph5_tokens               INTEGER    NOT NULL DEFAULT 0,
  eph1h_tokens              INTEGER    NOT NULL DEFAULT 0,
  text_chars                INTEGER    NOT NULL DEFAULT 0,
  web_search_requests       INTEGER,
  thinking_tokens           INTEGER    NOT NULL DEFAULT 0,
  turn_flags                INTEGER    DEFAULT 0,
  turn_tool_results         INTEGER    DEFAULT 0,
  model_id                  SMALLINT   NOT NULL REFERENCES dim_model(id),
  provider_id               SMALLINT   REFERENCES dim_provider(id),
  stop_reason_id            SMALLINT   REFERENCES dim_stop_reason(id),
  effort_id                 SMALLINT   REFERENCES dim_effort(id),
  pricing_version_id        SMALLINT   REFERENCES dim_pricing_version(id),
  rate_fingerprint_id       SMALLINT   REFERENCES dim_rate_fingerprint(id),
  cli_version_id            SMALLINT   REFERENCES dim_cli_version(id),
  fallback                  TEXT,          -- every non-uuid identity, verbatim
  request_id                TEXT       NOT NULL DEFAULT '',
  cost_usd                  NUMERIC(12,6) NOT NULL DEFAULT 0,
  reply_latency_s           NUMERIC(10,3),
  generation_s              NUMERIC(10,3),
  output_tokens_per_s       NUMERIC(20,3),
  request_fee_usd           NUMERIC(12,6),
  is_canonical              BOOLEAN    NOT NULL DEFAULT TRUE,
  is_replay                 BOOLEAN,
  long_context              BOOLEAN,
  PRIMARY KEY (file_id, line_num),
  -- One store per row: fallback is exclusive, seq needs its uuid_col.
  CHECK (fallback IS NULL OR (uuid_col IS NULL AND seq IS NULL)),
  CHECK (seq IS NULL OR uuid_col IS NOT NULL),
  -- Disjointness: no fallback may hold a form uuid_col or uuid_col+seq
  -- owns (pure vs split needs no rule — uuid::text has no colon).
  -- Leading zeros and an INT4-overflow counter ARE fallback forms and
  -- stay storable; CASE guards the numeric cast's evaluation order.
  CHECK (fallback IS NULL OR (
      fallback !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      AND CASE
        WHEN fallback ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:(0|[1-9][0-9]*)$'
        THEN substring(fallback FROM 38)::numeric > 2147483647
        ELSE TRUE END))
) WITH (fillfactor = 100);

-- records_uuid_idx stays gone (header above); no replacement.
CREATE INDEX IF NOT EXISTS records_ts_idx ON records (ts);
CREATE INDEX IF NOT EXISTS records_canonical_ts_idx
  ON records (ts) WHERE is_canonical;
-- Moved here from schema.sql: it keys on model_id now.
CREATE INDEX IF NOT EXISTS records_model_idx ON records (model_id);
-- Moved here from schema.sql: file_key became file_id.
CREATE INDEX IF NOT EXISTS records_latency_idx ON records (ts)
  INCLUDE (model_id, reply_latency_s, file_id, line_num)
  WHERE reply_latency_s IS NOT NULL;

-- 8. tool_uses: file_id FK + PK (file_id, line_num, idx); dimensions
-- become dim FKs (tool_name_id keeps the old NOT NULL; analysis columns
-- stay nullable so a switched-off writer leaves NULL). Widest-first
-- order as in records above. read_kind is BOOLEAN (task 7): the parser
-- emits exactly 'whole', 'slice' or NULL — two values plus absence —
-- and the column asks the boolean question directly, TRUE = the whole
-- file went into context. Empty read_targets/write_targets arrays are
-- stored as NULL by the writer (absent ≡ empty for every reader).
CREATE TABLE IF NOT EXISTS tool_uses (
  ts                    TIMESTAMPTZ,
  file_id               INTEGER    NOT NULL REFERENCES files(file_id)
                                    ON DELETE CASCADE,
  line_num              INTEGER    NOT NULL,
  idx                   INTEGER    NOT NULL,  -- index within content[]
  lines_added           INTEGER    NOT NULL DEFAULT 0,
  lines_deleted         INTEGER    NOT NULL DEFAULT 0,
  dispatch_prompt_chars INTEGER,
  result_chars          INTEGER,
  tool_name_id          SMALLINT   NOT NULL REFERENCES dim_tool_name(id),
  model_id              SMALLINT   REFERENCES dim_model(id),
  error_kind_id         SMALLINT   REFERENCES dim_error_kind(id),
  agent_type_id         SMALLINT   REFERENCES dim_agent_type(id),
  agent_model_id        SMALLINT   REFERENCES dim_agent_model(id),
  tool_use_id           TEXT,
  error_text            TEXT,
  dispatch_name         TEXT,
  read_targets          TEXT[],
  write_targets         TEXT[],
  read_kind             BOOLEAN,
  is_error              BOOLEAN,
  dispatch_brief_ref    BOOLEAN,
  is_reread             BOOLEAN,
  is_replay             BOOLEAN,
  is_canonical          BOOLEAN    NOT NULL DEFAULT TRUE,
  PRIMARY KEY (file_id, line_num, idx)
);

CREATE INDEX IF NOT EXISTS tool_uses_ts_idx ON tool_uses (ts);
CREATE INDEX IF NOT EXISTS tool_uses_tool_use_id_idx
  ON tool_uses (tool_use_id) WHERE tool_use_id IS NOT NULL;
-- Moved here from schema.sql: keyed on the dim FKs now.
CREATE INDEX IF NOT EXISTS tool_uses_error_kind_idx
  ON tool_uses (error_kind_id, ts) WHERE error_kind_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS tool_uses_agent_type_idx
  ON tool_uses (agent_type_id, ts) WHERE agent_type_id IS NOT NULL;

-- 9. Rollups: same semantic grains (SV-ROLLUP), TEXT dimensions stored
-- as dim FKs; each keeps the old NOT NULL where the text column had it,
-- the reserved id 0 standing for ''. Grain KEYS narrow with their
-- dimension (INTEGER identity, SMALLINT dim ids — task 7); every
-- TOTAL column stays BIGINT (R7): sums over the corpus exceed int4 by
-- construction, ids are keys and totals are aggregates.
CREATE TABLE IF NOT EXISTS usage_rollup (
  ses_id            INTEGER     NOT NULL REFERENCES sessions(ses_id)
                                  ON DELETE CASCADE,
  proj_id           INTEGER     NOT NULL REFERENCES projects(proj_id)
                                  ON DELETE CASCADE,
  hour              TIMESTAMPTZ NOT NULL,
  model_id          SMALLINT    NOT NULL REFERENCES dim_model(id),
  provider_id       SMALLINT    NOT NULL REFERENCES dim_provider(id),
  is_main           BOOLEAN     NOT NULL DEFAULT TRUE,
  first_ts          TIMESTAMPTZ NOT NULL,
  last_ts           TIMESTAMPTZ NOT NULL,
  requests          BIGINT      NOT NULL DEFAULT 0,
  fresh_tokens      BIGINT      NOT NULL DEFAULT 0,
  output_tokens     BIGINT      NOT NULL DEFAULT 0,
  cache_creation_tokens BIGINT  NOT NULL DEFAULT 0,
  cache_read_tokens BIGINT      NOT NULL DEFAULT 0,
  eph5_tokens       BIGINT      NOT NULL DEFAULT 0,
  eph1h_tokens      BIGINT      NOT NULL DEFAULT 0,
  thinking_tokens   BIGINT      NOT NULL DEFAULT 0,
  cost_usd          NUMERIC(18,8) NOT NULL DEFAULT 0,
  long_context      BOOLEAN     NOT NULL DEFAULT FALSE,
  PRIMARY KEY (ses_id, hour, model_id, provider_id, is_main, long_context)
);
CREATE INDEX IF NOT EXISTS usage_rollup_hour_idx ON usage_rollup (hour);
-- Moved here from schema.sql: project_id became proj_id.
CREATE INDEX IF NOT EXISTS usage_rollup_project_idx
  ON usage_rollup (proj_id, hour);

CREATE TABLE IF NOT EXISTS tool_rollup (
  hour           TIMESTAMPTZ NOT NULL,
  proj_id        INTEGER     NOT NULL REFERENCES projects(proj_id)
                              ON DELETE CASCADE,
  model_id       SMALLINT    NOT NULL REFERENCES dim_model(id),
  tool_name_id   SMALLINT    NOT NULL REFERENCES dim_tool_name(id),
  n_total        BIGINT      NOT NULL DEFAULT 0,
  n_rated        BIGINT      NOT NULL DEFAULT 0,
  n_error        BIGINT      NOT NULL DEFAULT 0,
  lines_added    BIGINT      NOT NULL DEFAULT 0,
  lines_deleted  BIGINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, proj_id, model_id, tool_name_id)
);
CREATE INDEX IF NOT EXISTS tool_rollup_hour_idx ON tool_rollup (hour);
CREATE INDEX IF NOT EXISTS tool_rollup_project_idx
  ON tool_rollup (proj_id, hour);

CREATE TABLE IF NOT EXISTS tool_error_rollup (
  hour           TIMESTAMPTZ NOT NULL,
  proj_id        INTEGER     NOT NULL REFERENCES projects(proj_id)
                              ON DELETE CASCADE,
  model_id       SMALLINT    NOT NULL REFERENCES dim_model(id),
  tool_name_id   SMALLINT    NOT NULL REFERENCES dim_tool_name(id),
  error_kind_id  SMALLINT    NOT NULL REFERENCES dim_error_kind(id),
  n              BIGINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, proj_id, model_id, tool_name_id, error_kind_id)
);
CREATE INDEX IF NOT EXISTS tool_error_rollup_hour_idx
  ON tool_error_rollup (hour);

CREATE TABLE IF NOT EXISTS dispatch_rollup (
  hour            TIMESTAMPTZ NOT NULL,
  proj_id         INTEGER     NOT NULL REFERENCES projects(proj_id)
                               ON DELETE CASCADE,
  agent_type_id   SMALLINT    NOT NULL REFERENCES dim_agent_type(id),
  agent_model_id  SMALLINT    NOT NULL REFERENCES dim_agent_model(id),
  n               BIGINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, proj_id, agent_type_id, agent_model_id)
);
CREATE INDEX IF NOT EXISTS dispatch_rollup_hour_idx
  ON dispatch_rollup (hour);

CREATE TABLE IF NOT EXISTS dispatch_brief_rollup (
  hour            TIMESTAMPTZ NOT NULL,
  proj_id         INTEGER     NOT NULL REFERENCES projects(proj_id)
                               ON DELETE CASCADE,
  agent_type_id   SMALLINT    NOT NULL REFERENCES dim_agent_type(id),
  brief_ref       BOOLEAN     NOT NULL,
  n               BIGINT      NOT NULL DEFAULT 0,
  prompt_chars    BIGINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, proj_id, agent_type_id, brief_ref)
);
CREATE INDEX IF NOT EXISTS dispatch_brief_rollup_hour_idx
  ON dispatch_brief_rollup (hour);

CREATE TABLE IF NOT EXISTS ctx_cost_rollup (
  hour          TIMESTAMPTZ NOT NULL,
  proj_id       INTEGER     NOT NULL REFERENCES projects(proj_id)
                             ON DELETE CASCADE,
  model_id      SMALLINT    NOT NULL REFERENCES dim_model(id),
  ctx_bucket    BIGINT      NOT NULL,
  requests      BIGINT      NOT NULL DEFAULT 0,
  total_tokens  BIGINT      NOT NULL DEFAULT 0,
  cost_usd      NUMERIC(18,8) NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, proj_id, model_id, ctx_bucket)
);
CREATE INDEX IF NOT EXISTS ctx_cost_rollup_hour_idx
  ON ctx_cost_rollup (hour);
CREATE INDEX IF NOT EXISTS ctx_cost_rollup_project_idx
  ON ctx_cost_rollup (proj_id, hour);

CREATE TABLE IF NOT EXISTS agent_rollup (
  hour            TIMESTAMPTZ NOT NULL,
  proj_id         INTEGER     NOT NULL REFERENCES projects(proj_id)
                               ON DELETE CASCADE,
  model_id        SMALLINT    NOT NULL REFERENCES dim_model(id),
  agent_type_id   SMALLINT    NOT NULL REFERENCES dim_agent_type(id),
  requests        BIGINT      NOT NULL DEFAULT 0,
  output_tokens   BIGINT      NOT NULL DEFAULT 0,
  total_tokens    BIGINT      NOT NULL DEFAULT 0,
  cost_usd        NUMERIC(18,8) NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, proj_id, model_id, agent_type_id)
);
CREATE INDEX IF NOT EXISTS agent_rollup_hour_idx ON agent_rollup (hour);
CREATE INDEX IF NOT EXISTS agent_rollup_project_idx
  ON agent_rollup (proj_id, hour);

-- proj_id 0 is the all-projects row (project_id '' reserved, step 4).
CREATE TABLE IF NOT EXISTS latency_rollup (
  bucket_s     INTEGER      NOT NULL,
  bucket       TIMESTAMPTZ  NOT NULL,
  proj_id      INTEGER      NOT NULL REFERENCES projects(proj_id)
                             ON DELETE CASCADE,
  model_id     SMALLINT     NOT NULL REFERENCES dim_model(id),
  n            BIGINT       NOT NULL,
  p10          DOUBLE PRECISION,
  p50          DOUBLE PRECISION,
  p90          DOUBLE PRECISION,
  outliers     JSONB        NOT NULL DEFAULT '[]'::jsonb,
  PRIMARY KEY (bucket_s, bucket, proj_id, model_id)
);
CREATE INDEX IF NOT EXISTS latency_rollup_lookup_idx
  ON latency_rollup (bucket_s, proj_id, bucket);

-- 10. Converge strays: this chain issues no ADD of the retired TEXT
--      dimension columns (schema.sql and its satellites say so at their
--      records site), but a database that still carries one — a
--      hand-added column, a restore of an older dump — loses it here, in
--      the same transaction, so the shape converges either way. Never
--      written by this binary; a DROP of an absent column costs nothing.
ALTER TABLE records DROP COLUMN IF EXISTS stop_reason;
ALTER TABLE records DROP COLUMN IF EXISTS effort;
ALTER TABLE records DROP COLUMN IF EXISTS provider;
ALTER TABLE records DROP COLUMN IF EXISTS pricing_version;
ALTER TABLE records DROP COLUMN IF EXISTS rate_fingerprint;
ALTER TABLE tool_uses DROP COLUMN IF EXISTS tool_name;
ALTER TABLE tool_uses DROP COLUMN IF EXISTS error_kind;
ALTER TABLE tool_uses DROP COLUMN IF EXISTS agent_type;
ALTER TABLE tool_uses DROP COLUMN IF EXISTS agent_model;
ALTER TABLE tool_uses DROP COLUMN IF EXISTS model;
ALTER TABLE usage_rollup DROP COLUMN IF EXISTS provider;
ALTER TABLE usage_rollup DROP COLUMN IF EXISTS session_id;
ALTER TABLE usage_rollup DROP COLUMN IF EXISTS project_id;
ALTER TABLE tool_rollup DROP COLUMN IF EXISTS project_id;
ALTER TABLE tool_rollup DROP COLUMN IF EXISTS model;
ALTER TABLE tool_rollup DROP COLUMN IF EXISTS tool_name;
ALTER TABLE tool_error_rollup DROP COLUMN IF EXISTS project_id;
ALTER TABLE tool_error_rollup DROP COLUMN IF EXISTS model;
ALTER TABLE tool_error_rollup DROP COLUMN IF EXISTS tool_name;
ALTER TABLE tool_error_rollup DROP COLUMN IF EXISTS error_kind;
ALTER TABLE dispatch_rollup DROP COLUMN IF EXISTS project_id;
ALTER TABLE dispatch_rollup DROP COLUMN IF EXISTS agent_type;
ALTER TABLE dispatch_rollup DROP COLUMN IF EXISTS agent_model;
ALTER TABLE dispatch_brief_rollup DROP COLUMN IF EXISTS project_id;
ALTER TABLE dispatch_brief_rollup DROP COLUMN IF EXISTS agent_type;
ALTER TABLE ctx_cost_rollup DROP COLUMN IF EXISTS project_id;
ALTER TABLE ctx_cost_rollup DROP COLUMN IF EXISTS model;
ALTER TABLE agent_rollup DROP COLUMN IF EXISTS project_id;
ALTER TABLE agent_rollup DROP COLUMN IF EXISTS model;
ALTER TABLE agent_rollup DROP COLUMN IF EXISTS agent_type;
ALTER TABLE latency_rollup DROP COLUMN IF EXISTS project_id;
ALTER TABLE latency_rollup DROP COLUMN IF EXISTS model;
