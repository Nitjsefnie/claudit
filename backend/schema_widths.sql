-- Shape convergence for the derived fact and rollup tables (issue #959,
-- task 7: storage widths).
--
-- Applied in the middle of schema's one stamped transaction
-- (backend/schema_files.py), after every file that adds columns to the
-- OLD text shape and BEFORE schema_dims.sql, whose CREATE TABLE
-- statements rebuild whatever this file drops: a guarded drop must run
-- before the create it unblocks, and schema_dims owns the canonical
-- declarations (SMALLINT dimension ids, INTEGER surrogate keys,
-- widest-first column order in records and tool_uses).
--
-- Three deployment states, one path each (SV-SCHEMA-AUTOAPPLY, one
-- transaction, re-run a no-op):
--   * fresh database: schema.sql just created records/tool_uses/rollups
--     in the pre-#959 text shape — signature differs, dropped here,
--     rebuilt by schema_dims.sql below;
--   * pre-#959 deploy: same shape with data in it — dropped (R1: the
--     derived rebuild the PARSER_VERSION bump's full reparse refills);
--   * database stamped by this branch at the EARLIER widths (BIGINT
--     ids, TEXT cli_version, narrow-last order): names and/or types
--     differ, dropped — this is the guard that keeps a stamped dev
--     database from keeping the old column order forever.
--
-- The guard is the table's live column signature, in attnum order, as
-- `attname:format_type(...)` (string_agg over pg_attribute): order AND
-- types in one value, so an ordering-only or type-only divergence both
-- fire. A table whose signature matches its canonical form is not
-- touched at all — no DROP means no attnum churn and no row loss on a
-- re-apply (tests/test_schema_attnums.py pins that invariant).
--
-- `records` accepts TWO canonical signatures: the resting split form
-- (its reprice columns live on records_price) and the pre-split form
-- schema_dims.sql creates mid-chain, because schema_records_price.sql
-- moves those columns AFTER this file and a database stopped between
-- the two (the carry-over state) must keep its rows so the sidecar copy
-- still finds them. Both spell the uuid SPLIT columns (uuid_col/seq/
-- fallback, task 8); a `uuid:text` records — pre-#959 or stamped by
-- this branch before the split — matches neither and is rebuilt, which
-- is why task 8 carries PARSER_VERSION 110: dropping the rows alone
-- would leave files.parser_version matching and nothing would refill
-- them.
--
-- records_price is dropped ONLY when records is: its FK cascades the
-- pair anyway, and dropping it alone would erase stored costs that
-- nothing rebuilds until the next reparse. Rollups are independent —
-- each is rebuilt from records by the ingest's rollup pass.
--
-- Identity tables (files, projects, sessions) and the dimension lookups
-- migrate IN PLACE (width narrowing only) in schema_types.sql, two
-- files later — after every ADD/CREATE has made the columns at all —
-- so this file never retypes a column a surviving table references.
--
-- ---------------------------------------------------------------------------
-- The guard, one pass: compute each table's live signature, drop the
-- table when the signature is present but not canonical. An absent
-- table yields a NULL signature and is left alone (its CREATE below
-- makes it); a canonical table is left alone (no drop means no row loss
-- and no attnum churn — the invariant tests/test_schema_attnums.py and
-- tests/test_storage_widths.py pin across re-applies).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  got    text;
  -- records has TWO canonical signatures: the resting split form, and
  -- the pre-split form schema_dims.sql creates mid-chain (this chain's
  -- own records_price split runs AFTER this file). A table matching
  -- neither — old text shape, earlier-widths stamped shape, or an
  -- out-of-band column change — is rebuilt with its sidecar.
  rec_bad boolean;
BEGIN
  ---------------------------------------------------------------- facts
  IF to_regclass('public.records') IS NOT NULL THEN
    SELECT string_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod),
                      ',' ORDER BY a.attnum)
      INTO got
      FROM pg_attribute a
     WHERE a.attrelid = 'records'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped;
  ELSE
    got := NULL;
  END IF;
  rec_bad := got IS NOT NULL
    AND got IS DISTINCT FROM 'uuid_col:uuid,ts:timestamp with time zone,file_id:integer,line_num:integer,seq:integer,fresh_tokens:integer,cache_creation_tokens:integer,cache_read_tokens:integer,cache_write_tokens:integer,output_tokens:integer,eph5_tokens:integer,eph1h_tokens:integer,text_chars:integer,web_search_requests:integer,thinking_tokens:integer,turn_flags:integer,turn_tool_results:integer,model_id:smallint,provider_id:smallint,stop_reason_id:smallint,effort_id:smallint,cli_version_id:smallint,fallback:text,request_id:text,reply_latency_s:numeric(10,3),generation_s:numeric(10,3),output_tokens_per_s:numeric(20,3),is_canonical:boolean,is_replay:boolean'
    AND got IS DISTINCT FROM 'uuid_col:uuid,ts:timestamp with time zone,long_context_input_mult:double precision,long_context_output_mult:double precision,file_id:integer,line_num:integer,seq:integer,fresh_tokens:integer,cache_creation_tokens:integer,cache_read_tokens:integer,cache_write_tokens:integer,output_tokens:integer,eph5_tokens:integer,eph1h_tokens:integer,text_chars:integer,web_search_requests:integer,thinking_tokens:integer,turn_flags:integer,turn_tool_results:integer,model_id:smallint,provider_id:smallint,stop_reason_id:smallint,effort_id:smallint,pricing_version_id:smallint,rate_fingerprint_id:smallint,cli_version_id:smallint,fallback:text,request_id:text,cost_usd:numeric(12,6),reply_latency_s:numeric(10,3),generation_s:numeric(10,3),output_tokens_per_s:numeric(20,3),request_fee_usd:numeric(12,6),is_canonical:boolean,is_replay:boolean,long_context:boolean';
  -- The sidecar goes first: its FK to records(file_id, line_num) would
  -- refuse the drop, and a price row without its record is meaningless
  -- (costs come back with the record, via the reparse this migration's
  -- PARSER_VERSION bump forces).
  IF rec_bad THEN
    DROP TABLE IF EXISTS records_price;
    DROP TABLE records;
  END IF;

  IF to_regclass('public.tool_uses') IS NOT NULL THEN
    SELECT string_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod),
                      ',' ORDER BY a.attnum)
      INTO got
      FROM pg_attribute a
     WHERE a.attrelid = 'tool_uses'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped;
  ELSE
    got := NULL;
  END IF;
  IF got IS NOT NULL AND got IS DISTINCT FROM
     'ts:timestamp with time zone,file_id:integer,line_num:integer,idx:integer,lines_added:integer,lines_deleted:integer,dispatch_prompt_chars:integer,result_chars:integer,tool_name_id:smallint,model_id:smallint,error_kind_id:smallint,agent_type_id:smallint,agent_model_id:smallint,tool_use_id:text,error_text:text,dispatch_name:text,read_targets:text[],write_targets:text[],read_kind:boolean,is_error:boolean,dispatch_brief_ref:boolean,is_reread:boolean,is_replay:boolean,is_canonical:boolean'
  THEN
    DROP TABLE tool_uses;
  END IF;

  --------------------------------------------------------------- rollups
  IF to_regclass('public.usage_rollup') IS NOT NULL THEN
    SELECT string_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod),
                      ',' ORDER BY a.attnum)
      INTO got
      FROM pg_attribute a
     WHERE a.attrelid = 'usage_rollup'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped;
  ELSE
    got := NULL;
  END IF;
  IF got IS NOT NULL AND got IS DISTINCT FROM
     'ses_id:integer,proj_id:integer,hour:timestamp with time zone,model_id:smallint,provider_id:smallint,is_main:boolean,first_ts:timestamp with time zone,last_ts:timestamp with time zone,requests:bigint,fresh_tokens:bigint,output_tokens:bigint,cache_creation_tokens:bigint,cache_read_tokens:bigint,eph5_tokens:bigint,eph1h_tokens:bigint,thinking_tokens:bigint,cost_usd:numeric(18,8),long_context:boolean'
  THEN
    DROP TABLE usage_rollup;
  END IF;

  IF to_regclass('public.tool_rollup') IS NOT NULL THEN
    SELECT string_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod),
                      ',' ORDER BY a.attnum)
      INTO got
      FROM pg_attribute a
     WHERE a.attrelid = 'tool_rollup'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped;
  ELSE
    got := NULL;
  END IF;
  IF got IS NOT NULL AND got IS DISTINCT FROM
     'hour:timestamp with time zone,proj_id:integer,model_id:smallint,tool_name_id:smallint,n_total:bigint,n_rated:bigint,n_error:bigint,lines_added:bigint,lines_deleted:bigint'
  THEN
    DROP TABLE tool_rollup;
  END IF;

  IF to_regclass('public.tool_error_rollup') IS NOT NULL THEN
    SELECT string_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod),
                      ',' ORDER BY a.attnum)
      INTO got
      FROM pg_attribute a
     WHERE a.attrelid = 'tool_error_rollup'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped;
  ELSE
    got := NULL;
  END IF;
  IF got IS NOT NULL AND got IS DISTINCT FROM
     'hour:timestamp with time zone,proj_id:integer,model_id:smallint,tool_name_id:smallint,error_kind_id:smallint,n:bigint'
  THEN
    DROP TABLE tool_error_rollup;
  END IF;

  IF to_regclass('public.dispatch_rollup') IS NOT NULL THEN
    SELECT string_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod),
                      ',' ORDER BY a.attnum)
      INTO got
      FROM pg_attribute a
     WHERE a.attrelid = 'dispatch_rollup'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped;
  ELSE
    got := NULL;
  END IF;
  IF got IS NOT NULL AND got IS DISTINCT FROM
     'hour:timestamp with time zone,proj_id:integer,agent_type_id:smallint,agent_model_id:smallint,n:bigint'
  THEN
    DROP TABLE dispatch_rollup;
  END IF;

  IF to_regclass('public.dispatch_brief_rollup') IS NOT NULL THEN
    SELECT string_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod),
                      ',' ORDER BY a.attnum)
      INTO got
      FROM pg_attribute a
     WHERE a.attrelid = 'dispatch_brief_rollup'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped;
  ELSE
    got := NULL;
  END IF;
  IF got IS NOT NULL AND got IS DISTINCT FROM
     'hour:timestamp with time zone,proj_id:integer,agent_type_id:smallint,brief_ref:boolean,n:bigint,prompt_chars:bigint'
  THEN
    DROP TABLE dispatch_brief_rollup;
  END IF;

  IF to_regclass('public.ctx_cost_rollup') IS NOT NULL THEN
    SELECT string_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod),
                      ',' ORDER BY a.attnum)
      INTO got
      FROM pg_attribute a
     WHERE a.attrelid = 'ctx_cost_rollup'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped;
  ELSE
    got := NULL;
  END IF;
  IF got IS NOT NULL AND got IS DISTINCT FROM
     'hour:timestamp with time zone,proj_id:integer,model_id:smallint,ctx_bucket:bigint,requests:bigint,total_tokens:bigint,cost_usd:numeric(18,8)'
  THEN
    DROP TABLE ctx_cost_rollup;
  END IF;

  IF to_regclass('public.agent_rollup') IS NOT NULL THEN
    SELECT string_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod),
                      ',' ORDER BY a.attnum)
      INTO got
      FROM pg_attribute a
     WHERE a.attrelid = 'agent_rollup'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped;
  ELSE
    got := NULL;
  END IF;
  IF got IS NOT NULL AND got IS DISTINCT FROM
     'hour:timestamp with time zone,proj_id:integer,model_id:smallint,agent_type_id:smallint,requests:bigint,output_tokens:bigint,total_tokens:bigint,cost_usd:numeric(18,8)'
  THEN
    DROP TABLE agent_rollup;
  END IF;

  IF to_regclass('public.latency_rollup') IS NOT NULL THEN
    SELECT string_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod),
                      ',' ORDER BY a.attnum)
      INTO got
      FROM pg_attribute a
     WHERE a.attrelid = 'latency_rollup'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped;
  ELSE
    got := NULL;
  END IF;
  IF got IS NOT NULL AND got IS DISTINCT FROM
     'bucket_s:integer,bucket:timestamp with time zone,proj_id:integer,model_id:smallint,n:bigint,p10:double precision,p50:double precision,p90:double precision,outliers:jsonb'
  THEN
    DROP TABLE latency_rollup;
  END IF;
END $$;