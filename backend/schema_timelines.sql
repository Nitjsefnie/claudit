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
    UPDATE files f SET (ctx_ts, ctx_line, ctx_input, ctx_output) = (
      SELECT COALESCE(array_agg(NULLIF(t->>'ts', '')::timestamptz ORDER BY n), '{}'),
             COALESCE(array_agg((t->>'line')::integer ORDER BY n), '{}'),
             COALESCE(array_agg((t->>'input')::integer ORDER BY n), '{}'),
             COALESCE(array_agg((t->>'output')::integer ORDER BY n), '{}')
        FROM jsonb_array_elements(f.ctx_turns) WITH ORDINALITY AS turns(t, n)
    );
    ALTER TABLE files DROP COLUMN ctx_turns;
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
