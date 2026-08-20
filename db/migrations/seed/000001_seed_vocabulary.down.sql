-- Reverts db/migrations/seed/000001_seed_vocabulary.up.sql
--
-- Deletes in FK order: sensor_type first, then the lookup tables it
-- references. All foreign keys into these tables are ON DELETE RESTRICT,
-- so this migration FAILS -- by design -- if any `sensor` row still
-- references a seeded sensor_type. Drop the dependent data first, or do
-- not revert.
--
-- Only the seeded rows are removed, so a row somebody added by hand
-- survives.

DELETE FROM sensor_type
WHERE magnitude IN (
    'ph', 'conductivity', 'irrigation_volume', 'temperature', 'humidity',
    'solar_radiation', 'adc_error', 'pulse_counter_error', 'i2c_error',
    'inverter_error', 'inverter_state', 'valve_position', 'inverter_on',
    'inverter_frequency'
);

DELETE FROM unit
WHERE symbol IN (
    '-', 'code', 'ppm', 'mS/cm', 'degC', '%', 'W/m2', 'L', 'deg', 'Hz'
);

DELETE FROM sensor_context
WHERE context IN ('soil', 'water', 'air', 'equipment');
