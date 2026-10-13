-- Optional collection storage permits NULL for fields not collected. Keep
-- types/defaults so older named-column writers still satisfy the schema.
ALTER TABLE files ADD COLUMN IF NOT EXISTS collection_flags TEXT[];
ALTER TABLE files ALTER COLUMN models DROP NOT NULL;
ALTER TABLE files ALTER COLUMN prompt_count DROP NOT NULL;
ALTER TABLE files ALTER COLUMN turn_count DROP NOT NULL;
ALTER TABLE records ALTER COLUMN turn_flags DROP NOT NULL;
ALTER TABLE records ALTER COLUMN turn_tool_results DROP NOT NULL;
ALTER TABLE records ALTER COLUMN cache_write_tokens DROP NOT NULL;

-- No app query needs records_request_idx or tool_uses_tool_idx (#958):
-- api_cache returns request_id but sorts by token counts; api_tool_panels
-- and ingest_rollup_hourly filter time/model/canonical rows and group tools.
-- Actual incidental optimizer index selection has not been measured.
DROP INDEX IF EXISTS public.records_request_idx;
DROP INDEX IF EXISTS public.tool_uses_tool_idx;
