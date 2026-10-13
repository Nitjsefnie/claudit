-- The reprice-column sidecar (issue #959, ruling R4).
--
-- Applied in schema's one stamped transaction (backend/schema_files.py),
-- after schema_dims.sql and schema_types.sql establish the record shape and widths.
-- The reprice-written columns (cost_usd, long_context, the meter
-- multipliers, the retired request_fee_usd, and the pricing_version /
-- rate_fingerprint staleness markers as dim FK ids) move to this narrow
-- (file_id, line_num) sidecar at fillfactor 50; `records` returns to
-- fillfactor 100. Tuple versions are whole-row, so an in-row column
-- split could never lower `records`' fillfactor — only this physical
-- split can. Cost accepted by R4: a PK join on cost-reading live
-- queries; rollup-served paths are unaffected.
--
-- Pair invariant: exactly one production writer (ingest_persist._write_facts)
-- writes BOTH the records row and its records_price row in one transaction.
-- Cost readers join the pair in both styles, and a missing-sidecar fault
-- degrades differently under each: the INNER joins (api_cache's fold, the
-- sessions list, api.py's cost-by panels) DROP the records row from list/
-- cost paths, while the LEFT joins (api_dashboard's fold,
-- ingest_rollup_hourly, the session detail) keep the row and ZERO its cost.
-- Production never has a missing sidecar row — one writer, one transaction
-- — so keep that single-writer invariant if a writer is ever added.
--
-- Why the sidecar keeps #350's behaviour: the reprice pass rewrites
-- every stale row on every PRICING_VERSION bump, and only the sidecar's
-- own PK index is touched now. At fillfactor 50 a page absorbs its
-- rows' new tuple versions in place (HOT) — measured pre-split at 57.6s
-- / 2.66 GB WAL per full-table restamp on full pages vs 12.2s / 499 MB
-- HOT at 50, stable across consecutive bump cycles — while `records`
-- stores full at 100 and stops reserving half of every page for a
-- rewrite that no longer lands there. One-time compaction of an existing
-- deploy's `records` (the reloption only shapes future page use):
-- `VACUUM FULL records` — ACCESS EXCLUSIVE, so off-peak (Task 4 owns the
-- vacuum follow-up).
--
-- Deployment states, and what each one takes:
--   * fresh database, or a pre-#959 deploy: the width guard's drop and
--     schema_dims.sql's CREATE have just rebuilt `records` in the
--     surrogate shape WITH these columns in-row, so the guard below
--     finds them, carries nothing (the table is empty) and drops them;
--     the PARSER_VERSION bump's reparse fills both tables from scratch.
--   * a dev database already in the surrogate shape and STILL in-row
--     (built by this chain minus this file): that in-row form is one of
--     records' two canonical signatures, so the width guard keeps the
--     rows and the same guard below fires WITH data — every value
--     (cost, flag, multipliers, fee, both dim ids) is copied into
--     records_price before the in-row columns drop, so no value
--     silently vanishes and none is zeroed. The production path never
--     needs the carry-over (the bump's full reparse rewrites every
--     row), but a dev DB that is already stamped would otherwise read
--     zeros until its next forced reparse.
--   * an already-split database: the guard does not fire; the drops
--     below remove the in-row columns (and schema_dims step 10 any
--     retired TEXT column a restore carried), and both fillfactors are
--     restamped.
-- Rollback stays one-directional: an older binary cannot read this
-- shape (issue #959's constraint, stated in the PR).
--
-- The FK from records_price to records(file_id, line_num) ON DELETE
-- CASCADE keeps the pair invariant under every deletion path (the
-- per-file wipe in ingest._persist, the suppressed-model purge, the
-- files cascade). schema_widths.sql's guarded DROP of a non-canonical
-- `records` drops this sidecar FIRST and explicitly — dropping records
-- alone would fail on this FK, and a sidecar row outliving its record
-- has no meaning. The sidecar is rebuilt with records by the same
-- path, so the pair never splits.

-- 1. The sidecar. Dim FK ids, not TEXT (the binding resolution): the
--    sidecar mirrors the columns' types exactly, so carried-over values
--    convert without a cast. Widths match records' own columns (task 7):
--    INTEGER file_id, SMALLINT dimension ids.
CREATE TABLE IF NOT EXISTS records_price (
  file_id                  INTEGER    NOT NULL,
  line_num                 INTEGER    NOT NULL,
  cost_usd                 NUMERIC(12,6) NOT NULL DEFAULT 0,
  long_context             BOOLEAN,
  long_context_input_mult  DOUBLE PRECISION,
  long_context_output_mult DOUBLE PRECISION,
  request_fee_usd          NUMERIC(12,6),
  pricing_version_id       SMALLINT   REFERENCES dim_pricing_version(id),
  rate_fingerprint_id      SMALLINT   REFERENCES dim_rate_fingerprint(id),
  PRIMARY KEY (file_id, line_num),
  FOREIGN KEY (file_id, line_num) REFERENCES records (file_id, line_num)
    ON DELETE CASCADE
) WITH (fillfactor = 50);

-- 2. Carry-over for a database still in the in-row shape, guarded on
--    records.cost_usd — the marker this file's own step 3 drops, so a
--    second run skips the copy while the unconditional drops below still
--    remove the in-row columns. ON CONFLICT
--    DO NOTHING: if a sidecar row somehow exists already, the stored
--    sidecar value wins.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'records'
       AND column_name = 'cost_usd'
  ) THEN
    INSERT INTO records_price (file_id, line_num, cost_usd, long_context,
                               long_context_input_mult,
                               long_context_output_mult, request_fee_usd,
                               pricing_version_id, rate_fingerprint_id)
    SELECT file_id, line_num, cost_usd, long_context,
           long_context_input_mult, long_context_output_mult,
           request_fee_usd, pricing_version_id, rate_fingerprint_id
      FROM records
    ON CONFLICT DO NOTHING;
  END IF;
END $$;

-- 3. Drop the in-row columns: the carry-over above when they hold data.
--    After this the sidecar owns every one of them; `records` never
--    carries a copy again, so a re-apply finds nothing to drop and
--    creates nothing to burn an attnum on.
ALTER TABLE records DROP COLUMN IF EXISTS cost_usd;
ALTER TABLE records DROP COLUMN IF EXISTS long_context;
ALTER TABLE records DROP COLUMN IF EXISTS long_context_input_mult;
ALTER TABLE records DROP COLUMN IF EXISTS long_context_output_mult;
ALTER TABLE records DROP COLUMN IF EXISTS pricing_version_id;
ALTER TABLE records DROP COLUMN IF EXISTS rate_fingerprint_id;
ALTER TABLE records DROP COLUMN IF EXISTS request_fee_usd;

-- 4. Fillfactor intent, restamped on every startup (SV-SCHEMA-AUTOAPPLY):
--    exactly one assignment per table in the whole chain — the reprice
--    rewrites land HOT on records_price at 50; records stores full.
ALTER TABLE records SET (fillfactor = 100);
ALTER TABLE records_price SET (fillfactor = 50);
