-- Reverts db/migrations/core/000001_init_core_schema.up.sql.
--
-- Drop order respects FK dependencies (children before parents); no
-- CASCADE needed. Indexes, triggers, and comments are dropped
-- automatically with their owning table/function.

DROP TABLE reading;
DROP TABLE device_placement;
DROP TABLE sensor;
DROP TABLE device;
DROP TABLE location;
DROP TABLE sensor_type;
DROP TABLE unit;
DROP TABLE sensor_context;

DROP FUNCTION set_updated_at();
