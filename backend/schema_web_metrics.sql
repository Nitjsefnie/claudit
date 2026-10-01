-- Frontend performance telemetry (issue #436): the browser side of what
-- latency_rollup measures on the model side. Raw beacons, plus percentile
-- rollups keyed on the LATENCY_BUCKETS widths.
--
-- A SEPARATE file from schema.sql, applied immediately after it in the same
-- transaction and under the same content stamp (db.SCHEMA_PATHS). The reason
-- is the module-size ratchet: schema.sql sits exactly at its recorded
-- ceiling, a recorded size that is never raised and never seeded for an
-- existing family, so a table added there fails the gate for good. The
-- ratchet's own remedy — relocate the code into a new module — is what this
-- is, and keeping the tables together here means the next split has a
-- precedent and a boundary to follow.
--
-- `metric`/`part`/`region`/`phase` are CLOSED vocabularies enforced by
-- backend/web_metrics.py at the sink (it refuses an unknown term with a 400),
-- not by CHECK constraints: the sink is the only writer, and an unconstrained
-- TEXT column would let a bug mint a new rollup grain per request. `value` is
-- milliseconds for the timing parts and a dimensionless Layout Instability
-- score for `shift`, clamped to its part's own range. `region`/`phase` are ''
-- on a row that carries neither, so the rollup grain is uniform.
--
-- Not project-scoped and not user-private: a beacon names a journey, not a
-- session, and guests may send them (session._guest_denied leaves
-- /api/metrics alone). `user_id` is 0 for a guest, as the session layer
-- reports it. The (user_id, ts) index serves the sink's per-user row cap.
--
-- `user_id` is BIGINT because the session layer reports the AUTH database's
-- user_id, and there that column is a BIGINT: a named user's id exceeds int4
-- on this host, and an INTEGER here raised
-- `psycopg.errors.NumericValueOutOfRange: integer out of range` in the
-- sink's executemany on a real beacon from a logged-in user. Guests (0) are
-- what the sink was exercised with, so the narrower column was never held to
-- a value it could not store.
CREATE TABLE IF NOT EXISTS web_metrics (
  id       BIGSERIAL   PRIMARY KEY,
  ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
  user_id  BIGINT      NOT NULL DEFAULT 0,
  metric   TEXT        NOT NULL,
  part     TEXT        NOT NULL DEFAULT '',
  region   TEXT        NOT NULL DEFAULT '',
  phase    TEXT        NOT NULL DEFAULT '',
  value    DOUBLE PRECISION NOT NULL
);

-- The guarded, idempotent widening for a database created while the column
-- was still INTEGER: a no-op once it is BIGINT. This is the same shape, and
-- the same allowance, as `user_session.user_id` in schema.sql — a pure
-- int->bigint widening is inert for an older binary, which only ever wrote
-- ids that fit and reads them indifferent to the column's width; nothing
-- drops, nothing narrows (SV-SCHEMA-AUTOAPPLY's guarded exceptions).
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_attribute
     WHERE attrelid = 'web_metrics'::regclass
       AND attname = 'user_id'
       AND NOT attisdropped
       AND atttypid = 'integer'::regtype
  ) THEN
    ALTER TABLE web_metrics ALTER COLUMN user_id TYPE BIGINT;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS web_metrics_ts_idx ON web_metrics (ts);
CREATE INDEX IF NOT EXISTS web_metrics_user_ts_idx ON web_metrics (user_id, ts);
CREATE INDEX IF NOT EXISTS web_metrics_grain_idx
  ON web_metrics (metric, part, region, phase, ts);

-- Percentiles DO NOT COMPOSE, so — exactly like latency_rollup — one row is
-- stored per display-bucket width, each over its own bucket's whole
-- population. `total` is the exception: a pure SUM composes, and it is what
-- the layout-shift and long-task readouts need, a cumulative layout shift
-- being a sum of shift values and not a percentile of them. No all-projects
-- row: a browser metric has no project to filter by.
CREATE TABLE IF NOT EXISTS web_metrics_rollup (
  bucket_s  INTEGER     NOT NULL,
  bucket    TIMESTAMPTZ NOT NULL,
  metric    TEXT        NOT NULL,
  part      TEXT        NOT NULL DEFAULT '',
  region    TEXT        NOT NULL DEFAULT '',
  phase     TEXT        NOT NULL DEFAULT '',
  n         BIGINT      NOT NULL,
  p50       DOUBLE PRECISION,
  p75       DOUBLE PRECISION,
  total     DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (bucket_s, bucket, metric, part, region, phase)
);

CREATE INDEX IF NOT EXISTS web_metrics_rollup_lookup_idx
  ON web_metrics_rollup (bucket_s, metric, bucket);
