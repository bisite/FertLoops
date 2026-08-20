-- Reverts db/migrations/extensions/timescaledb/000002_reading_continuous_aggregates.up.sql
--
-- Remove the background policies BEFORE dropping the views. Dropping a
-- continuous aggregate does cascade-remove its own policies, but a
-- refresh or columnstore job that is mid-run can race the DROP.
-- if_exists keeps this safe if a policy was never added or already gone.
--
-- Removing the policies first is necessary but NOT sufficient on its own:
-- it only closes the race if each removal COMMITS before the DROP runs,
-- which means this source must be applied with x-multi-statement=true so
-- every statement autocommits. Applied whole-file (one transaction), the
-- scheduler never observes the policy removals and teardown fails
-- intermittently with `tuple concurrently deleted` -- measured at 2 of 3
-- attempts. See docs/adr/0008 and db/migrations/README.md.
--
-- reading_daily first throughout: it is the hierarchical aggregate built
-- on reading_hourly.

SELECT remove_continuous_aggregate_policy('reading_daily', if_exists => true);
SELECT remove_continuous_aggregate_policy('reading_hourly', if_exists => true);

CALL remove_columnstore_policy('reading_daily', if_exists => true);
CALL remove_columnstore_policy('reading_hourly', if_exists => true);

DROP MATERIALIZED VIEW reading_daily;
DROP MATERIALIZED VIEW reading_hourly;
