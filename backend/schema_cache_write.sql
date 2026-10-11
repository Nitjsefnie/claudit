-- 2026-10-11 (issue #842): the provider-neutral cache-write count.
--
-- A request's input splits into three neutral categories on every lane:
-- read from cache (cache_read_tokens), written to cache
-- (cache_write_tokens), and the uncached tail
-- (fresh_tokens + cache_creation_tokens - cache_write_tokens). An
-- automatic-caching lane bills its implicit writes as fresh input, so
-- `fresh_tokens` cannot be shrunk; the neutral split therefore lands in
-- its own column, derived at parse time
-- (parse_common.resolve_cache_writes) from an explicit
-- `cache_creation_tokens` or the next request's cache-read growth.
--
-- It is NOT a pricing input -- compute_cost never sees it -- so billing
-- is byte-identical. Additive and NOT NULL DEFAULT 0
-- (SV-SCHEMA-AUTOAPPLY): a row stored before the PARSER_VERSION bump's
-- reparse keeps 0 until then, and an older binary ignores the column
-- (rollback stays one-directional). A separate SCHEMA_PATHS entry keeps
-- schema.sql within its size ratchet.
ALTER TABLE records ADD COLUMN IF NOT EXISTS
  cache_write_tokens BIGINT NOT NULL DEFAULT 0;
