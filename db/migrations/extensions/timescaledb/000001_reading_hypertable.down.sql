-- Reverts db/migrations/extensions/timescaledb/000001_reading_hypertable.up.sql
--
-- TimescaleDB has no built-in "un-hypertable" operation, so this rebuilds
-- `reading` as a plain table: copy its data into a fresh ordinary table
-- (querying a hypertable transparently reads compressed chunks too, so no
-- explicit decompression step is needed), drop the hypertable — which
-- also drops its chunks and any policies/jobs attached to it — and
-- restore reading to the exact shape
-- db/migrations/core/000001_init_core_schema.up.sql created. CREATE TABLE
-- ... LIKE never copies foreign keys or index/constraint names, so the
-- primary key, both foreign keys, and the named indexes are all
-- recreated explicitly below rather than relied on from LIKE.
--
-- If a project uncommented the retention policy or continuous
-- aggregates from the up migration, drop those explicitly first —
-- continuous aggregates are materialized views, not something dropping
-- the hypertable cleans up automatically.

CREATE TABLE reading_plain (
    LIKE reading INCLUDING COMMENTS INCLUDING DEFAULTS INCLUDING GENERATED
        INCLUDING IDENTITY INCLUDING STORAGE
);

-- OVERRIDING SYSTEM VALUE is required: INCLUDING IDENTITY copied the
-- GENERATED ALWAYS identity onto reading_plain.id, and preserving the
-- original ids means writing explicit values into it.
INSERT INTO reading_plain
OVERRIDING SYSTEM VALUE
SELECT * FROM reading;

DROP TABLE reading;

ALTER TABLE reading_plain RENAME TO reading;

-- Advance the copied identity sequence past the preserved ids so the next
-- generated insert doesn't collide with an existing row. The third arg
-- (is_called) is "do rows exist" — with rows the next value is MAX(id)+1,
-- with none the sequence starts fresh at its first value.
SELECT setval(
    pg_get_serial_sequence('reading', 'id'),
    COALESCE((SELECT MAX(id) FROM reading), 1),
    (SELECT MAX(id) IS NOT NULL FROM reading)
);

ALTER TABLE reading ADD CONSTRAINT reading_pkey PRIMARY KEY (id);

ALTER TABLE reading
    ADD CONSTRAINT fk_reading_sensor
        FOREIGN KEY (sensor_id) REFERENCES sensor (id) ON DELETE RESTRICT;

ALTER TABLE reading
    ADD CONSTRAINT fk_reading_corrects_reading
        FOREIGN KEY (corrects_reading_id) REFERENCES reading (id) ON DELETE RESTRICT;

CREATE INDEX idx_reading_sensor_id_observed_at ON reading (sensor_id, observed_at);

CREATE INDEX idx_reading_corrects_reading_id
    ON reading (corrects_reading_id)
    WHERE corrects_reading_id IS NOT NULL;

CREATE UNIQUE INDEX uq_reading_sensor_observed_at_original
    ON reading (sensor_id, observed_at)
    WHERE corrects_reading_id IS NULL;
