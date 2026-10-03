-- 2026-10-03 (issue #469): the per-request fee a serving host's provider
-- row charges beside its token rates (a RECORDED_FEE note, e.g.
-- web_search $0.01/request), folded into cost_usd at parse and at reprice
-- and stored on records.request_fee_usd for provenance. NULL on every
-- record whose resolved entry carries no fee — every other lane, every
-- unmodelled listing. Additive and nullable (SV-SCHEMA-AUTOAPPLY); older
-- binaries ignore the column and price by the token columns they read,
-- so rollback stays one-directional. A separate SCHEMA_PATHS entry so
-- schema.sql stays within its size ratchet.
ALTER TABLE records ADD COLUMN IF NOT EXISTS request_fee_usd NUMERIC(12,6);
