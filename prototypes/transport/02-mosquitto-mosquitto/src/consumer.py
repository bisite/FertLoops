#!/usr/bin/env python3
"""Stands in for whatever ingests on the VPS.

Durable session (clean_session false) with a stable client id and a QoS 1
subscription, i.e. the configuration the hub needs for store-and-forward to
mean anything. Appends one line per delivery to received.jsonl in arrival
order, so the file itself is the record of ordering.
"""

import json
import os
import pathlib
import time

import paho.mqtt.client as mqtt

RESULTS = pathlib.Path(os.environ.get("RESULTS_DIR", "/results"))
BROKER_HOST = os.environ.get("BROKER_HOST", "hub-broker")
BROKER_PORT = int(os.environ.get("BROKER_PORT", "1883"))
CLIENT_ID = os.environ.get("CLIENT_ID", "fl-consumer")
TOPIC_FILTER = os.environ.get("TOPIC_FILTER", "fertloops/#")


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = open(RESULTS / "received.jsonl", "a", buffering=1)
    counter = {"n": 0}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        print(f"[consumer] connected to {BROKER_HOST}: {reason_code}", flush=True)
        client.subscribe(TOPIC_FILTER, qos=1)

    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        print(f"[consumer] DISCONNECTED: {reason_code}", flush=True)

    def on_message(client, userdata, msg):
        now = time.time()
        try:
            seq = json.loads(msg.payload)["seq"]
        except (ValueError, KeyError):
            print(f"[consumer] unparseable payload, {len(msg.payload)} bytes", flush=True)
            return
        counter["n"] += 1
        out.write(
            '{"seq":%d,"t":%.6f,"qos":%d,"dup":%s,"bytes":%d}\n'
            % (seq, now, msg.qos, "true" if msg.dup else "false", len(msg.payload))
        )
        if counter["n"] % 200 == 0:
            print(f"[consumer] {counter['n']} deliveries (last seq {seq})", flush=True)

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=CLIENT_ID,
        clean_session=False,
        protocol=mqtt.MQTTv311,
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=2)
    # No client-side cap: a truncated consumer queue would be indistinguishable
    # from a truncated broker queue, which is the thing being measured.
    client.max_queued_messages_set(0)
    client.connect_async(BROKER_HOST, BROKER_PORT, keepalive=15)
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
