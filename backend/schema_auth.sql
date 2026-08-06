-- claudit session records, applied to the AUTH database (DATABASE_URL_AUTH),
-- alongside the shared users table. Phase 2 shares this table with the other
-- services; the DDL moves into the Discord bot's schema.sql at that point, but the
-- table and its rows do not move.

CREATE TABLE IF NOT EXISTS web_sessions (
  nonce         TEXT PRIMARY KEY,
  user_id       BIGINT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  user_agent    TEXT NOT NULL DEFAULT '',
  ip            TEXT NOT NULL DEFAULT '',
  revoked_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS web_sessions_user_id_idx
  ON web_sessions (user_id) WHERE revoked_at IS NULL;
