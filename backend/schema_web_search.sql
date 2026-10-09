-- 2026-10-09 (issue #879): a transcript's per-record web-search count.
-- NULL means the lane did not carry a usable count; positive values are
-- billed against the record's dated provider or vendor web_search rate.
-- Additive and nullable so older rows and unsupported lanes remain honest
-- until reparsed (SV-PARSER-SPEC, SV-SCHEMA-AUTOAPPLY).
ALTER TABLE records ADD COLUMN IF NOT EXISTS web_search_requests INTEGER;
