-- 2026-10-03 (issue #469): legacy provenance column for the former
-- note-derived request fee. Issue #879 retires that path; ingest no longer
-- writes the column and repricing clears it to NULL. Keep it additive and
-- nullable (SV-SCHEMA-AUTOAPPLY) so older binaries can ignore it and
-- rollback remains one-directional. A separate SCHEMA_PATHS entry keeps
-- schema.sql within its size ratchet.
ALTER TABLE records ADD COLUMN IF NOT EXISTS request_fee_usd NUMERIC(12,6);
