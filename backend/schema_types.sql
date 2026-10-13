-- Per-row type compaction (issue #959): the R5 turn_flags mask and the
-- R7 INT4 measures, converged on a database that already carries the
-- surrogate-id shape.
--
-- Applied near the end of schema's one stamped transaction
-- (backend/schema_files.py), after schema_dims.sql and before
-- schema_records_price.sql, the file that follows it:
--   * a pre-#959 database: schema_dims.sql's guarded drop-and-recreate
--     has just rebuilt records/tool_uses in this shape, so every guard
--     below finds the narrow type already and is a no-op;
--   * a database migrated by the surrogate-id step alone: its drop guard
--     keyed on records.file_key and did not fire, so the older wide types
--     converge here, in place, each guarded by a catalog check so a
--     re-run is a no-op (SV-SCHEMA-AUTOAPPLY, one transaction).
-- Values: a measure outside int4's range aborts the migration loudly
-- rather than truncating (the corpus holds none — the bounds report of
-- scripts/uuid_gate.py is the evidence). turn_flags name lists convert
-- to 0 and are rebuilt by each file's next reparse; the column has no
-- reader, and the deploy path this ships on rebuilds every file under
-- the PARSER_VERSION bump.
--
-- uuid does NOT convert here: ruling R6's gate found values the uuid
-- type cannot hold (lane uuids of the shape <session-or-key>:<counter>),
-- so records.uuid stays TEXT — never coerced, never NULLed (a NULL uuid
-- means always-canonical in the winner rule).

-- turn_flags TEXT[] -> INT4 mask (R5). The old default must go first:
-- '{}'::text[] has no automatic cast to integer. information_schema
-- spells an array's data_type 'ARRAY' (uppercase).
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'records'
       AND column_name = 'turn_flags' AND data_type = 'ARRAY'
  ) THEN
    ALTER TABLE records
      ALTER COLUMN turn_flags DROP DEFAULT,
      ALTER COLUMN turn_flags TYPE INTEGER USING 0,
      ALTER COLUMN turn_flags SET DEFAULT 0;
  END IF;
END $$;

-- Per-row measures BIGINT -> INT4 (R7). Rollup TOTAL columns are never
-- touched: they stay BIGINT. The assignment cast fails the transaction
-- on an out-of-range value instead of wrapping.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'records'
       AND column_name = 'fresh_tokens' AND data_type = 'bigint'
  ) THEN
    ALTER TABLE records
      ALTER COLUMN fresh_tokens TYPE INTEGER,
      ALTER COLUMN cache_creation_tokens TYPE INTEGER,
      ALTER COLUMN cache_read_tokens TYPE INTEGER,
      ALTER COLUMN cache_write_tokens TYPE INTEGER,
      ALTER COLUMN output_tokens TYPE INTEGER,
      ALTER COLUMN eph5_tokens TYPE INTEGER,
      ALTER COLUMN eph1h_tokens TYPE INTEGER,
      ALTER COLUMN thinking_tokens TYPE INTEGER,
      ALTER COLUMN text_chars TYPE INTEGER;
  END IF;
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'tool_uses'
       AND column_name = 'lines_added' AND data_type = 'bigint'
  ) THEN
    ALTER TABLE tool_uses
      ALTER COLUMN lines_added TYPE INTEGER,
      ALTER COLUMN lines_deleted TYPE INTEGER,
      ALTER COLUMN result_chars TYPE INTEGER;
  END IF;
END $$;

-- Identity and dimension storage widths (issue #959 task 7), the same
-- convergence this file already does for the measure columns: it runs
-- after schema_dims.sql has ADDed/CREATEd every key column, so a fresh
-- database finds the narrow type already and takes the no-op, and a
-- database stamped at the branch's earlier BIGSTRID widths converts
-- here. Each guard keys on the CURRENT type, because ALTER COLUMN TYPE
-- rewrites the table — running it on an already-narrow column would
-- burn a full rewrite on every apply for nothing.
--
--   files.file_id / projects.proj_id / sessions(ses_id, proj_id)
--     BIGINT -> INTEGER (36k / 3k / 9k rows today; 2.1B ceiling);
--   every dim lookup's id BIGINT -> SMALLINT (SMALLSERIAL on a fresh
--     create; only a table an earlier build made at BIGSERIAL lands
--     here), which is what makes the fact and rollup FK columns —
--     already declared SMALLINT by schema_dims.sql — finally point at
--     a same-typed parent.
--
-- A value outside the narrow range aborts the transaction loudly (the
-- assignment cast raises) rather than wrapping — with the migration
-- running under one stamp, an aborted conversion discards the whole
-- apply instead of half of it. Integer FK columns may reference a
-- parent of the other integer width mid-block (PostgreSQL keeps the
-- constraint — the two types share an equality operator); parent and
-- child both narrow before the chain ends, so the rest state matches.

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_attribute
     WHERE attrelid = 'files'::regclass AND attname = 'file_id'
       AND atttypid = 'int8'::regtype
  ) THEN
    ALTER TABLE files ALTER COLUMN file_id TYPE INTEGER;
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_attribute
     WHERE attrelid = 'projects'::regclass AND attname = 'proj_id'
       AND atttypid = 'int8'::regtype
  ) THEN
    ALTER TABLE projects ALTER COLUMN proj_id TYPE INTEGER;
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_attribute
     WHERE attrelid = 'sessions'::regclass AND attname = 'ses_id'
       AND atttypid = 'int8'::regtype
  ) THEN
    ALTER TABLE sessions
      ALTER COLUMN ses_id TYPE INTEGER,
      ALTER COLUMN proj_id TYPE INTEGER;
  END IF;
END $$;

DO $$
DECLARE dim text;
BEGIN
  FOREACH dim IN ARRAY ARRAY['dim_model', 'dim_provider', 'dim_tool_name',
                             'dim_agent_type', 'dim_agent_model',
                             'dim_error_kind', 'dim_stop_reason',
                             'dim_effort', 'dim_rate_fingerprint',
                             'dim_pricing_version'] LOOP
    IF EXISTS (
      SELECT 1 FROM pg_attribute
       WHERE attrelid = format('%I', dim)::regclass AND attname = 'id'
         AND atttypid = 'int8'::regtype
    ) THEN
      EXECUTE format('ALTER TABLE %I ALTER COLUMN id TYPE SMALLINT', dim);
    END IF;
  END LOOP;
END $$;
