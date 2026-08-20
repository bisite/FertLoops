-- Continuous aggregates for `reading`: an hourly rollup, and a daily
-- rollup stacked on top of it (hierarchical continuous aggregates —
-- docs/adr/0009-timescaledb-layer-hypertable-and-aggregates.md). Requires
-- 000001_reading_hypertable to already be applied.
--
-- Reading corrections (docs/adr/0007): these aggregates summarize
-- ORIGINALS ONLY -- reading_hourly filters `WHERE corrects_reading_id IS
-- NULL`, and reading_daily inherits that because it is built from
-- reading_hourly rather than from `reading`.
--
-- The filter is not a preference, it is a correctness fix.
-- uq_reading_sensor_observed_at_original is a PARTIAL unique index: a
-- correction shares (sensor_id, observed_at) with the row it corrects and
-- both stay live in `reading` forever. Unfiltered, that double-counts
-- reading_count, skews avg_value, and creates a tied last().
--
-- What these aggregates therefore do NOT do is resolve corrections -- a
-- corrected instant contributes nothing at all here, rather than
-- contributing the corrected value. Reconciling that is still a
-- consumer-side concern (docs/adr/0007), and it genuinely cannot be
-- folded into an incremental refresh: a correction inserted today can
-- change which row should win for an instant whose bucket was already
-- materialized. Note that the obvious escape hatch does NOT work -- a
-- continuous aggregate can only be defined over a hypertable or another
-- continuous aggregate, never over a plain view, so "resolve corrections
-- in a view and aggregate over that" is not available. Query `reading`
-- directly for exact correction resolution.

-- ============================================================
-- reading_hourly
-- ============================================================

CREATE MATERIALIZED VIEW reading_hourly
WITH (timescaledb.continuous) AS
SELECT
    sensor_id,
    time_bucket(INTERVAL '1 hour', observed_at) AS bucket,
    COUNT(*) AS reading_count,
    AVG(value) AS avg_value,
    -- Meaningless for most sensor types, but exact for delta-style ones
    -- (e.g. a per-interval litre volume), and it lets reading_daily
    -- compute its weighted average without multiplying avg_value back
    -- through reading_count.
    SUM(value) AS sum_value,
    MIN(value) AS min_value,
    MAX(value) AS max_value,
    last(value, observed_at) AS last_value
FROM reading
WHERE corrects_reading_id IS NULL
GROUP BY sensor_id, bucket
WITH NO DATA;

-- Real-time aggregation: disabled by default since TimescaleDB 2.13.
-- Enabled here for the hourly rollup so "this hour so far" is visible
-- immediately in dashboards, at the cost of also scanning recent raw
-- rows on every query. Left at the (coarser, less useful for
-- partial-bucket freshness) default for reading_daily below.
ALTER MATERIALIZED VIEW reading_hourly SET (timescaledb.materialized_only = false);

-- start_offset omitted (NULL) refreshes all history on each run — fine at
-- this bucket size. end_offset/schedule_interval of 15 minutes matches
-- general TimescaleDB guidance for frequently-refreshed dashboard-facing
-- aggregates.
SELECT add_continuous_aggregate_policy('reading_hourly',
    start_offset => NULL,
    end_offset => INTERVAL '15 minutes',
    schedule_interval => INTERVAL '15 minutes');

-- Columnstore settings are inferred from the view's own GROUP BY/time
-- structure (segment by sensor_id, order by bucket) — no explicit
-- segmentby/orderby needed here, unlike the raw hypertable.
ALTER MATERIALIZED VIEW reading_hourly SET (timescaledb.enable_columnstore = true);

-- after must exceed the refresh policy's start_offset (NULL/unbounded
-- here) in principle — 3 days keeps recently-refreshed buckets in the
-- rowstore a while before compressing, matching general guidance for
-- hourly rollups.
CALL add_columnstore_policy('reading_hourly', after => INTERVAL '3 days');

CREATE INDEX idx_reading_hourly_sensor_bucket ON reading_hourly (sensor_id, bucket DESC);

-- ============================================================
-- reading_daily — hierarchical: built from reading_hourly, not from
-- reading directly. Valid because 1 day is an exact multiple of the
-- 1 hour bucket below it (TimescaleDB requires this for stacked caggs).
--
-- LOCAL DAYS, NOT UTC DAYS. The timezone argument is load-bearing, not
-- decoration. Without it, time_bucket anchors day boundaries at
-- 00:00 UTC -- it defaults to UTC+0 and, unlike date_trunc, it ignores
-- the session TimeZone entirely, so no server or client setting can fix
-- it. In Spain (UTC+1/+2) that files the first hour or two of every
-- local day under the previous day: the water drawn just after local
-- midnight gets credited to yesterday, every single day. Measured on a
-- DST boundary, a UTC-bucketed query for "29 March" returned 1440
-- minutes where the true local day has 1380 (23 hours).
--
-- The timezone must be an IANA name, never a fixed offset like '+01' --
-- a fixed offset is precisely what breaks when DST shifts.
--
-- Cost of this choice, and it is real: a timezone-aware day bucket is
-- VARIABLE-width (23/24/25 hours), and TimescaleDB refuses to stack a
-- FIXED-width cagg on top of a variable-width one. So a future weekly or
-- monthly rollup built on reading_daily must carry the timezone too
-- (variable-on-variable is allowed, verified). It also makes this
-- aggregate site-specific: a second greenhouse in another timezone would
-- need its own. See docs/adr/0009.
-- ============================================================

CREATE MATERIALIZED VIEW reading_daily
WITH (timescaledb.continuous) AS
SELECT
    sensor_id,
    time_bucket(INTERVAL '1 day', bucket, timezone => 'Europe/Madrid') AS bucket,
    SUM(reading_count) AS reading_count,
    -- Weighted, not a naive AVG(avg_value): hours with more/fewer
    -- readings must not be treated as equal-sized samples. Computed
    -- straight from the stored sums rather than multiplying avg_value
    -- back through reading_count.
    SUM(sum_value) / NULLIF(SUM(reading_count), 0) AS avg_value,
    SUM(sum_value) AS sum_value,
    MIN(min_value) AS min_value,
    MAX(max_value) AS max_value,
    last(last_value, bucket) AS last_value
FROM reading_hourly
GROUP BY sensor_id, time_bucket(INTERVAL '1 day', bucket, timezone => 'Europe/Madrid')
WITH NO DATA;

-- Left at the default (materialized_only = true): a day bucket is
-- mostly incomplete for "today" regardless of real-time aggregation, so
-- the freshness benefit that justifies it on reading_hourly doesn't
-- carry over here.

SELECT add_continuous_aggregate_policy('reading_daily',
    start_offset => NULL,
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour');

ALTER MATERIALIZED VIEW reading_daily SET (timescaledb.enable_columnstore = true);

CALL add_columnstore_policy('reading_daily', after => INTERVAL '7 days');

CREATE INDEX idx_reading_daily_sensor_bucket ON reading_daily (sensor_id, bucket DESC);

-- Both views are created WITH NO DATA and populate on their first
-- scheduled refresh (or on demand via a manual refresh_continuous_aggregate
-- call) — not synchronously here, since that would mean scanning all of
-- `reading` inside this migration on a project that already has a large
-- table.
