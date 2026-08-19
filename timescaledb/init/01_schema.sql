-- Sandbox schema for local experimentation (Mosquitto + TimescaleDB).
-- Mirrors the ESP32 frame from docs/trama-de-datos-riego.md as one row per frame.
-- NOT the canonical measurement model (that's decided in GitHub issue #7) -- just enough to
-- get data flowing end-to-end for the #3/#4 research tickets.

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE readings (
    time            TIMESTAMPTZ       NOT NULL,
    dev_id          TEXT              NOT NULL,
    ph              DOUBLE PRECISION,
    ce              DOUBLE PRECISION,
    solar           DOUBLE PRECISION,
    volume          DOUBLE PRECISION,
    soil_temp       DOUBLE PRECISION,
    soil_humidity   DOUBLE PRECISION,
    soil_conduct    DOUBLE PRECISION,
    air_temp        DOUBLE PRECISION,
    air_humidity    DOUBLE PRECISION,
    err_adc         SMALLINT,
    err_pulses      SMALLINT,
    err_i2c         SMALLINT,
    err_inverter    SMALLINT,
    err_inverter_state SMALLINT,
    valve           SMALLINT,
    inv_on          SMALLINT,
    inv_freq        DOUBLE PRECISION,
    raw             JSONB             NOT NULL
);

SELECT create_hypertable('readings', 'time');

CREATE INDEX ON readings (dev_id, time DESC);

-- MQTT QoS 1 is at-least-once: the same frame can be redelivered (broker
-- reconnect, unacked message replayed, etc). `time` is ingestion time so it
-- differs per redelivery -- a plain unique index on (dev_id, device Timestamp)
-- can't dedupe it here: TimescaleDB refuses a unique index on a hypertable
-- unless the partitioning column ("time") is part of it, which would defeat
-- the purpose (confirmed by testing -- this failed with "cannot create a
-- unique index without the column "time" (used in partitioning)" on a fresh
-- database). Redelivery dedup happens upstream instead, in Bento's pipeline
-- (see bento/fertloops.yaml), keyed on dev_id + the device's own Timestamp.
