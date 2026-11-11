-- latency_rollup's grain and lifecycle for text_chars p50/p90.
CREATE TABLE IF NOT EXISTS response_size_rollup (
  bucket_s  INTEGER      NOT NULL,
  bucket    TIMESTAMPTZ  NOT NULL,
  proj_id   INTEGER      NOT NULL REFERENCES projects(proj_id)
                          ON DELETE CASCADE,
  model_id  SMALLINT     NOT NULL REFERENCES dim_model(id),
  n         BIGINT       NOT NULL,
  p50       DOUBLE PRECISION,
  p90       DOUBLE PRECISION,
  PRIMARY KEY (bucket_s, bucket, proj_id, model_id)
);
CREATE INDEX IF NOT EXISTS response_size_rollup_lookup_idx
  ON response_size_rollup (bucket_s, proj_id, bucket);
