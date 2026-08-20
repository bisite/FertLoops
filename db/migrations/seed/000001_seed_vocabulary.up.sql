-- FertLoops vocabulary seed: the closed controlled vocabulary the ingestion
-- pipeline resolves against.
--
-- These rows live in `core` tables but their values are project-specific,
-- so they do not belong in migrations/core, which stays generic
-- (docs/adr/0004). They are not application-layer schema either, so they
-- get their own migration source: db/migrations/seed, tracked in
-- schema_migrations_seed. See docs/adr/0008 and db/migrations/README.md.
--
-- Every row is derived from the frozen UART contract
-- (docs/trama-de-datos-riego.md, agreed in issue #2) and from the
-- canonical measurement model in docs/adr/0011. Seventeen series per
-- Device: nine measured magnitudes, five equipment error/state codes and
-- three control echoes.
--
-- Idempotent: ON CONFLICT DO NOTHING everywhere, so re-applying is safe.
-- Applied WITHOUT x-multi-statement, so the whole seed is one transaction.
--
-- The vocabulary is CLOSED by design: the pipeline rejects a
-- (context, magnitude, unit) triple that is absent here rather than
-- creating it, which is what stops unit drift (docs/adr/0005). Adding a
-- magnitude is therefore a new migration, not a config edit.

-- ============================================================
-- sensor_context
--
-- The three measured media, plus `equipment` for values that describe the
-- hardware itself rather than an environment: subsystem error codes and
-- actuator echoes. Named `equipment` and not `device` to avoid colliding
-- with the `device` table in prose and in queries.
-- ============================================================

INSERT INTO sensor_context (context) VALUES
('soil'),
('water'),
('air'),
('equipment')
ON CONFLICT (context) DO NOTHING;

-- ============================================================
-- unit
--
-- `-` and `code` are both dimensionless but deliberately distinct: `-` is
-- a measurement without units (pH), while `code` is a categorical value
-- where arithmetic is meaningless. Keeping them apart stops anyone
-- averaging an error code by accident.
-- ============================================================

INSERT INTO unit (symbol, name) VALUES
('-', 'dimensionless'),
('code', 'enumerated code'),
('ppm', 'parts per million'),
('mS/cm', 'milliSiemens per centimetre'),
('degC', 'degree Celsius'),
('%', 'percent'),
('W/m2', 'watt per square metre'),
('L', 'litre'),
('deg', 'degree of arc'),
('Hz', 'hertz')
ON CONFLICT (symbol) DO NOTHING;

-- ============================================================
-- sensor_type
--
-- The UART path each triple comes from is in the trailing comment. Note
-- `conductivity` appears twice with different units and contexts: soil is
-- a conductivity probe in mS/cm, water is a TDS probe in ppm. They are
-- different magnitudes, not the same one converted, and the unique key
-- (sensor_context_id, magnitude, unit_id) keeps them apart -- no
-- conversion is applied anywhere (docs/adr/0011).
-- ============================================================

INSERT INTO sensor_type (sensor_context_id, unit_id, magnitude)
SELECT
    c.id AS sensor_context_id,
    u.id AS unit_id,
    v.magnitude
FROM (
    VALUES
    -- Nine measured magnitudes
    ('water', '-', 'ph'),                              -- Data.pH
    ('water', 'ppm', 'conductivity'),                  -- Data.CE
    ('water', 'L', 'irrigation_volume'),               -- Data.Volume (per-interval delta)
    ('soil', 'degC', 'temperature'),                   -- Data.THC.T
    ('soil', '%', 'humidity'),                         -- Data.THC.H
    ('soil', 'mS/cm', 'conductivity'),                 -- Data.THC.C
    ('air', 'degC', 'temperature'),                    -- Data.TH.T
    ('air', '%', 'humidity'),                          -- Data.TH.H
    ('air', 'W/m2', 'solar_radiation'),                -- Data.Solar
    -- Four error codes and one state enum
    ('equipment', 'code', 'adc_error'),                -- Errors.ADC
    ('equipment', 'code', 'pulse_counter_error'),      -- Errors.Pulses
    ('equipment', 'code', 'i2c_error'),                -- Errors.I2C
    ('equipment', 'code', 'inverter_error'),           -- Errors.Inverter
    -- Errors.Inverter_State -- a VFD state enum, not an error code
    ('equipment', 'code', 'inverter_state'),
    -- Three control echoes
    ('equipment', 'deg', 'valve_position'),            -- Control.Valve (0-90)
    ('equipment', 'code', 'inverter_on'),              -- Control.Inv.On
    ('equipment', 'Hz', 'inverter_frequency')          -- Control.Inv.Freq
) AS v (context, unit_symbol, magnitude)
INNER JOIN sensor_context AS c ON c.context = v.context
INNER JOIN unit AS u ON u.symbol = v.unit_symbol
ON CONFLICT (sensor_context_id, magnitude, unit_id) DO NOTHING;
