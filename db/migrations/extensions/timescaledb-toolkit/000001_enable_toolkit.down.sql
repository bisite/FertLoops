-- Reverts db/migrations/extensions/timescaledb-toolkit/000001_enable_toolkit.up.sql
--
-- Fails if any view/function in the database still depends on a toolkit
-- type or function (e.g. a saved query using time_weight/lttb) — drop
-- those first.

DROP EXTENSION timescaledb_toolkit;
