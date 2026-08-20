-- IoT Core schema — initial creation.
--
-- Reference engine: PostgreSQL, conservative/standard-flavored SQL.
-- No extensions required (gen_random_uuid() is native since PG 13).
-- Primary keys are UUID except `reading`, which uses BIGINT identity —
-- see docs/adr/0005-core-schema-keys-timestamps-vocabularies.md.
-- See docs/adr/0004 through 0010 for the design decisions behind this file,
-- and CONTEXT.md for the canonical domain glossary.
--
-- Applied via golang-migrate (docs/adr/0008): this is version 1 of the
-- `core` migration source (db/migrations/core/). See db/migrations/README.md
-- for how to run it.

-- ============================================================
-- Shared: updated_at maintenance
--
-- Append-only tables (reading) intentionally have no updated_at and are
-- not attached to this trigger (docs/adr/0005-core-schema-keys-timestamps-vocabularies.md).
-- ============================================================

CREATE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- sensor_context
-- ============================================================

CREATE TABLE sensor_context (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    context TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_sensor_context_context UNIQUE (context)
);

COMMENT ON TABLE sensor_context IS
    'The environment a sensor type applies to (e.g. soil, water, air). '
    'A lookup table, not an enum — new contexts are a row insert.';
COMMENT ON COLUMN sensor_context.context IS
    'Canonical name of the environment, e.g. "soil", "water", "air".';

CREATE TRIGGER trg_sensor_context_updated_at
    BEFORE UPDATE ON sensor_context
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- unit
-- ============================================================

CREATE TABLE unit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_unit_symbol UNIQUE (symbol)
);

COMMENT ON TABLE unit IS
    'Controlled vocabulary of measurement units. A lookup table, not free '
    'text, to prevent drift ("C" / "Celsius" / "celsius") across projects '
    'and years (docs/adr/0005-core-schema-keys-timestamps-vocabularies.md).';
COMMENT ON COLUMN unit.symbol IS
    'Canonical unit symbol, e.g. "degC", "%", "hPa". Kept ASCII-safe by '
    'convention.';
COMMENT ON COLUMN unit.name IS 'Human-readable unit name, e.g. "Celsius".';

CREATE TRIGGER trg_unit_updated_at
    BEFORE UPDATE ON unit
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- sensor_type
-- ============================================================

CREATE TABLE sensor_type (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sensor_context_id UUID NOT NULL,
    unit_id UUID NOT NULL,
    magnitude TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_sensor_type_sensor_context
        FOREIGN KEY (sensor_context_id) REFERENCES sensor_context (id) ON DELETE RESTRICT,
    CONSTRAINT fk_sensor_type_unit
        FOREIGN KEY (unit_id) REFERENCES unit (id) ON DELETE RESTRICT,
    CONSTRAINT uq_sensor_type_context_magnitude_unit
        UNIQUE (sensor_context_id, magnitude, unit_id)
);

COMMENT ON TABLE sensor_type IS
    'Classification of sensors by what they measure (magnitude) and in '
    'what unit, within a sensor context — e.g. "temperature, in soil, in '
    'degC". A lookup table, not an enum.';
COMMENT ON COLUMN sensor_type.magnitude IS
    'The physical magnitude measured, e.g. "temperature", "humidity".';

CREATE TRIGGER trg_sensor_type_updated_at
    BEFORE UPDATE ON sensor_type
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_sensor_type_sensor_context_id ON sensor_type (sensor_context_id);
CREATE INDEX idx_sensor_type_unit_id ON sensor_type (unit_id);

-- ============================================================
-- device
-- ============================================================

CREATE TABLE device (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_device_name UNIQUE (name)
);

COMMENT ON TABLE device IS
    'A physical or logical thing that produces sensor readings. '
    'Deliberately has no owner/access-control reference — that is an '
    'application-layer concern (docs/adr/0006-what-core-deliberately-does-not-model.md). '
    'Deliberately has no status/decommissioned field — inferred from '
    'device_placement history instead (docs/adr/0006-what-core-deliberately-does-not-model.md).';
COMMENT ON COLUMN device.name IS
    'Human-readable device name, unique within this deployment.';

CREATE TRIGGER trg_device_updated_at
    BEFORE UPDATE ON device
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- location
-- ============================================================

CREATE TABLE location (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT,
    latitude NUMERIC(9, 6) NOT NULL,
    longitude NUMERIC(9, 6) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_location_latitude_range CHECK (latitude BETWEEN -90 AND 90),
    CONSTRAINT chk_location_longitude_range CHECK (longitude BETWEEN -180 AND 180)
);

COMMENT ON TABLE location IS
    'A named point in space (plain latitude/longitude). Polygon/area '
    'representation is deliberately not part of core — projects that need '
    'spatial indexing or geometry types add PostGIS as an opt-in '
    'enhancement layer (docs/adr/0006-what-core-deliberately-does-not-model.md).';
COMMENT ON COLUMN location.latitude IS
    'Decimal degrees, WGS84 assumed by convention (not enforced — no SRID '
    'metadata in core).';
COMMENT ON COLUMN location.longitude IS
    'Decimal degrees, WGS84 assumed by convention (not enforced — no SRID '
    'metadata in core).';

CREATE TRIGGER trg_location_updated_at
    BEFORE UPDATE ON location
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- device_placement
-- ============================================================

CREATE TABLE device_placement (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id UUID NOT NULL,
    location_id UUID NOT NULL,
    placed_at TIMESTAMPTZ NOT NULL,
    removed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_device_placement_device
        FOREIGN KEY (device_id) REFERENCES device (id) ON DELETE RESTRICT,
    CONSTRAINT fk_device_placement_location
        FOREIGN KEY (location_id) REFERENCES location (id) ON DELETE RESTRICT,
    CONSTRAINT chk_device_placement_removed_after_placed
        CHECK (removed_at IS NULL OR removed_at > placed_at)
);

COMMENT ON TABLE device_placement IS
    'Append-only history of which location a device was placed at, and '
    'when. A device''s current location is the placement row with no '
    'removed_at set. Absence of an active placement is also how '
    '"decommissioned" is inferred, rather than a separate status field '
    '(docs/adr/0006-what-core-deliberately-does-not-model.md).';
COMMENT ON COLUMN device_placement.removed_at IS
    'When the device was removed from this location. NULL means this is '
    'the device''s current placement.';

CREATE TRIGGER trg_device_placement_updated_at
    BEFORE UPDATE ON device_placement
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_device_placement_device_id ON device_placement (device_id);
CREATE INDEX idx_device_placement_location_id ON device_placement (location_id);

-- A device can only be in one place at a time — enforces that
-- device_placement always has at most one row with removed_at IS NULL per
-- device, which is what makes the decommissioning inference in
-- docs/adr/0006 well-defined.
CREATE UNIQUE INDEX uq_device_placement_one_active_per_device
    ON device_placement (device_id)
    WHERE removed_at IS NULL;

-- ============================================================
-- sensor
-- ============================================================

CREATE TABLE sensor (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id UUID NOT NULL,
    sensor_type_id UUID NOT NULL,
    label TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_sensor_device
        FOREIGN KEY (device_id) REFERENCES device (id) ON DELETE RESTRICT,
    CONSTRAINT fk_sensor_sensor_type
        FOREIGN KEY (sensor_type_id) REFERENCES sensor_type (id) ON DELETE RESTRICT
);

COMMENT ON TABLE sensor IS
    'A specific measuring instrument attached to a device, of a given '
    'sensor type. Multiple sensors of the same type may exist on the same '
    'device — uniqueness is structural (UUID keys), not a sequence number '
    '(docs/adr/0006-what-core-deliberately-does-not-model.md).';
COMMENT ON COLUMN sensor.label IS
    'Optional, non-unique free-text label for human reference, e.g. '
    '"north bed". Not load-bearing.';

CREATE TRIGGER trg_sensor_updated_at
    BEFORE UPDATE ON sensor
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_sensor_device_id ON sensor (device_id);
CREATE INDEX idx_sensor_sensor_type_id ON sensor (sensor_type_id);

-- ============================================================
-- reading
-- ============================================================

CREATE TABLE reading (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sensor_id UUID NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    corrects_reading_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Deliberately no updated_at: reading rows are never mutated after
    -- insert (docs/adr/0005, docs/adr/0007). Corrections are new rows.
    CONSTRAINT fk_reading_sensor
        FOREIGN KEY (sensor_id) REFERENCES sensor (id) ON DELETE RESTRICT,
    CONSTRAINT fk_reading_corrects_reading
        FOREIGN KEY (corrects_reading_id) REFERENCES reading (id) ON DELETE RESTRICT
);

COMMENT ON TABLE reading IS
    'A single validated sensor measurement at a point in time. Assumed '
    'already clean/validated before reaching this schema '
    '(docs/adr/0006-what-core-deliberately-does-not-model.md). Append-only: never updated '
    'or deleted. A wrong reading is corrected by inserting a new row '
    'referencing it via corrects_reading_id '
    '(docs/adr/0007-append-only-readings-corrections.md). Uses a BIGINT '
    'identity primary key rather than UUID, unlike every other table in '
    'this schema — see docs/adr/0005-core-schema-keys-timestamps-vocabularies.md.';
COMMENT ON COLUMN reading.value IS
    'The validated measurement value, in the unit defined by this '
    'reading''s sensor -> sensor_type -> unit chain.';
COMMENT ON COLUMN reading.observed_at IS
    'When the device took this measurement (device-reported event time) — '
    'not when the row was written.';
COMMENT ON COLUMN reading.created_at IS
    'When this row was inserted (system-received time). Doubles as the '
    '"when did we learn about this" signal for delayed/batched uploads.';
COMMENT ON COLUMN reading.corrects_reading_id IS
    'If set, this row is a correction superseding the referenced reading. '
    'The referenced row is never modified or deleted.';

CREATE INDEX idx_reading_sensor_id_observed_at ON reading (sensor_id, observed_at);
CREATE INDEX idx_reading_corrects_reading_id
    ON reading (corrects_reading_id)
    WHERE corrects_reading_id IS NOT NULL;

-- An original (non-correcting) reading is unique per sensor+instant.
-- Corrections are explicitly exempt from this: a correction shares the
-- same sensor_id/observed_at as the reading it corrects, by design.
CREATE UNIQUE INDEX uq_reading_sensor_observed_at_original
    ON reading (sensor_id, observed_at)
    WHERE corrects_reading_id IS NULL;
