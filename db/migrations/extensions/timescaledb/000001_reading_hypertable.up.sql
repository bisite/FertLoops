-- TimescaleDB enhancement layer: converts `reading` into a hypertable.
--
-- Optional, opt-in per project (docs/adr/0004-schema-layers.md).
-- This is an independent golang-migrate source (docs/adr/0008) from
-- `core` — apply it with its own -database x-migrations-table, after
-- db/migrations/core/000001_init_core_schema.up.sql has run. A project that
-- doesn't want TimescaleDB can skip this source entirely — `reading`
-- stays a plain table and nothing else in core depends on it.
--
-- `reading` is core's highest-volume, insert-heavy, append-only table
-- (docs/adr/0005-core-schema-keys-timestamps-vocabularies.md) and scores as a strong
-- hypertable candidate. See docs/adr/0009-timescaledb-layer-hypertable-and-aggregates.md
-- for why the primary key and self-referencing FK below change shape.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============================================================
-- Preconditions
--
-- TimescaleDB requires every PRIMARY KEY/UNIQUE constraint on a
-- hypertable to include the partition column, and does not support
-- foreign keys that reference a hypertable — including a table
-- referencing itself. Adjust reading accordingly before conversion.
-- ============================================================

-- Hypertable-to-hypertable FKs are unsupported, and `reading` becomes its
-- own FK target via corrects_reading_id. Referential integrity for
-- corrections becomes an application-layer responsibility once this
-- enhancement is applied — the column and its intent
-- (docs/adr/0007-append-only-readings-corrections.md) are unchanged,
-- only the database-level enforcement goes away.
ALTER TABLE reading DROP CONSTRAINT fk_reading_corrects_reading;

-- The primary key must include the partition column (observed_at). `id`
-- remains effectively unique in practice (it's still a single identity
-- sequence) — this only relaxes what Postgres enforces.
ALTER TABLE reading DROP CONSTRAINT reading_pkey;
ALTER TABLE reading ADD CONSTRAINT reading_pkey PRIMARY KEY (id, observed_at);

-- ============================================================
-- Hypertable conversion
--
-- Partition column: observed_at (device-reported event time) — the
-- column this schema already queries and indexes by
-- (idx_reading_sensor_id_observed_at), not created_at (system-received
-- time). Chunk interval defaults to 7 days per general TimescaleDB
-- guidance when actual ingestion volume is unknown — tune per project
-- once it is (target: recent chunk indexes fit in <25% of RAM).
-- migrate_data => true handles the case where a project applies this
-- after `reading` already has rows.
-- ============================================================

SELECT create_hypertable(
    'reading',
    'observed_at',
    chunk_time_interval => INTERVAL '7 days',
    migrate_data => true
);

-- ============================================================
-- Columnstore (compression)
--
-- segment_by: sensor_id — the existing primary filter dimension for
-- readings (idx_reading_sensor_id_observed_at) — assumes >100 rows per
-- sensor per chunk. Revisit (drop segment_by, or fold sensor_id into
-- order_by instead) if a project's sensors report less frequently than
-- that.
-- order_by: observed_at DESC — natural time-series progression.
-- sparse_index: minmax(value) — supports range/outlier queries
-- (e.g. value > x) on compressed chunks without decompressing them.
-- ============================================================

ALTER TABLE reading SET (
    timescaledb.enable_columnstore = true,
    timescaledb.segmentby = 'sensor_id',
    timescaledb.orderby = 'observed_at DESC'
);

ALTER TABLE reading SET (
    timescaledb.sparse_index = 'minmax(value)'
);

-- Enabling the columnstore above only makes compression POSSIBLE -- it
-- does not schedule it. Verified on TimescaleDB 2.27.2: after
-- `SET (timescaledb.enable_columnstore = true)` the hypertable has zero
-- jobs, so without the call below nothing ever gets compressed. (Earlier
-- revisions of this file claimed >= 2.23 auto-creates a 7-day policy.
-- That does not hold here, and leaving the call commented out meant raw
-- readings were never compressed at all.)
--
-- 14 days, not 7: a reconnecting gateway backfills readings with old
-- observed_at values, and a delay of one full chunk interval gives that
-- backfill rowstore headroom instead of forcing a decompress/recompress
-- on every reconnect.
CALL add_columnstore_policy('reading', after => INTERVAL '14 days');

-- ============================================================
-- Retention policy — deliberately NOT set, and at this project's volume
-- the recommendation is to never set one. Roughly 80k rows/day
-- compresses to a fraction of a gigabyte per year, so dropping raw
-- readings buys almost no storage while permanently destroying the only
-- uncorrected, full-resolution record there is. Keeping raw forever is
-- also what makes the aggregates' unbounded refresh affordable, since
-- they stay a regenerable cache rather than standing in for dropped raw.
--
-- Ratifying or diverging from that is
-- https://github.com/bisite/FertLoops/issues/14. If a window is ever
-- adopted, uncomment and set it here.
-- ============================================================
-- SELECT add_retention_policy('reading', drop_after => INTERVAL '<project retention period>');

-- ============================================================
-- Continuous aggregates (hourly/daily rollups of reading) are their own
-- migration: 000002_reading_continuous_aggregates.
-- ============================================================
