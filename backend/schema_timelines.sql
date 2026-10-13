-- File timelines converge in place in the startup schema transaction.
-- Reapplication neither rewrites typed columns nor allocates column slots.
ALTER TABLE files ADD COLUMN IF NOT EXISTS ctx_ts TIMESTAMPTZ[] NOT NULL DEFAULT '{}';
ALTER TABLE files ADD COLUMN IF NOT EXISTS ctx_line INTEGER[] NOT NULL DEFAULT '{}';
ALTER TABLE files ADD COLUMN IF NOT EXISTS ctx_input INTEGER[] NOT NULL DEFAULT '{}';
ALTER TABLE files ADD COLUMN IF NOT EXISTS ctx_output INTEGER[] NOT NULL DEFAULT '{}';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_attribute
     WHERE attrelid = 'files'::regclass AND attname = 'ctx_turns'
       AND NOT attisdropped
  ) THEN
    CREATE FUNCTION pg_temp.ctx_timeline_ts(v jsonb) RETURNS timestamptz[]
    LANGUAGE sql AS $body$
      SELECT COALESCE(array_agg(NULLIF(t->>'ts', '')::timestamptz ORDER BY n), '{}')
        FROM jsonb_array_elements(v) WITH ORDINALITY AS turns(t, n)
    $body$;
    CREATE FUNCTION pg_temp.ctx_timeline_ints(v jsonb, field text) RETURNS integer[]
    LANGUAGE sql AS $body$
      SELECT COALESCE(array_agg((t->>field)::integer ORDER BY n), '{}')
        FROM jsonb_array_elements(v) WITH ORDINALITY AS turns(t, n)
    $body$;
    -- TYPE USING rewrites live rows in heap scan order without UPDATE
    -- moving their row versions to different heap positions.
    ALTER TABLE files
      ALTER COLUMN ctx_ts TYPE TIMESTAMPTZ[] USING pg_temp.ctx_timeline_ts(ctx_turns),
      ALTER COLUMN ctx_line TYPE INTEGER[] USING pg_temp.ctx_timeline_ints(ctx_turns, 'line'),
      ALTER COLUMN ctx_input TYPE INTEGER[] USING pg_temp.ctx_timeline_ints(ctx_turns, 'input'),
      ALTER COLUMN ctx_output TYPE INTEGER[] USING pg_temp.ctx_timeline_ints(ctx_turns, 'output');
    ALTER TABLE files DROP COLUMN ctx_turns;
    DROP FUNCTION pg_temp.ctx_timeline_ts(jsonb);
    DROP FUNCTION pg_temp.ctx_timeline_ints(jsonb, text);
    -- The prompt conversion below rewrites AFTER the drop, reclaiming the
    -- removed JSON bytes. A database with already-typed prompts needs its
    -- own post-drop rewrite; the nontrivial USING expression forces one.
    IF NOT EXISTS (
      SELECT 1 FROM pg_attribute
       WHERE attrelid = 'files'::regclass AND attname = 'prompt_ts'
         AND atttypid = 'jsonb'::regtype AND NOT attisdropped
    ) THEN
      ALTER TABLE files ALTER COLUMN ctx_ts TYPE TIMESTAMPTZ[]
        USING ctx_ts || '{}'::timestamptz[];
    END IF;
  END IF;

  IF EXISTS (
    SELECT 1 FROM pg_attribute
     WHERE attrelid = 'files'::regclass AND attname = 'prompt_ts'
       AND atttypid = 'jsonb'::regtype AND NOT attisdropped
  ) THEN
    -- A temporary function permits an ordered aggregate in TYPE USING,
    -- keeping prompt_ts's attribute number and leaving no persistent helper.
    CREATE FUNCTION pg_temp.prompt_timeline(v jsonb) RETURNS timestamptz[]
    LANGUAGE sql AS $body$
      SELECT COALESCE(array_agg(
               CASE WHEN pg_input_is_valid(p, 'timestamptz')
                    THEN p::timestamptz END ORDER BY n), '{}'::timestamptz[])
        FROM jsonb_array_elements_text(v) WITH ORDINALITY AS prompts(p, n)
    $body$;
    ALTER TABLE files
      ALTER COLUMN prompt_ts DROP DEFAULT,
      ALTER COLUMN prompt_ts TYPE TIMESTAMPTZ[] USING pg_temp.prompt_timeline(prompt_ts),
      ALTER COLUMN prompt_ts SET DEFAULT '{}';
    DROP FUNCTION pg_temp.prompt_timeline(jsonb);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'files'::regclass AND conname = 'files_ctx_cardinality'
  ) THEN
    ALTER TABLE files ADD CONSTRAINT files_ctx_cardinality CHECK (
      cardinality(ctx_ts) = cardinality(ctx_line)
      AND cardinality(ctx_ts) = cardinality(ctx_input)
      AND cardinality(ctx_ts) = cardinality(ctx_output)
    );
  END IF;
END $$;
