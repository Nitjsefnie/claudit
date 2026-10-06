-- 2026-10-06 (issue #687): a forked Codex rollout re-journals its parent's
-- requests in its replayed prefix, so the same uuid (or call_id) exists in
-- two files and the canonical winner must be the parent's original
-- whatever the key order. Set at parse time -- codex_fork.mark_replay
-- flags the fork's leading rows, before the fork's own first model
-- declaration. NULL (every non-Codex row, and rows written before this
-- column existed) ranks as an original in recompute_canonical's winner
-- rule; the PARSER_VERSION bump riding the same change reparses and
-- fills it. Additive and nullable (SV-SCHEMA-AUTOAPPLY); older binaries
-- ignore the column. A separate SCHEMA_PATHS entry so schema.sql stays
-- within its size ratchet.
ALTER TABLE records ADD COLUMN IF NOT EXISTS is_replay BOOLEAN;
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS is_replay BOOLEAN;
